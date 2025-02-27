from copy import deepcopy
import os
from typing import List, Optional, Union, Dict
import time
from dataclasses import dataclass

from deepeval.test_run.hyperparameters import process_hyperparameters
from deepeval.utils import (
    get_or_create_event_loop,
    should_ignore_errors,
    should_use_cache,
    should_verbose_print,
)
from deepeval.telemetry import capture_evaluation_run
from deepeval.metrics import BaseMetric, BaseConversationalMetric
from deepeval.metrics.indicator import (
    measure_metrics_with_indicator,
)
from deepeval.test_case import LLMTestCase, ConversationalTestCase
from deepeval.constants import PYTEST_RUN_TEST_NAME
from deepeval.test_run import (
    global_test_run_manager,
    LLMApiTestCase,
    ConversationalApiTestCase,
    MetricData,
    TestRunManager,
)
from deepeval.utils import get_is_running_deepeval, set_indicator
from deepeval.test_run.cache import (
    global_test_run_cache_manager,
    Cache,
    CachedTestCase,
    CachedMetricData,
)
from deepeval.tracing import get_trace_stack


@dataclass
class TestResult:
    """Returned from run_test"""

    success: bool
    metrics_data: List[MetricData]
    input: str
    actual_output: str
    expected_output: str
    context: List[str]
    retrieval_context: List[str]


def create_metric_data(metric: BaseMetric) -> MetricData:
    if metric.error is not None:
        return MetricData(
            name=metric.__name__,
            threshold=metric.threshold,
            score=None,
            reason=None,
            success=False,
            strictMode=metric.strict_mode,
            evaluationModel=metric.evaluation_model,
            error=metric.error,
            evaluationCost=metric.evaluation_cost,
            verboseLogs=metric.verbose_logs,
        )
    else:
        return MetricData(
            name=metric.__name__,
            score=metric.score,
            threshold=metric.threshold,
            reason=metric.reason,
            success=metric.is_successful(),
            strictMode=metric.strict_mode,
            evaluationModel=metric.evaluation_model,
            error=None,
            evaluationCost=metric.evaluation_cost,
            verboseLogs=metric.verbose_logs,
        )


def create_test_result(
    test_case: Union[LLMApiTestCase, ConversationalApiTestCase],
) -> TestResult:
    if isinstance(test_case, ConversationalApiTestCase):
        tc = test_case.messages[len(test_case.messages) - 1]
    else:
        tc = test_case

    return TestResult(
        success=tc.success,
        metrics_data=tc.metrics_data,
        input=tc.input,
        actual_output=tc.actual_output,
        expected_output=tc.expected_output,
        context=tc.context,
        retrieval_context=tc.retrieval_context,
    )


# Used to cache llm test cases that are part of conversation
llm_test_case_lookup_map: Dict[int, LLMApiTestCase] = {}
conversational_test_case_lookup_map: Dict[int, ConversationalApiTestCase] = {}


def create_api_test_case(
    test_case: Union[LLMTestCase, ConversationalTestCase],
    index: Optional[int] = None,
    conversational_instance_id: Optional[int] = None,
    additional_metadata: Optional[Dict] = None,
    comments: Optional[str] = None,
) -> Union[LLMApiTestCase, ConversationalApiTestCase]:
    if isinstance(test_case, LLMTestCase):
        if llm_test_case_lookup_map.get(id(test_case)):
            api_test_case = llm_test_case_lookup_map[id(test_case)]
            return llm_test_case_lookup_map[id(test_case)]

        if conversational_instance_id:
            success = None
            name = f"message_{index}"
            order = index

            # Manually set the metadata and comments on conversational test case
            # to each individual message (test case)
            test_case.additional_metadata = additional_metadata
            test_case.comments = comments
            trace_stack = None
        else:
            success = True
            name = os.getenv(PYTEST_RUN_TEST_NAME, f"test_case_{index}")
            order = test_case._dataset_rank
            trace_stack = get_trace_stack()

        api_test_case = LLMApiTestCase(
            name=name,
            input=test_case.input,
            actualOutput=test_case.actual_output,
            expectedOutput=test_case.expected_output,
            context=test_case.context,
            retrievalContext=test_case.retrieval_context,
            toolsUsed=test_case.tools_used,
            expectedTools=test_case.expected_tools,
            success=success,
            metricsData=None,
            runDuration=None,
            evaluationCost=None,
            order=order,
            additionalMetadata=test_case.additional_metadata,
            comments=test_case.comments,
            traceStack=trace_stack,
            conversational_instance_id=conversational_instance_id,
        )

        if conversational_instance_id:
            llm_test_case_lookup_map[id(test_case)] = api_test_case

        return api_test_case

    elif isinstance(test_case, ConversationalTestCase):
        api_test_case = ConversationalApiTestCase(
            name=os.getenv(
                PYTEST_RUN_TEST_NAME, f"conversational_test_case_{index}"
            ),
            success=True,
            metricsData=None,
            runDuration=0,
            evaluationCost=None,
            order=test_case._dataset_rank,
            testCases=[],
        )
        api_test_case.instance_id = id(api_test_case)
        api_test_case.messages = [
            create_api_test_case(
                message.llm_test_case,
                index,
                api_test_case.instance_id,
                test_case.additional_metadata,
                test_case.comments,
            )
            for index, message in enumerate(test_case.messages)
        ]

        return api_test_case


