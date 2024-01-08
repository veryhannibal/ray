import asyncio
import inspect
import logging
import os
import pickle
import time
import traceback
from contextlib import contextmanager
from importlib import import_module
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Tuple, Union

import aiorwlock
import starlette.responses
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

import ray
from ray import cloudpickle
from ray._private.async_compat import sync_to_async
from ray._private.utils import get_or_create_event_loop
from ray.actor import ActorClass
from ray.remote_function import RemoteFunction
from ray.serve import metrics
from ray.serve._private.autoscaling_metrics import InMemoryMetricsStore
from ray.serve._private.common import (
    DeploymentID,
    ReplicaName,
    ReplicaTag,
    ServeComponentType,
    StreamingHTTPRequest,
    gRPCRequest,
)
from ray.serve._private.config import DeploymentConfig
from ray.serve._private.constants import (
    DEFAULT_LATENCY_BUCKET_MS,
    GRPC_CONTEXT_ARG_NAME,
    HEALTH_CHECK_METHOD,
    RAY_SERVE_GAUGE_METRIC_SET_PERIOD_S,
    RAY_SERVE_REPLICA_AUTOSCALING_METRIC_RECORD_PERIOD_S,
    RECONFIGURE_METHOD,
    REPLICA_CONTROL_PLANE_CONCURRENCY_GROUP,
    SERVE_CONTROLLER_NAME,
    SERVE_LOGGER_NAME,
    SERVE_NAMESPACE,
)
from ray.serve._private.http_util import (
    ASGIAppReplicaWrapper,
    ASGIMessageQueue,
    ASGIReceiveProxy,
    Response,
)
from ray.serve._private.logging_utils import (
    access_log_msg,
    configure_component_cpu_profiler,
    configure_component_logger,
    configure_component_memory_profiler,
    get_component_logger_file_path,
)
from ray.serve._private.router import RequestMetadata
from ray.serve._private.utils import (
    MetricsPusher,
    merge_dict,
    parse_import_path,
    wrap_to_ray_error,
)
from ray.serve._private.version import DeploymentVersion
from ray.serve.config import AutoscalingConfig
from ray.serve.deployment import Deployment
from ray.serve.exceptions import RayServeException
from ray.serve.schema import LoggingConfig

logger = logging.getLogger(SERVE_LOGGER_NAME)


def _load_deployment_def_from_import_path(import_path: str) -> Callable:
    module_name, attr_name = parse_import_path(import_path)
    deployment_def = getattr(import_module(module_name), attr_name)

    # For ray or serve decorated class or function, strip to return
    # original body.
    if isinstance(deployment_def, RemoteFunction):
        deployment_def = deployment_def._function
    elif isinstance(deployment_def, ActorClass):
        deployment_def = deployment_def.__ray_metadata__.modified_class
    elif isinstance(deployment_def, Deployment):
        logger.warning(
            f'The import path "{import_path}" contains a '
            "decorated Serve deployment. The decorator's settings "
            "are ignored when deploying via import path."
        )
        deployment_def = deployment_def.func_or_class

    return deployment_def


