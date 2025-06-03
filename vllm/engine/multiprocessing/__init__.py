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

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Mapping, Optional, Union, overload

from typing_extensions import deprecated

from vllm import PoolingParams
from vllm.distributed.device_communicators.nixl import NixlMetadata
from vllm.inputs import PromptType
from vllm.lora.request import LoRARequest
from vllm.outputs import RequestOutput
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.remote_prefill import RemotePrefillParams
from vllm.sampling_params import SamplingParams
from vllm.utils import Device, deprecate_kwargs

VLLM_RPC_SUCCESS_STR = "SUCCESS"

IPC_INPUT_EXT = "_input_socket"
IPC_OUTPUT_EXT = "_output_socket"
IPC_HEALTH_EXT = "_health_socket"
IPC_DATA_EXT = "_data_socket"
IPC_REMOTE_PREFILL_REQUEST_EXT = "_remote_prefill_request_socket"
IPC_REMOTE_NIXL_METADATA_EXT = "_remote_nixl_metadata_socket"
IPC_METRICS_EXT = "_metrics_socket"


class MQEngineDeadError(RuntimeError):
    pass


@dataclass
class RPCProcessRequest:
    prompt: PromptType
    params: Union[SamplingParams, PoolingParams]
    request_id: str
    lora_request: Optional[LoRARequest] = None
    trace_headers: Optional[Mapping[str, str]] = None
    prompt_adapter_request: Optional[PromptAdapterRequest] = None
    priority: int = 0
    remote_prefill_params: Optional[RemotePrefillParams] = None

    @overload
    def __init__(
        self,
        prompt: PromptType,
        params: Union[SamplingParams, PoolingParams],
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
        priority: int = 0,
    ) -> None:
        ...

    @overload
    @deprecated("'inputs' will be renamed to 'prompt")
    def __init__(
        self,
        *,
        inputs: PromptType,
        params: Union[SamplingParams, PoolingParams],
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
        priority: int = 0,
    ) -> None:
        ...

    @deprecate_kwargs(
        "inputs",
        additional_message="Please use the 'prompt' parameter instead.",
    )
    def __init__(
            self,
            prompt: Optional[PromptType] = None,
            params: Optional[Union[SamplingParams, PoolingParams]] = None,
            request_id: Optional[str] = None,
            lora_request: Optional[LoRARequest] = None,
            trace_headers: Optional[Mapping[str, str]] = None,
            prompt_adapter_request: Optional[PromptAdapterRequest] = None,
            priority: int = 0,
            remote_prefill_params: Optional[RemotePrefillParams] = None,
            *,
            inputs: Optional[PromptType] = None,  # DEPRECATED
    ) -> None:
        if inputs is not None:
            prompt = inputs
        assert (prompt is not None and params is not None
                and request_id is not None)

        super().__init__()

        self.prompt = prompt
        self.params = params
        self.request_id = request_id
        self.lora_request = lora_request
        self.trace_headers = trace_headers
        self.prompt_adapter_request = prompt_adapter_request
        self.priority = priority
        self.remote_prefill_params = remote_prefill_params


@dataclass
class RPCError:
    request_id: Optional[str]
    is_engine_errored: bool
    exception: BaseException


@dataclass
class RPCAbortRequest:
    request_id: str


class RPCStartupRequest(Enum):
    IS_SERVER_READY = 1


@dataclass
class RPCStartupResponse:
    tracing_enabled: bool
    nixl_metadata: Optional[bytes] = None


class RPCUProfileRequest(Enum):
    START_PROFILE = 1
    STOP_PROFILE = 2


class RPCResetMultiModalCacheRequest(Enum):
    RESET = 1


@dataclass
class RPCResetPrefixCacheRequest:
    device: Device


class RPCSleepRequest(Enum):
    SLEEP_LEVEL_1 = 1
    SLEEP_LEVEL_2 = 2


@dataclass
class RPCWakeUpRequest:
    tags: Optional[list[str]] = None


@dataclass
class RPCIsSleepingRequest:
    # Set the default value of request_id to a new UUID
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class RPCIsSleepingResponse:
    request_id: str
    is_sleeping: bool


@dataclass
class RPCLoadAdapterRequest:
    lora_request: LoRARequest
    # Set the default value of request_id to a new UUID
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class RPCAdapterLoadedResponse:
    request_id: str


RPC_REQUEST_T = Union[RPCProcessRequest, RPCAbortRequest, RPCStartupRequest,
                      RPCUProfileRequest, RPCLoadAdapterRequest,
                      RPCResetMultiModalCacheRequest,
                      RPCResetPrefixCacheRequest, RPCSleepRequest,
                      RPCWakeUpRequest, RPCIsSleepingRequest]

REQUEST_OUTPUTS_T = Union[List[RequestOutput], RPCAdapterLoadedResponse,
                          RPCIsSleepingResponse, RPCError]


def ENGINE_DEAD_ERROR(
        error: Optional[BaseException] = None) -> MQEngineDeadError:
    if error is None:
        return MQEngineDeadError(
            "Engine loop is not running. Inspect the stacktrace to "
            "find the original error")

    return MQEngineDeadError(
        "Engine loop is not running. Inspect the stacktrace to "
        f"find the original error: {repr(error)}.")


@dataclass
class KvMetrics:
    request_active_slots: int
    request_total_slots: int
    kv_active_blocks: int
    kv_total_blocks: int
    num_requests_waiting: int
    gpu_cache_usage_perc: float
    gpu_prefix_cache_hit_rate: float