def execute_test_cases(
    test_cases: List[Union[LLMTestCase, ConversationalTestCase]],
    metrics: List[Union[BaseMetric, BaseConversationalMetric]],
    ignore_errors: bool,
    use_cache: bool,
    save_to_disk: bool = False,
    verbose_mode: Optional[bool] = None,
    test_run_manager: Optional[TestRunManager] = None,
) -> List[TestResult]:
    test_results: List[TestResult] = []
    global_test_run_cache_manager.disable_write_cache = save_to_disk == False

    if test_run_manager is None:
        test_run_manager = global_test_run_manager

    test_run_manager.save_to_disk = save_to_disk
    test_run = test_run_manager.get_test_run()

    if verbose_mode is not None:
        for metric in metrics:
            metric.verbose_mode = verbose_mode

    global llm_test_case_lookup_map
    llm_test_case_lookup_map = {}
    for test_case in test_cases:
        if isinstance(test_case, ConversationalTestCase):
            for message in test_case.messages:
                # Messages in a conversation must be appended
                # at the end of test_cases to ensure conversational test cases
                # are always created before its messages, and llm_test_case_count
                # is always right
                if message.should_evaluate:
                    test_cases.append(message.llm_test_case)

    llm_test_case_count = -1
    conversational_test_case_count = -1
    conversational_metrics: List[BaseConversationalMetric] = []
    llm_metrics: List[BaseMetric] = []
    for metric in metrics:
        if isinstance(metric, BaseMetric):
            llm_metrics.append(metric)
        elif isinstance(metric, BaseConversationalMetric):
            conversational_metrics.append(metric)

    for test_case in test_cases:
        with capture_evaluation_run("test case"):
            for metric in metrics:
                metric.error = None  # Reset metric error

            if isinstance(test_case, LLMTestCase):
                if len(llm_metrics) == 0:
                    continue

                llm_test_case_count += 1
                cached_test_case = None
                if use_cache:
                    cached_test_case = (
                        global_test_run_cache_manager.get_cached_test_case(
                            test_case, test_run.hyperparameters
                        )
                    )

                ##### Metric Calculation #####
                api_test_case: LLMApiTestCase = create_api_test_case(
                    test_case, llm_test_case_count
                )
                new_cached_test_case: CachedTestCase = CachedTestCase()

                test_start_time = time.perf_counter()
                read_all_metrics_from_cache = True
                for metric in llm_metrics:
                    metric_data = None
                    if cached_test_case is not None:
                        cached_metric_data = Cache.get_metric_data(
                            metric, cached_test_case
                        )
                        if cached_metric_data:
                            metric_data = cached_metric_data.metric_data

                    if metric_data is None:
                        read_all_metrics_from_cache = False
                        metric.async_mode = False  # Override metric async
                        try:
                            metric.measure(test_case)
                        except Exception as e:
                            if ignore_errors:
                                metric.error = str(e)  # Override metric error
                                metric.success = (
                                    False  # Override metric success
                                )
                            else:
                                raise
                        metric_data = create_metric_data(metric)

                    # here, we will check for an additional property on the flattened test cases to see if updating is necessary
                    api_test_case.update_metric_data(metric_data)
                    if metric.error is None:
                        cache_metric_data = deepcopy(metric_data)
                        cache_metric_data.evaluation_cost = 0  # Cached metrics will have evaluation cost as 0, not None.
                        updated_cached_metric_data = CachedMetricData(
                            metric_data=cache_metric_data,
                            metric_configuration=Cache.create_metric_configuration(
                                metric
                            ),
                        )
                        new_cached_test_case.cached_metrics_data.append(
                            updated_cached_metric_data
                        )

                test_end_time = time.perf_counter()
                if read_all_metrics_from_cache:
                    run_duration = 0
                else:
                    run_duration = test_end_time - test_start_time
                api_test_case.update_run_duration(run_duration)

                ### Update Test Run ###
                test_run_manager.update_test_run(api_test_case, test_case)

                ### Cache Test Run ###
                global_test_run_cache_manager.cache_test_case(
                    test_case,
                    new_cached_test_case,
                    test_run.hyperparameters,
                )
                global_test_run_cache_manager.cache_test_case(
                    test_case,
                    new_cached_test_case,
                    test_run.hyperparameters,
                    to_temp=True,
                )

                test_result = create_test_result(api_test_case)
                test_results.append(test_result)

            # No caching for conversational metrics yet
            elif isinstance(test_case, ConversationalTestCase):
                conversational_test_case_count += 1
                api_test_case: ConversationalApiTestCase = create_api_test_case(
                    test_case, conversational_test_case_count
                )

                test_start_time = time.perf_counter()
                for metric in conversational_metrics:
                    # Skip non conversational metrics for converstaional test cases
                    if isinstance(metric, BaseConversationalMetric):
                        metric.async_mode = False  # Override metric async
                        try:
                            metric.measure(test_case)
                        except Exception as e:
                            if ignore_errors:
                                metric.error = str(e)  # Override metric error
                                metric.success = (
                                    False  # Override metric success
                                )
                            else:
                                raise
                        metric_data = create_metric_data(metric)
                        api_test_case.update_metric_data(metric_data)

                test_end_time = time.perf_counter()
                if len(conversational_metrics) > 0:
                    run_duration = test_end_time - test_start_time
                    api_test_case.update_run_duration(run_duration)

                ### Update Test Run ###
                test_run_manager.update_test_run(api_test_case, test_case)

    return test_results