class ReplicaMetricsManager:
    """Manages metrics for the replica.

    A variety of metrics are managed:
        - Fine-grained metrics are set for every request.
        - Autoscaling statistics are periodically pushed to the controller.
        - Queue length metrics are periodically recorded as user-facing gauges.
    """

    def __init__(
        self,
        replica_tag: ReplicaTag,
        deployment_id: DeploymentID,
        autoscaling_config: Optional[AutoscalingConfig],
    ):
        self._replica_tag = replica_tag
        self._deployment_id = deployment_id
        self._metrics_pusher = MetricsPusher()
        self._metrics_store = InMemoryMetricsStore()
        self._autoscaling_config = autoscaling_config
        self._controller_handle = ray.get_actor(
            SERVE_CONTROLLER_NAME, namespace=SERVE_NAMESPACE
        )

        # Request counter (only set on replica startup).
        self._restart_counter = metrics.Counter(
            "serve_deployment_replica_starts",
            description=(
                "The number of times this replica has been restarted due to failure."
            ),
        )
        self._restart_counter.inc()

        # Per-request metrics.
        self._request_counter = metrics.Counter(
            "serve_deployment_request_counter",
            description=(
                "The number of queries that have been processed in this replica."
            ),
            tag_keys=("route",),
        )

        self._error_counter = metrics.Counter(
            "serve_deployment_error_counter",
            description=(
                "The number of exceptions that have occurred in this replica."
            ),
            tag_keys=("route",),
        )

        self._processing_latency_tracker = metrics.Histogram(
            "serve_deployment_processing_latency_ms",
            description="The latency for queries to be processed.",
            boundaries=DEFAULT_LATENCY_BUCKET_MS,
            tag_keys=("route",),
        )

        # User-facing Prometheus gauges.
        self._num_pending_items = metrics.Gauge(
            "serve_replica_pending_queries",
            description="The current number of pending queries.",
        )
        self._num_processing_items = metrics.Gauge(
            "serve_replica_processing_queries",
            description="The current number of queries being processed.",
        )

        # Set user-facing gauges periodically.
        self._metrics_pusher.register_task(
            self._set_replica_requests_metrics,
            RAY_SERVE_GAUGE_METRIC_SET_PERIOD_S,
        )

        if self._autoscaling_config:
            # Push autoscaling metrics to the controller periodically.
            self._metrics_pusher.register_task(
                self._collect_autoscaling_metrics,
                self._autoscaling_config.metrics_interval_s,
                self._controller_handle.record_autoscaling_metrics.remote,
            )
            # Collect autoscaling metrics locally periodically.
            self._metrics_pusher.register_task(
                self.get_num_pending_and_running_requests,
                min(
                    RAY_SERVE_REPLICA_AUTOSCALING_METRIC_RECORD_PERIOD_S,
                    self._autoscaling_config.metrics_interval_s,
                ),
                self._add_autoscaling_metrics_point,
            )

    def start(self):
        """Start periodic background tasks."""
        self._metrics_pusher.start()

    def shutdown(self):
        """Stop periodic background tasks."""
        self._metrics_pusher.shutdown()

    def set_autoscaling_config(self, autoscaling_config: AutoscalingConfig):
        """Dynamically update autoscaling config."""
        self._autoscaling_config = autoscaling_config

    def get_num_pending_and_running_requests(self) -> int:
        """Get current total queue length of requests for this replica."""
        stats = self._get_handle_request_stats() or {}
        return stats.get("pending", 0) + stats.get("running", 0)

    def record_request_metrics(
        self, *, route: str, status_str: str, latency_ms: float, was_error: bool
    ):
        """Records per-request metrics."""
        self._processing_latency_tracker.observe(latency_ms, tags={"route": route})
        if was_error:
            self._error_counter.inc(tags={"route": route})
        else:
            self._request_counter.inc(tags={"route": route})

    def _collect_autoscaling_metrics(self):
        look_back_period = self._autoscaling_config.look_back_period_s
        return self._replica_tag, self._metrics_store.window_average(
            self._replica_tag, time.time() - look_back_period
        )

    def _add_autoscaling_metrics_point(self, data, send_timestamp: float):
        self._metrics_store.add_metrics_point(
            {self._replica_tag: data},
            send_timestamp,
        )

    def _set_replica_requests_metrics(self):
        self._num_processing_items.set(self._get_num_running_requests())
        self._num_pending_items.set(self._get_num_pending_requests())

    def _get_num_running_requests(self) -> int:
        stats = self._get_handle_request_stats() or {}
        return stats.get("running", 0)

    def _get_num_pending_requests(self) -> int:
        stats = self._get_handle_request_stats() or {}
        return stats.get("pending", 0)

    def _get_handle_request_stats(self) -> Optional[Dict[str, int]]:
        replica_actor_name = self._deployment_id.to_replica_actor_class_name()
        actor_stats = ray.runtime_context.get_runtime_context()._get_actor_call_stats()
        method_stats = actor_stats.get(f"{replica_actor_name}.handle_request")
        streaming_method_stats = actor_stats.get(
            f"{replica_actor_name}.handle_request_streaming"
        )
        method_stats_java = actor_stats.get(
            f"{replica_actor_name}.handle_request_from_java"
        )
        return merge_dict(
            merge_dict(method_stats, streaming_method_stats), method_stats_java
        )


