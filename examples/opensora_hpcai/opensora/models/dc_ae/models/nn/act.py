# Copyright 2024 MIT Han Lab
#
# This code is adapted from https://github.com/hpcaitech/Open-Sora
# with modifications run Open-Sora on mindspore.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

from typing import Optional

from mindspore import mint, nn

from ..nn.vo_ops import build_kwargs_from_config

__all__ = ["build_act"]


# register activation function here
REGISTERED_ACT_DICT: dict[str, type] = {
    "relu": mint.nn.ReLU,
    "relu6": mint.nn.ReLU6,
    "hswish": mint.nn.Hardswish,
    "silu": mint.nn.SiLU,
    "gelu": nn.GELU,
}


def build_act(name: str, **kwargs) -> Optional[nn.Cell]:
    if name in REGISTERED_ACT_DICT:
        act_cls = REGISTERED_ACT_DICT[name]
        args = build_kwargs_from_config(kwargs, act_cls)
        return act_cls(**args)
    else:
        return None