async def a_execute_test_cases(
    test_cases: List[Union[LLMTestCase, ConversationalTestCase]],
    metrics: List[Union[BaseMetric, BaseConversationalMetric]],
    ignore_errors: bool,
    use_cache: bool,
    save_to_disk: bool = False,
    verbose_mode: Optional[bool] = None,
    test_run_manager: Optional[TestRunManager] = None,
) -> List[TestResult]:
    test_results: List[TestResult] = []
    global_test_run_cache_manager.disable_write_cache = save_to_disk == False

    if test_run_manager is None:
        test_run_manager = global_test_run_manager

    test_run_manager.save_to_disk = save_to_disk
    test_run = test_run_manager.get_test_run()

    if verbose_mode is not None:
        for metric in metrics:
            metric.verbose_mode = verbose_mode

    global llm_test_case_lookup_map
    llm_test_case_lookup_map = {}
    for test_case in test_cases:
        if isinstance(test_case, ConversationalTestCase):
            for message in test_case.messages:
                # Messages in a conversation must be appended
                # at the end of test_cases to ensure conversational test cases
                # are always created before its messages, and llm_test_case_count
                # is always right
                if message.should_evaluate:
                    test_cases.append(message.llm_test_case)

    llm_test_case_count = -1
    conversational_test_case_count = -1
    conversational_metrics: List[BaseConversationalMetric] = []
    llm_metrics: List[BaseMetric] = []
    for metric in metrics:
        if isinstance(metric, BaseMetric):
            llm_metrics.append(metric)
        elif isinstance(metric, BaseConversationalMetric):
            conversational_metrics.append(metric)

    for test_case in test_cases:
        with capture_evaluation_run("test case"):
            if isinstance(test_case, LLMTestCase):
                if len(llm_metrics) == 0:
                    continue

                llm_test_case_count += 1
                cached_test_case = None
                for metric in metrics:
                    metric.error = None  # Reset metric error

                # only use cache when NOT conversational test case
                if use_cache:
                    cached_test_case = (
                        global_test_run_cache_manager.get_cached_test_case(
                            test_case,
                            test_run.hyperparameters,
                        )
                    )

                ##### Metric Calculation #####
                api_test_case = create_api_test_case(
                    test_case, llm_test_case_count
                )

                new_cached_test_case: CachedTestCase = CachedTestCase()
                test_start_time = time.perf_counter()
                await measure_metrics_with_indicator(
                    llm_metrics, test_case, cached_test_case, ignore_errors
                )

                for metric in llm_metrics:
                    metric_data = create_metric_data(metric)
                    api_test_case.update_metric_data(metric_data)

                    if metric.error is None:
                        cache_metric_data = deepcopy(metric_data)
                        cache_metric_data.evaluation_cost = (
                            0  # Create new copy and save 0 for cost
                        )
                        updated_cached_metric_data = CachedMetricData(
                            metric_data=cache_metric_data,
                            metric_configuration=Cache.create_metric_configuration(
                                metric
                            ),
                        )
                        new_cached_test_case.cached_metrics_data.append(
                            updated_cached_metric_data
                        )

                test_end_time = time.perf_counter()
                run_duration = test_end_time - test_start_time
                # Quick hack to check if all metrics were from cache
                if run_duration < 1:
                    run_duration = 0
                api_test_case.update_run_duration(run_duration)

                ### Update Test Run ###
                test_run_manager.update_test_run(api_test_case, test_case)

                ### Cache Test Run ###
                global_test_run_cache_manager.cache_test_case(
                    test_case,
                    new_cached_test_case,
                    test_run.hyperparameters,
                )
                global_test_run_cache_manager.cache_test_case(
                    test_case,
                    new_cached_test_case,
                    test_run.hyperparameters,
                    to_temp=True,
                )

                test_result = create_test_result(api_test_case)
                test_results.append(test_result)

            elif isinstance(test_case, ConversationalTestCase):
                conversational_test_case_count += 1
                api_test_case: ConversationalApiTestCase = create_api_test_case(
                    test_case, conversational_test_case_count
                )

                test_start_time = time.perf_counter()
                await measure_metrics_with_indicator(
                    conversational_metrics, test_case, None, ignore_errors
                )
                for metric in conversational_metrics:
                    metric_data = create_metric_data(metric)
                    api_test_case.update_metric_data(metric_data)

                test_end_time = time.perf_counter()
                if len(conversational_metrics) > 0:
                    run_duration = test_end_time - test_start_time
                    api_test_case.update_run_duration(run_duration)

                ### Update Test Run ###
                test_run_manager.update_test_run(api_test_case, test_case)

    return test_results


