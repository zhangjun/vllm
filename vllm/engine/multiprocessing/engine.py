# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pickle
import signal
from contextlib import contextmanager
from typing import Iterator, List, Optional, Union, Dict

import cloudpickle
import time
import zmq
import msgspec
from vllm import AsyncEngineArgs, SamplingParams
from vllm.config import VllmConfig
from vllm.engine.llm_engine import LLMEngine
# yapf conflicts with isort for this block
# yapf: disable
from vllm.engine.multiprocessing import (ENGINE_DEAD_ERROR, IPC_DATA_EXT,
                                         IPC_HEALTH_EXT, IPC_INPUT_EXT,
                                         REQUEST_OUTPUTS_T,
                                         VLLM_RPC_SUCCESS_STR, IPC_REMOTE_PREFILL_REQUEST_EXT,
                                         RPCAbortRequest,
                                         IPC_OUTPUT_EXT, IPC_METRICS_EXT,
                                         RPCAdapterLoadedResponse, RPCError,
                                         RPCIsSleepingRequest,
                                         RPCIsSleepingResponse,
                                         RPCLoadAdapterRequest,
                                         RPCProcessRequest,
                                         RPCResetPrefixCacheRequest,
                                         RPCSleepRequest, RPCStartupRequest,
                                         RPCStartupResponse,
                                         RPCUProfileRequest, RPCWakeUpRequest, KvMetrics,
                                         IPC_REMOTE_NIXL_METADATA_EXT)
# yapf: enable
from vllm.logger import init_logger
from vllm.outputs import RequestOutput
from vllm.transformers_utils.config import (
    maybe_register_config_serialize_by_value)
from vllm.usage.usage_lib import UsageContext
from vllm.remote_prefill import RemotePrefillRequest
from vllm.distributed.device_communicators.nixl import NixlMetadata

from vllm.engine.metrics_types import StatLoggerBase, Stats, SupportsMetricsInfo
from dataclasses import dataclass, field
from vllm.worker.model_runner_base import InputProcessingError

logger = init_logger(__name__)

POLLING_TIMEOUT_MS = 10000
HEALTHY_RESPONSE = (pickle.dumps(VLLM_RPC_SUCCESS_STR), )

class KvStatLogger(StatLoggerBase):
    def __init__(
        self,
        max_num_seqs: int,
        num_total_gpu_blocks: int,
        metrics_socket
    ):
        # Must query initialized scheduler for max infos
        self.request_total_slots = max_num_seqs
        self.kv_total_blocks = num_total_gpu_blocks
        self.metrics_socket = metrics_socket

        # KV metrics
        self._send_kv_metrics(0, 0, 0, 0.0, 0.0)

    def log(self, stats: Stats) -> None:
        self._send_kv_metrics(
            stats.num_running_sys,
            int(stats.gpu_cache_usage_sys * self.kv_total_blocks),
            stats.num_waiting_sys,
            stats.gpu_cache_usage_sys,
            stats.gpu_prefix_cache_hit_rate
        )

    def info(self, type: str, obj: SupportsMetricsInfo) -> None:
        pass

    def _send_kv_metrics(
        self,
        active_slots,
        active_kv_blocks,
        num_requests_waiting,
        gpu_cache_usage_perc,
        gpu_prefix_cache_hit_rate,
    ):
        if not self.metrics_socket.closed:
            metrics_bytes = pickle.dumps(
                KvMetrics(
                    active_slots,
                    self.request_total_slots,
                    active_kv_blocks,
                    self.kv_total_blocks,
                    num_requests_waiting,
                    gpu_cache_usage_perc,
                    gpu_prefix_cache_hit_rate,
                )
            )
            self.metrics_socket.send_multipart((metrics_bytes, ), copy=False)

# TODO: Send entire stats object to the client
# class StatLogger(StatLoggerBase):
#     def __init__(
#         self,
#         metrics_socket
#     ):
#         self.metrics_socket = metrics_socket

#     def log(self, stats: Stats) -> None:
#         self._send_metrics(stats)

#     def info(self, type: str, obj: SupportsMetricsInfo) -> None:
#         pass