class ReplicaActor:
    """Actor definition for replicas of Ray Serve deployments.

    This class defines the interface that the controller and deployment handles
    (i.e., from proxies and other replicas) use to interact with a replica.

    All interaction with the user-provided callable is done via the
    `UserCallableWrapper` class.
    """

    async def __init__(
        self,
        deployment_id: DeploymentID,
        replica_tag: str,
        serialized_deployment_def: bytes,
        serialized_init_args: bytes,
        serialized_init_kwargs: bytes,
        deployment_config_proto_bytes: bytes,
        version: DeploymentVersion,
    ):
        self._version = version
        self._replica_tag = replica_tag
        self._deployment_id = deployment_id
        self._deployment_config = DeploymentConfig.from_proto_bytes(
            deployment_config_proto_bytes
        )
        self._configure_logger_and_profilers(self._deployment_config.logging_config)
        self._event_loop = get_or_create_event_loop()

        deployment_def = cloudpickle.loads(serialized_deployment_def)
        if isinstance(deployment_def, str):
            deployment_def = _load_deployment_def_from_import_path(deployment_def)

        self._user_callable_wrapper = UserCallableWrapper(
            deployment_def,
            cloudpickle.loads(serialized_init_args),
            cloudpickle.loads(serialized_init_kwargs),
            deployment_id=deployment_id,
        )

        # Guards against calling the user's callable constructor multiple times.
        self._user_callable_initialized = False
        self._user_callable_initialized_lock = asyncio.Lock()

        # Set metadata for logs and metrics.
        # servable_object will be populated in `initialize_and_get_metadata`.
        self._set_internal_replica_context(servable_object=None)

        self._metrics_manager = ReplicaMetricsManager(
            replica_tag, deployment_id, self._deployment_config.autoscaling_config
        )
        self._metrics_manager.start()

    def _set_internal_replica_context(self, *, servable_object: Callable = None):
        ray.serve.context._set_internal_replica_context(
            app_name=self._deployment_id.app,
            deployment=self._deployment_id.name,
            replica_tag=self._replica_tag,
            servable_object=servable_object,
        )

    def _configure_logger_and_profilers(
        self, logging_config: Union[None, Dict, LoggingConfig]
    ):
        if logging_config is None:
            logging_config = {}
        if isinstance(logging_config, dict):
            logging_config = LoggingConfig(**logging_config)

        replica_name = ReplicaName.from_replica_tag(self._replica_tag)
        if replica_name.app_name:
            component_name = f"{replica_name.app_name}_{replica_name.deployment_name}"
        else:
            component_name = f"{replica_name.deployment_name}"
        component_id = replica_name.replica_suffix

        configure_component_logger(
            component_type=ServeComponentType.REPLICA,
            component_name=component_name,
            component_id=component_id,
            logging_config=logging_config,
        )
        configure_component_memory_profiler(
            component_type=ServeComponentType.REPLICA,
            component_name=component_name,
            component_id=component_id,
        )
        self.cpu_profiler, self.cpu_profiler_log = configure_component_cpu_profiler(
            component_type=ServeComponentType.REPLICA,
            component_name=component_name,
            component_id=component_id,
        )

    @ray.method(concurrency_group=REPLICA_CONTROL_PLANE_CONCURRENCY_GROUP)
    def get_num_ongoing_requests(self) -> int:
        """Fetch the number of ongoing requests at this replica (queue length).

        This runs on a separate thread (using a Ray concurrency group) so it will
        not be blocked by user code.
        """
        return self._metrics_manager.get_num_pending_and_running_requests()

    @contextmanager
    def _wrap_user_method_call(self, request_metadata: RequestMetadata):
        """Context manager that wraps user method calls.

        1) Sets the request context var with appropriate metadata.
        2) Records the access log message (if not disabled).
        3) Records per-request metrics via the metrics manager.
        """
        ray.serve.context._serve_request_context.set(
            ray.serve.context._RequestContext(
                request_metadata.route,
                request_metadata.request_id,
                self._deployment_id.app,
                request_metadata.multiplexed_model_id,
                request_metadata.grpc_context,
            )
        )

        start_time = time.time()
        user_exception = None
        try:
            yield
        except Exception as e:
            user_exception = e
            logger.error(f"Request failed:\n{e}")
            if ray.util.pdb._is_ray_debugger_enabled():
                ray.util.pdb._post_mortem()

        latency_ms = (time.time() - start_time) * 1000
        if user_exception is None:
            status_str = "OK"
        elif isinstance(user_exception, asyncio.CancelledError):
            status_str = "CANCELLED"
        else:
            status_str = "ERROR"

        logger.info(
            access_log_msg(
                method=request_metadata.call_method,
                status=status_str,
                latency_ms=latency_ms,
            ),
            extra={"serve_access_log": True},
        )
        self._metrics_manager.record_request_metrics(
            route=request_metadata.route,
            status_str=status_str,
            latency_ms=latency_ms,
            was_error=user_exception is not None,
        )

        if user_exception is not None:
            raise user_exception from None

    async def handle_request(
        self,
        pickled_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> Tuple[bytes, Any]:
        """Entrypoint for all `stream=False` calls."""
        request_metadata = pickle.loads(pickled_request_metadata)
        with self._wrap_user_method_call(request_metadata):
            return await self._user_callable_wrapper.call_user_method(
                request_metadata, request_args, request_kwargs
            )

    async def _handle_http_request_generator(
        self,
        request_metadata: RequestMetadata,
        request: StreamingHTTPRequest,
    ) -> AsyncGenerator[Message, None]:
        """Handle an HTTP request and stream ASGI messages to the caller.

        This is a generator that yields ASGI-compliant messages sent by user code
        via an ASGI send interface.
        """
        receiver_task = None
        call_user_method_task = None
        wait_for_message_task = None
        try:
            receiver = ASGIReceiveProxy(
                request_metadata.request_id, request.http_proxy_handle
            )
            receiver_task = self._event_loop.create_task(
                receiver.fetch_until_disconnect()
            )

            scope = pickle.loads(request.pickled_asgi_scope)
            asgi_queue_send = ASGIMessageQueue()
            request_args = (scope, receiver, asgi_queue_send)
            request_kwargs = {}

            # Handle the request in a background asyncio.Task. It's expected that
            # this task will use the provided ASGI send interface to send its HTTP
            # the response. We will poll for the sent messages and yield them back
            # to the caller.
            call_user_method_task = self._event_loop.create_task(
                self._user_callable_wrapper.call_user_method(
                    request_metadata, request_args, request_kwargs
                )
            )

            while True:
                wait_for_message_task = self._event_loop.create_task(
                    asgi_queue_send.wait_for_message()
                )
                done, _ = await asyncio.wait(
                    [call_user_method_task, wait_for_message_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Consume and yield all available messages in the queue.
                # The messages are batched into a list to avoid unnecessary RPCs and
                # we use vanilla pickle because it's faster than cloudpickle and we
                # know it's safe for these messages containing primitive types.
                yield pickle.dumps(asgi_queue_send.get_messages_nowait())

                # Exit once `call_user_method` has finished. In this case, all
                # messages must have already been sent.
                if call_user_method_task in done:
                    break

            e = call_user_method_task.exception()
            if e is not None:
                raise e from None
        finally:
            if receiver_task is not None:
                receiver_task.cancel()

            if call_user_method_task is not None and not call_user_method_task.done():
                call_user_method_task.cancel()

            if wait_for_message_task is not None and not wait_for_message_task.done():
                wait_for_message_task.cancel()

    async def handle_request_streaming(
        self,
        pickled_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> AsyncGenerator[Any, None]:
        """Generator that is the entrypoint for all `stream=True` handle calls."""
        request_metadata = pickle.loads(pickled_request_metadata)
        with self._wrap_user_method_call(request_metadata):
            if request_metadata.is_http_request:
                assert len(request_args) == 1 and isinstance(
                    request_args[0], StreamingHTTPRequest
                )
                generator = self._handle_http_request_generator(
                    request_metadata, request_args[0]
                )
            else:
                generator = self._user_callable_wrapper.call_user_method_generator(
                    request_metadata, request_args, request_kwargs
                )

            async for result in generator:
                yield result

    async def handle_request_from_java(
        self,
        proto_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> Any:
        from ray.serve.generated.serve_pb2 import (
            RequestMetadata as RequestMetadataProto,
        )

        proto = RequestMetadataProto.FromString(proto_request_metadata)
        request_metadata: RequestMetadata = RequestMetadata(
            proto.request_id,
            proto.endpoint,
            call_method=proto.call_method,
            multiplexed_model_id=proto.multiplexed_model_id,
            route=proto.route,
        )
        with self._wrap_user_method_call(request_metadata):
            return await self._user_callable_wrapper.call_user_method(
                request_metadata, request_args[0], request_kwargs
            )

    async def is_allocated(self) -> str:
        """poke the replica to check whether it's alive.

        When calling this method on an ActorHandle, it will complete as
        soon as the actor has started running. We use this mechanism to
        detect when a replica has been allocated a worker slot.
        At this time, the replica can transition from PENDING_ALLOCATION
        to PENDING_INITIALIZATION startup state.

        Returns:
            The PID, actor ID, node ID, node IP, and log filepath id of the replica.
        """

        return (
            os.getpid(),
            ray.get_runtime_context().get_actor_id(),
            ray.get_runtime_context().get_worker_id(),
            ray.get_runtime_context().get_node_id(),
            ray.util.get_node_ip_address(),
            get_component_logger_file_path(),
        )

    async def initialize_and_get_metadata(
        self,
        deployment_config: DeploymentConfig = None,
        _after: Optional[Any] = None,
    ) -> Tuple[DeploymentConfig, DeploymentVersion]:
        # Unused `_after` argument is for scheduling: passing an ObjectRef
        # allows delaying this call until after the `_after` call has returned.
        try:
            # Ensure that initialization is only performed once.
            # When controller restarts, it will call this method again.
            async with self._user_callable_initialized_lock:
                if not self._user_callable_initialized:
                    await self._user_callable_wrapper.initialize_callable()
                    self._user_callable_initialized = True
                    self._set_internal_replica_context(
                        servable_object=self._user_callable_wrapper.user_callable
                    )
                if deployment_config:
                    await self._user_callable_wrapper.call_reconfigure(
                        deployment_config.user_config
                    )

            # A new replica should not be considered healthy until it passes
            # an initial health check. If an initial health check fails,
            # consider it an initialization failure.
            await self.check_health()
            return self._get_metadata()
        except Exception:
            raise RuntimeError(traceback.format_exc()) from None

    async def reconfigure(
        self,
        deployment_config: DeploymentConfig,
    ) -> Tuple[DeploymentConfig, DeploymentVersion]:
        try:
            user_config_changed = (
                deployment_config.user_config != self._deployment_config.user_config
            )
            logging_config_changed = (
                deployment_config.logging_config
                != self._deployment_config.logging_config
            )
            self._deployment_config = deployment_config
            self._version = DeploymentVersion.from_deployment_version(
                self._version, deployment_config
            )

            self._metrics_manager.set_autoscaling_config(
                deployment_config.autoscaling_config
            )
            if logging_config_changed:
                self._configure_logger_and_profilers(deployment_config.logging_config)

            if user_config_changed:
                await self._user_callable_wrapper.call_reconfigure(
                    deployment_config.user_config
                )

            return self._get_metadata()
        except Exception:
            raise RuntimeError(traceback.format_exc()) from None

    def _get_metadata(
        self,
    ) -> Tuple[DeploymentConfig, DeploymentVersion]:
        return (
            self._version.deployment_config,
            self._version,
        )

    def _save_cpu_profile_data(self) -> str:
        """Saves CPU profiling data, if CPU profiling is enabled.

        Logs a warning if CPU profiling is disabled.
        """

        if self.cpu_profiler is not None:
            import marshal

            self.cpu_profiler.snapshot_stats()
            with open(self.cpu_profiler_log, "wb") as f:
                marshal.dump(self.cpu_profiler.stats, f)
            logger.info(f'Saved CPU profile data to file "{self.cpu_profiler_log}"')
            return self.cpu_profiler_log
        else:
            logger.error(
                "Attempted to save CPU profile data, but failed because no "
                "CPU profiler was running! Enable CPU profiling by enabling "
                "the RAY_SERVE_ENABLE_CPU_PROFILING env var."
            )

    async def _drain_ongoing_requests(self):
        """Wait for any ongoing requests to finish.

        Sleep for a grace period before the first time we check the number of ongoing
        requests to allow the notification to remove this replica to propagate to
        callers first.
        """
        wait_loop_period_s = self._deployment_config.graceful_shutdown_wait_loop_s
        while True:
            await asyncio.sleep(wait_loop_period_s)

            num_ongoing_requests = (
                self._metrics_manager.get_num_pending_and_running_requests()
            )
            if num_ongoing_requests > 0:
                logger.info(
                    f"Waiting for an additional {wait_loop_period_s}s to shut down "
                    f"because there are {num_ongoing_requests} ongoing requests."
                )
            else:
                logger.info(
                    "Graceful shutdown complete; replica exiting.",
                    extra={"log_to_stderr": False},
                )
                break

    async def perform_graceful_shutdown(self):
        # If the replica was never initialized it never served traffic, so we
        # can skip the wait period.
        if self._user_callable_initialized:
            await self._drain_ongoing_requests()
            await self._user_callable_wrapper.call_destructor()

        self._metrics_manager.shutdown()

    @ray.method(concurrency_group=REPLICA_CONTROL_PLANE_CONCURRENCY_GROUP)
    async def check_health(self):
        await self._user_callable_wrapper.call_user_health_check()


class UserCallableWrapper:
    """Wraps a user-provided callable that is used to handle requests to a replica."""

    def __init__(
        self,
        deployment_def: Callable,
        init_args: Tuple,
        init_kwargs: Dict,
        *,
        deployment_id: DeploymentID,
    ):
        if not (inspect.isfunction(deployment_def) or inspect.isclass(deployment_def)):
            raise TypeError(
                "deployment_def must be a function or class. Instead, its type was "
                f"{type(deployment_def)}."
            )

        self._deployment_def = deployment_def
        self._init_args = init_args
        self._init_kwargs = init_kwargs
        self._is_function = inspect.isfunction(deployment_def)
        self._deployment_id = deployment_id
        self._rwlock = aiorwlock.RWLock()
        self._delete_lock = asyncio.Lock()

        # Will be populated in `initialize_callable`.
        self._callable = None
        self._user_health_check = None

    def _get_user_callable_method(self, method_name: str) -> Callable:
        if self._is_function:
            return self._callable

        if not hasattr(self._callable, method_name):
            # Filter to methods that don't start with '__' prefix.
            def callable_method_filter(attr):
                if attr.startswith("__"):
                    return False
                elif not callable(getattr(self._callable, attr)):
                    return False

                return True

            methods = list(filter(callable_method_filter, dir(self._callable)))
            raise RayServeException(
                f"Tried to call a method '{method_name}' "
                "that does not exist. Available methods: "
                f"{methods}."
            )

        return getattr(self._callable, method_name)

    async def _send_user_result_over_asgi(
        self, result: Any, scope: Scope, receive: Receive, send: Send
    ):
        """Handle the result from user code and send it over the ASGI interface.

        If the result is already a Response type, it is sent directly. Otherwise, it
        is converted to a custom Response type that handles serialization for
        common Python objects.
        """
        if isinstance(result, starlette.responses.Response):
            await result(scope, receive, send)
        else:
            await Response(result).send(scope, receive, send)

    @property
    def user_callable(self) -> Optional[Callable]:
        return self._callable

    async def initialize_callable(self):
        # This closure initializes user code and finalizes replica
        # startup. By splitting the initialization step like this,
        # we can already access this actor before the user code
        # has finished initializing.
        # The supervising state manager can then wait
        # for allocation of this replica by using the `is_allocated`
        # method. After that, it calls `reconfigure` to trigger
        # user code initialization.
        logger.info(
            "Started initializing replica.",
            extra={"log_to_stderr": False},
        )

        if self._is_function:
            self._callable = self._deployment_def
        else:
            # This allows deployments to define an async __init__
            # method (mostly used for testing).
            self._callable = self._deployment_def.__new__(self._deployment_def)
            await sync_to_async(self._callable.__init__)(
                *self._init_args, **self._init_kwargs
            )

            if isinstance(self._callable, ASGIAppReplicaWrapper):
                await self._callable._run_asgi_lifespan_startup()

        user_health_check = getattr(self._callable, HEALTH_CHECK_METHOD, None)
        if not callable(user_health_check):

            def user_health_check():
                pass

        self._user_health_check = sync_to_async(user_health_check)

        logger.info(
            "Finished initializing replica.",
            extra={"log_to_stderr": False},
        )

    async def call_user_health_check(self):
        await self._user_health_check()

    async def call_reconfigure(self, user_config: Any):
        async with self._rwlock.writer:
            if user_config is not None:
                if self._is_function:
                    raise ValueError(
                        "deployment_def must be a class to use user_config"
                    )
                elif not hasattr(self._callable, RECONFIGURE_METHOD):
                    raise RayServeException(
                        "user_config specified but deployment "
                        + self._deployment_id
                        + " missing "
                        + RECONFIGURE_METHOD
                        + " method"
                    )
                reconfigure_method = sync_to_async(
                    getattr(self._callable, RECONFIGURE_METHOD)
                )
                await reconfigure_method(user_config)

    async def call_user_method(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
    ) -> Any:
        """Call a user method that is *not* expected to be a generator.

        Raises any exception raised by the user code so it can be propagated as a
        `RayTaskError`.
        """
        logger.info(
            f"Started executing request {request_metadata.request_id}",
            extra={"log_to_stderr": False, "serve_access_log": True},
        )

        if request_metadata.is_http_request:
            # For HTTP requests we always expect (scope, receive, send) as args.
            assert len(request_args) == 3
            scope, receive, send = request_args

            if isinstance(self._callable, ASGIAppReplicaWrapper):
                request_args = (scope, receive, send)
            else:
                request_args = (Request(scope, receive, send),)
        elif request_metadata.is_grpc_request:
            # Ensure the request args are a single gRPCRequest object.
            assert len(request_args) == 1 and isinstance(request_args[0], gRPCRequest)
            request_args = (pickle.loads(request_args[0].grpc_user_request),)

        runner_method = None
        try:
            runner_method = self._get_user_callable_method(request_metadata.call_method)
            if inspect.isgeneratorfunction(runner_method) or inspect.isasyncgenfunction(
                runner_method
            ):
                raise TypeError(
                    f"Method '{runner_method.__name__}' is a generator function. "
                    "You must use `handle.options(stream=True)` to call "
                    "generators on a deployment."
                )

            method_to_call = sync_to_async(runner_method)

            # Edge case to support empty HTTP handlers: don't pass the Request
            # argument if the callable has no parameters.
            params = inspect.signature(runner_method).parameters
            if request_metadata.is_http_request and len(params) == 0:
                request_args, request_kwargs = tuple(), {}
            elif request_metadata.is_grpc_request and GRPC_CONTEXT_ARG_NAME in params:
                request_kwargs = {GRPC_CONTEXT_ARG_NAME: request_metadata.grpc_context}

            result = await method_to_call(*request_args, **request_kwargs)
            if inspect.isgenerator(result) or inspect.isasyncgen(result):
                raise TypeError(
                    f"Method '{runner_method.__name__}' returned a generator. You "
                    "must use `handle.options(stream=True)` to call "
                    "generators on a deployment."
                )

        except Exception as e:
            function_name = "unknown"
            if runner_method is not None:
                function_name = runner_method.__name__
            e = wrap_to_ray_error(function_name, e)
            if request_metadata.is_http_request:
                result = starlette.responses.Response(
                    f"Unexpected error, traceback: {e}.", status_code=500
                )
                await self._send_user_result_over_asgi(result, scope, receive, send)

            raise e from None

        if request_metadata.is_http_request and not isinstance(
            self._callable, ASGIAppReplicaWrapper
        ):
            # For the FastAPI codepath, the response has already been sent over the
            # ASGI interface, but for the vanilla deployment codepath we need to
            # send it.
            await self._send_user_result_over_asgi(result, scope, receive, send)
        elif request_metadata.is_grpc_request:
            result = (request_metadata.grpc_context, result.SerializeToString())

        return result

    async def call_user_method_generator(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        """Call a user method that is expected to be a generator.

        Raises any exception raised by the user code so it can be propagated as a
        `RayTaskError`.
        """
        assert (
            not request_metadata.is_http_request
        ), "HTTP requests should go through `call_user_method`."

        logger.info(
            f"Started executing request {request_metadata.request_id}",
            extra={"log_to_stderr": False, "serve_access_log": True},
        )

        user_method = self._get_user_callable_method(request_metadata.call_method)
        if request_metadata.is_grpc_request:
            assert len(request_args) == 1 and isinstance(request_args[0], gRPCRequest)
            request_args = (pickle.loads(request_args[0].grpc_user_request),)
            if GRPC_CONTEXT_ARG_NAME in inspect.signature(user_method).parameters:
                request_kwargs = {GRPC_CONTEXT_ARG_NAME: request_metadata.grpc_context}

        result_generator = user_method(*request_args, **request_kwargs)
        if inspect.iscoroutine(result_generator):
            result_generator = await result_generator

        if inspect.isgenerator(result_generator):
            for result in result_generator:
                if request_metadata.is_grpc_request:
                    yield request_metadata.grpc_context, result.SerializeToString()
                else:
                    yield result
        elif inspect.isasyncgen(result_generator):
            async for result in result_generator:
                if request_metadata.is_grpc_request:
                    yield request_metadata.grpc_context, result.SerializeToString()
                else:
                    yield result
        else:
            raise TypeError(
                "When using `stream=True`, the called method must be a generator "
                f"function, but '{user_method.__name__}' is not."
            )

    async def call_destructor(self):
        """Explicitly call the `__del__` method of the user callable.

        We set the del method to noop after successfully calling it so the
        destructor is called only once.
        """
        async with self._delete_lock:
            if not hasattr(self, "_callable"):
                return

            try:
                if hasattr(self._callable, "__del__"):
                    # Make sure to accept `async def __del__(self)` as well.
                    await sync_to_async(self._callable.__del__)()
                    setattr(self._callable, "__del__", lambda _: None)

                if hasattr(self._callable, "__serve_multiplex_wrapper"):
                    await getattr(
                        self._callable, "__serve_multiplex_wrapper"
                    ).shutdown()

            except Exception as e:
                logger.exception(f"Exception during graceful shutdown of replica: {e}")
            finally:
                if hasattr(self._callable, "__del__"):
                    del self._callable