def assert_test(
    test_case: Union[LLMTestCase, ConversationalTestCase],
    metrics: List[Union[BaseMetric, BaseConversationalMetric]],
    run_async: bool = True,
):
    if run_async:
        loop = get_or_create_event_loop()
        test_result = loop.run_until_complete(
            a_execute_test_cases(
                [test_case],
                metrics,
                ignore_errors=should_ignore_errors(),
                use_cache=should_use_cache(),
                verbose_mode=should_verbose_print(),
                save_to_disk=get_is_running_deepeval(),
            )
        )[0]
    else:
        test_result = execute_test_cases(
            [test_case],
            metrics,
            ignore_errors=should_ignore_errors(),
            use_cache=should_use_cache(),
            verbose_mode=should_verbose_print(),
            save_to_disk=get_is_running_deepeval(),
        )[0]

    if not test_result.success:
        failed_metrics_data: List[MetricData] = []
        # even for conversations, test_result right now is just the
        # result for the last message
        for metric_data in test_result.metrics_data:
            if metric_data.error is not None:
                failed_metrics_data.append(metric_data)
            else:
                # This try block is for user defined custom metrics,
                # which might not handle the score == undefined case elegantly
                try:
                    if not metric_data.success:
                        failed_metrics_data.append(metric_data)
                except:
                    failed_metrics_data.append(metric_data)

        failed_metrics_str = ", ".join(
            [
                f"{metrics_data.name} (score: {metrics_data.score}, threshold: {metrics_data.threshold}, strict: {metrics_data.strict_mode}, error: {metrics_data.error})"
                for metrics_data in failed_metrics_data
            ]
        )
        raise AssertionError(f"Metrics: {failed_metrics_str} failed.")