#     def _send_metrics(self, stats: Stats):
#         if not self.metrics_socket.closed:
#             metrics_bytes = pickle.dumps(stats)
#             self.metrics_socket.send_multipart((metrics_bytes, ), copy=False)





class MQLLMEngine:
    """A multiprocessing wrapper for :class:`LLMEngine`.

    This class is used to wrap the :class:`LLMEngine` class to enable use
    in concurrnet manner. It runs a background loop and uses zeromq to
    receive new requests and stream outputs incrementally via ipc.

    The :class:`LLMEngine` generate or encode process is kicked off when a new
    RPCProcessRequest is received by the input_socket.

    The self.engine_loop checks the input_socket for new requests,
    adds them to the LLMEngine if there are any, calls the internal
    :class:`LLMEngine.step()`, and sends the RequestOutputs back over
    the output_socket.

    If use_async_sockets is set, the logic associated with reading new
    requests from the socket and sending data to the socket is passed
    as a callback to the llm_engine, which calls the logic asynchronously
    such that the IPC can be overlapped with the GPU.

    Args:
        ipc_path: Base path for zeromq interprocess messaging
        use_async_sockets: Whether to make send/recv async with GPU
        log_requests: Whether to log the requests.
        *args: Arguments for :class:`LLMEngine`.
        **kwargs: Arguments for :class:`LLMEngine`.
    """

    def __init__(self,
                 ipc_path: str,
                 use_async_sockets: bool,
                 *args,
                 log_requests: bool = True,
                 **kwargs) -> None:
        # For MQLLMEngine, we can use cached outputs, since each new request
        # output is immediately pickled and send over the socket, which frees
        # the python object to be reused again.
        kwargs['use_cached_outputs'] = True

        self.engine = LLMEngine(*args, **kwargs)
        self.log_requests = log_requests

        self.use_async_sockets = use_async_sockets
        if self.use_async_sockets:
            self.engine.process_request_outputs_callback = \
                self._async_socket_engine_callback

        self.ctx = zmq.Context()  # type: ignore[attr-defined]

        # Receive input from the client.
        self.input_socket = self.ctx.socket(zmq.constants.PULL)
        self.input_socket.bind(f"{ipc_path}{IPC_INPUT_EXT}")

        # Send output stream back to client.
        self.output_socket = self.ctx.socket(zmq.constants.PUSH)
        self.output_socket.bind(f"{ipc_path}{IPC_OUTPUT_EXT}")

        # Send heartbeats back to client.
        self.heartbeat_socket = self.ctx.socket(zmq.constants.PUSH)
        self.heartbeat_socket.bind(f"{ipc_path}{IPC_HEALTH_EXT}")

        # Send metrics back to client.
        self.metrics_socket = self.ctx.socket(zmq.constants.PUSH)
        self.metrics_socket.bind(f"{ipc_path}{IPC_METRICS_EXT}")

        # IPC path for the data socket.
        self.data_ipc_path = f"{ipc_path}{IPC_DATA_EXT}"

        # Error state.
        self._errored_with: Optional[BaseException] = None

        self.remote_prefill_request_socket = self.ctx.socket(zmq.constants.PUSH)
        self.remote_nixl_metadata_socket = self.ctx.socket(zmq.constants.PULL)
        if self.engine.is_nixl_initialized:
            self.remote_prefill_request_socket.bind(f"{ipc_path}{IPC_REMOTE_PREFILL_REQUEST_EXT}")
            self.remote_nixl_metadata_socket.bind(f"{ipc_path}{IPC_REMOTE_NIXL_METADATA_EXT}")


        # Attach logger for continuous metrics publishing
        self.kv_stat_logger = KvStatLogger(
            self.engine.scheduler_config.max_num_seqs,
            self.engine.cache_config.num_gpu_blocks,
            self.metrics_socket
        )
        self.engine.add_logger("kv_metrics", self.kv_stat_logger)
        
        # TODO investigate sending whole stats object
        # self.general_stat_logger = StatLogger(
        #     self.metrics_socket
        # )
        # self.engine.add_logger("general_metrics", self.general_stat_logger)

    @property
    def dead_error(self) -> BaseException:
        if self._errored_with is not None:
            return ENGINE_DEAD_ERROR(self._errored_with)
        else:
            return ENGINE_DEAD_ERROR()

    @classmethod
    def from_vllm_config(cls, vllm_config: VllmConfig,
                         usage_context: UsageContext,
                         disable_log_requests: bool, disable_log_stats: bool,
                         ipc_path: str) -> "MQLLMEngine":
        # Setup plugins for each process
        from vllm.plugins import load_general_plugins
        load_general_plugins()

        use_async_sockets = vllm_config.model_config.use_async_output_proc

        return cls(
            vllm_config=vllm_config,
            executor_class=LLMEngine._get_executor_cls(vllm_config),
            ipc_path=ipc_path,
            usage_context=usage_context,
            use_async_sockets=use_async_sockets,
            log_requests=(not disable_log_requests),
            log_stats=(not disable_log_stats),
        )

    @staticmethod
    def from_engine_args(engine_args: AsyncEngineArgs,
                         usage_context: UsageContext, ipc_path: str):
        """Creates an MQLLMEngine from the engine arguments."""

        vllm_config = engine_args.create_engine_config(usage_context)
        return MQLLMEngine.from_vllm_config(
            ipc_path=ipc_path,
            vllm_config=vllm_config,
            usage_context=usage_context,
            disable_log_requests=engine_args.disable_log_requests,
            disable_log_stats=engine_args.disable_log_stats,
        )

    def start(self):
        try:
            try:
                logger.debug("Starting Startup Loop.")
                self.run_startup_loop()
                logger.debug("Starting Engine Loop.")
                self.run_engine_loop()
            except Exception as e:
                logger.exception(repr(e))
        except KeyboardInterrupt:
            logger.debug("Shutting down MQLLMEngine.")
        finally:
            logger.debug("MQLLMEngine is shut down.")
            self.cleanup()

    def cleanup(self):
        """Cleanup zeromq state on shutdown."""
        # Closes all sockets and destroys context.
        self.ctx.destroy(linger=0)
        del self.engine

    @contextmanager
    def make_data_socket(
            self) -> Iterator[zmq.Socket]:  # type: ignore[name-defined]
        socket = self.ctx.socket(zmq.constants.ROUTER)
        try:
            socket.bind(self.data_ipc_path)
            yield socket
        finally:
            socket.close(linger=0)

    def run_startup_loop(self) -> None:
        """Startup loop for sending data from Engine -> Client."""

        with self.make_data_socket() as socket:
            response: Union[RPCStartupResponse, BaseException]
            try:
                identity, message = socket.recv_multipart(copy=False)
                request: RPCStartupRequest = pickle.loads(message.buffer)

                # Handle the query from the Client.
                if request == RPCStartupRequest.IS_SERVER_READY:
                    tracing_enabled = self.engine.is_tracing_enabled()
            
                    # Send nixl metadata to the client
                    if self.engine.is_nixl_initialized:
                        nixl_metadata = self.engine.get_nixl_metadata()
                        encoded_nixl_metadata = msgspec.msgpack.encode(nixl_metadata)
                        response = RPCStartupResponse(
                            tracing_enabled=tracing_enabled,
                            nixl_metadata=encoded_nixl_metadata)
                    else:
                        response = RPCStartupResponse(
                            tracing_enabled=tracing_enabled)

            except Exception as e:
                response = e

            socket.send_multipart((identity, pickle.dumps(response)),
                                  copy=False)

    def run_engine_loop(self):
        """Core busy loop of the LLMEngine."""

        while True:
            if not self.engine.has_unfinished_requests():
                logger.debug("No unfinished requests")
                # Poll until there is work to do.
                while self.input_socket.poll(timeout=POLLING_TIMEOUT_MS) == 0:
                    # When there's no work, check on engine health and send
                    # health status back to client
                    self._health_check()
                    self.engine.do_log_stats()
                    logger.debug("Waiting for new requests in engine loop.")

            # Handle any input from the client.
            self.handle_new_input()

            # Engine step.
            request_outputs = self.engine_step()

            # Send request outputs (if async, done in engine_step callback).
            if not self.use_async_sockets:
                self._send_outputs(request_outputs)

    def engine_step(self) -> List[RequestOutput]:
        """Engine step wrapper with error handling."""
        try:
            return self.engine.step()
        except SystemExit:
            raise
        except InputProcessingError as e:
            # Special case where we handle an error preparing the inputs for
            # a single request in the batch
            rpc_err = RPCError(request_id=e.request_id,
                               is_engine_errored=False,
                               exception=e.__cause__)
            self._send_outputs(rpc_err)
            return []
        except BaseException as e:
            self._set_errored(e)
            rpc_err = RPCError(request_id=None,
                               is_engine_errored=True,
                               exception=e)
            self._send_outputs(rpc_err)
            raise e

    def handle_new_input(self):
        """Handle new input from the socket"""
        try:
            if self.engine.is_nixl_initialized:
                while self.remote_nixl_metadata_socket.poll(timeout=0) != 0:
                    frames = self.remote_nixl_metadata_socket.recv(copy=False)
                    nixl_metadata = msgspec.msgpack.decode(frames.buffer, type=NixlMetadata)
                    logger.debug("Adding remote nixl metadata for engine: %s", nixl_metadata.engine_id)
                    self.engine.add_remote_nixl_metadata(nixl_metadata)

            while self.input_socket.poll(timeout=0) != 0:
                frames = self.input_socket.recv_multipart(copy=False)
                request = pickle.loads(frames[0].buffer)

                if isinstance(request, RPCProcessRequest):
                    if len(frames) > 1:
                        # Use cloudpickle for logits processors
                        assert isinstance(request.params, SamplingParams)
                        lprocs = cloudpickle.loads(frames[1].buffer)
                        request.params.logits_processors = lprocs
                    self._handle_process_request(request)
                elif isinstance(request, RPCAbortRequest):
                    self._handle_abort_request(request)
                elif isinstance(request, RPCUProfileRequest):
                    if request == RPCUProfileRequest.START_PROFILE:
                        self.start_profile()
                    else:
                        self.stop_profile()
                elif isinstance(request, RPCLoadAdapterRequest):
                    self._handle_load_adapter_request(request)
                elif isinstance(request, RPCResetPrefixCacheRequest):
                    self.reset_prefix_cache()
                elif isinstance(request, RPCSleepRequest):
                    self.sleep(request.value)
                elif isinstance(request, RPCWakeUpRequest):
                    self.wake_up(request.tags)
                elif isinstance(request, RPCIsSleepingRequest):
                    self._handle_is_sleeping_request(request)
                else:
                    raise ValueError("Unknown RPCRequest Type: "
                                     f"{type(request)}")

        except Exception as e:
            self._set_errored(e)
            self._send_unhealthy(e)
            raise e

    def _handle_process_request(self, request: RPCProcessRequest):
        """Handle RPCProcessRequest by adding it to the LLMEngine."""
        request_id = request.request_id

        if self._errored_with is not None:
            rpc_err = RPCError(request_id=request_id,
                               is_engine_errored=True,
                               exception=ENGINE_DEAD_ERROR(self._errored_with))
            self._send_outputs(rpc_err)

        try:
            if request.remote_prefill_params is not None and request.remote_prefill_params.is_remote_prefill:
                def remote_prefill_request_callback(request: RemotePrefillRequest):
                    logger.debug("Sending remote prefill request: %s", request.request_id)
                    self.remote_prefill_request_socket.send(msgspec.msgpack.encode(request), copy=False)
                request.remote_prefill_params.remote_prefill_request_callback = remote_prefill_request_callback
            self.engine.add_request(
                request_id=request_id,
                prompt=request.prompt,
                params=request.params,
                lora_request=request.lora_request,
                trace_headers=request.trace_headers,
                prompt_adapter_request=request.prompt_adapter_request,
                priority=request.priority,
                remote_prefill_params=request.remote_prefill_params,
            )

            if self.log_requests:
                logger.info("Added request %s.", request.request_id)

        except Exception as e:
            # We do not set self._errored = True here, since the error
            # is due to an issue adding this request to the engine,
            # rather than an issue with the engine itself.
            logger.debug("Failed to add request %s to engine. %s",
                         request.request_id, e)
            is_errored = self._errored_with is not None
            rpc_err = RPCError(request_id=request_id,
                               is_engine_errored=is_errored,
                               exception=e)
            self._send_outputs(rpc_err)

            # Remove request from the engine.
            self.engine.abort_request(request_id)

    def _handle_abort_request(self, request: RPCAbortRequest):
        self.engine.abort_request(request.request_id)
        if self.log_requests:
            logger.info("Aborted request %s.", request.request_id)

    def _handle_load_adapter_request(self, request: RPCLoadAdapterRequest):
        try:
            self.engine.add_lora(request.lora_request)
        except BaseException as e:
            # Send back an error if the adater fails to load
            rpc_err = RPCError(request_id=request.request_id,
                               is_engine_errored=False,
                               exception=e)
            self._send_outputs(rpc_err)
            return
        # Otherwise, send back the successful load message
        self._send_outputs(
            RPCAdapterLoadedResponse(request_id=request.request_id))

    def _handle_is_sleeping_request(self, request: RPCIsSleepingRequest):
        is_sleeping = self.is_sleeping()
        self._send_outputs(
            RPCIsSleepingResponse(request_id=request.request_id,
                                  is_sleeping=is_sleeping))

    def _health_check(self):
        # Send unhealthy if engine has already errored
        if self._errored_with is not None:
            self._send_unhealthy(self._errored_with)
        try:
            self.engine.check_health()
            self._send_healthy()
        except Exception as e:
            self._set_errored(e)
            self._send_unhealthy(e)

    def _send_outputs(self, outputs: REQUEST_OUTPUTS_T):
        """Send outputs back to the engine client. These can be:
        - Exceptions
        - A list of generation outputs
        - A response from loading a lora adapter
        """
        if outputs:
            try:
                from ray.exceptions import RayTaskError

                # RayTaskError might not pickelable here. We need to unpack the
                # underlying exception as the real exception in the output.
                if (isinstance(outputs, RPCError)
                        and isinstance(outputs.exception, RayTaskError)):
                    outputs.exception = outputs.exception.cause
            except ImportError:
                pass

            output_bytes = pickle.dumps(outputs)
            self.output_socket.send_multipart((output_bytes, ), copy=False)

    def _send_healthy(self):
        """Send HEALTHY message to RPCClient."""
        if not self.heartbeat_socket.closed:
            self.heartbeat_socket.send_multipart(HEALTHY_RESPONSE, copy=False)

    def _send_unhealthy(self, error: BaseException):
        """Send UNHEALTHY message to RPCClient."""
        if not self.heartbeat_socket.closed:
            error_bytes = pickle.dumps(error)
            self.heartbeat_socket.send_multipart((error_bytes, ), copy=False)

    def _async_socket_engine_callback(self,
                                      request_outputs: REQUEST_OUTPUTS_T):
        """Callback used by engine to make socket handling async with GPU."""
        self._send_outputs(request_outputs)
        self.handle_new_input()

    def _set_errored(self, e: BaseException):
        """Log and set errored status if this is the first issue."""
        if self._errored_with is None:
            self._errored_with = e

    def start_profile(self) -> None:
        self.engine.start_profile()

    def stop_profile(self) -> None:
        self.engine.stop_profile()

    def reset_prefix_cache(self) -> bool:
        return self.engine.reset_prefix_cache()

    def sleep(self, level: int = 1) -> None:
        self.engine.sleep(level)

    def wake_up(self, tags: Optional[list[str]] = None) -> None:
        self.engine.wake_up(tags)

    def is_sleeping(self) -> bool:
        return self.engine.is_sleeping()


def signal_handler(*_) -> None:
    raise KeyboardInterrupt("MQLLMEngine terminated")


def run_mp_engine(vllm_config: VllmConfig, usage_context: UsageContext,
                  ipc_path: str, disable_log_stats: bool,
                  disable_log_requests: bool, engine_alive):
    try:
        # Ensure we can serialize transformer config before spawning
        maybe_register_config_serialize_by_value()

        engine = MQLLMEngine.from_vllm_config(
            vllm_config=vllm_config,
            usage_context=usage_context,
            disable_log_stats=disable_log_stats,
            disable_log_requests=disable_log_requests,
            ipc_path=ipc_path)

        signal.signal(signal.SIGTERM, signal_handler)

        engine.start()

    except BaseException as e:
        logger.exception(e)
        engine_alive.value = False
        raise e