def evaluate(
    test_cases: List[Union[LLMTestCase, ConversationalTestCase]],
    metrics: List[BaseMetric],
    hyperparameters: Optional[Dict[str, Union[str, int, float]]] = None,
    run_async: bool = True,
    show_indicator: bool = True,
    print_results: bool = True,
    write_cache: bool = True,
    use_cache: bool = False,
    ignore_errors: bool = False,
    verbose_mode: Optional[bool] = None,
):
    if hyperparameters is not None:
        if (
            hyperparameters.get("model") is None
            or hyperparameters.get("prompt template") is None
        ):
            raise ValueError(
                "A `model` and `prompt template` key must be provided when logging `hyperparameters`."
            )
        hyperparameters = process_hyperparameters(hyperparameters)

    set_indicator(show_indicator)

    global_test_run_manager.reset()
    start_time = time.perf_counter()
    if print_results:
        print("Evaluating test cases...")

    with capture_evaluation_run("evaluate()"):
        if run_async:
            loop = get_or_create_event_loop()
            test_results = loop.run_until_complete(
                a_execute_test_cases(
                    test_cases,
                    metrics,
                    ignore_errors=ignore_errors,
                    use_cache=use_cache,
                    verbose_mode=verbose_mode,
                    save_to_disk=write_cache,
                )
            )
        else:
            test_results = execute_test_cases(
                test_cases,
                metrics,
                ignore_errors=ignore_errors,
                use_cache=use_cache,
                verbose_mode=verbose_mode,
                save_to_disk=write_cache,
            )

    end_time = time.perf_counter()
    run_duration = end_time - start_time
    if print_results:
        for test_result in test_results:
            print_test_result(test_result)

        aggregate_metric_pass_rates(test_results)

    test_run = global_test_run_manager.get_test_run()
    test_run.hyperparameters = hyperparameters
    global_test_run_manager.save_test_run()
    global_test_run_manager.wrap_up_test_run(run_duration, display_table=False)
    return test_results


def print_test_result(test_result: TestResult):
    print("")
    print("=" * 70 + "\n")
    print("Metrics Summary\n")
    for metric_data in test_result.metrics_data:
        successful = True
        if metric_data.error is not None:
            successful = False
        else:
            # This try block is for user defined custom metrics,
            # which might not handle the score == undefined case elegantly
            try:
                if not metric_data.success:
                    successful = False
            except:
                successful = False

        if not successful:
            print(
                f"  - ❌ {metric_data.name} (score: {metric_data.score}, threshold: {metric_data.threshold}, strict: {metric_data.strict_mode}, evaluation model: {metric_data.evaluation_model}, reason: {metric_data.reason}, error: {metric_data.error})"
            )
        else:
            print(
                f"  - ✅ {metric_data.name} (score: {metric_data.score}, threshold: {metric_data.threshold}, strict: {metric_data.strict_mode}, evaluation model: {metric_data.evaluation_model}, reason: {metric_data.reason}, error: {metric_data.error})"
            )

    print("")
    print("For test case:\n")
    print(f"  - input: {test_result.input}")
    print(f"  - actual output: {test_result.actual_output}")
    print(f"  - expected output: {test_result.expected_output}")
    print(f"  - context: {test_result.context}")
    print(f"  - retrieval context: {test_result.retrieval_context}")


def aggregate_metric_pass_rates(test_results: List[TestResult]) -> dict:
    metric_counts = {}
    metric_successes = {}

    for result in test_results:
        for metric_data in result.metrics_data:
            metric_name = metric_data.name
            if metric_name not in metric_counts:
                metric_counts[metric_name] = 0
                metric_successes[metric_name] = 0
            metric_counts[metric_name] += 1
            if metric_data.success:
                metric_successes[metric_name] += 1

    metric_pass_rates = {
        metric: (metric_successes[metric] / metric_counts[metric])
        for metric in metric_counts
    }

    print("\n" + "=" * 70 + "\n")
    print("Overall Metric Pass Rates\n")
    for metric, pass_rate in metric_pass_rates.items():
        print(f"{metric}: {pass_rate:.2%} pass rate")
    print("\n" + "=" * 70 + "\n")

    return metric_pass_rates
