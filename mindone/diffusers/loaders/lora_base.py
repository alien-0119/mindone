# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# This code is adapted from https://github.com/huggingface/diffusers
# with modifications to run diffusers on mindspore.
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

import copy
import inspect
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import safetensors
from huggingface_hub import model_info
from huggingface_hub.constants import HF_HUB_OFFLINE

import mindspore as ms
from mindspore import nn

from mindone.safetensors.mindspore import load_file, save_file
from mindone.transformers import MSPreTrainedModel

from .._peft.tuners.tuners_utils import BaseTunerLayer
from ..models.lora import text_encoder_attn_modules, text_encoder_mlp_modules
from ..models.modeling_utils import ModelMixin
from ..utils import (
    _get_model_file,
    convert_state_dict_to_diffusers,
    convert_state_dict_to_peft,
    delete_adapter_layers,
    deprecate,
    get_adapter_name,
    get_peft_kwargs,
    is_peft_version,
    logging,
    recurse_remove_peft_layers,
    scale_lora_layers,
    set_adapter_layers,
    set_weights_and_activate_adapters,
)

logger = logging.get_logger(__name__)

LORA_WEIGHT_NAME = "pytorch_lora_weights.bin"
LORA_WEIGHT_NAME_SAFE = "pytorch_lora_weights.safetensors"


def fuse_text_encoder_lora(text_encoder, lora_scale=1.0, safe_fusing=False, adapter_names=None):
    """
    Fuses LoRAs for the text encoder.

    Args:
        text_encoder (`nn.Cell`):
            The text encoder module to set the adapter layers for. If `None`, it will try to get the `text_encoder`
            attribute.
        lora_scale (`float`, defaults to 1.0):
            Controls how much to influence the outputs with the LoRA parameters.
        safe_fusing (`bool`, defaults to `False`):
            Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
        adapter_names (`List[str]` or `str`):
            The names of the adapters to use.
    """
    merge_kwargs = {"safe_merge": safe_fusing}

    for _, module in text_encoder.cells_and_names():
        if isinstance(module, BaseTunerLayer):
            if lora_scale != 1.0:
                module.scale_layer(lora_scale)

            # For BC with previous PEFT versions, we need to check the signature
            # of the `merge` method to see if it supports the `adapter_names` argument.
            supported_merge_kwargs = list(inspect.signature(module.merge).parameters)
            if "adapter_names" in supported_merge_kwargs:
                merge_kwargs["adapter_names"] = adapter_names
            elif "adapter_names" not in supported_merge_kwargs and adapter_names is not None:
                raise ValueError(
                    "The `adapter_names` argument is not supported with your PEFT version. "
                    "Please upgrade to the latest version of PEFT. `pip install -U peft`"
                )

            module.merge(**merge_kwargs)


def unfuse_text_encoder_lora(text_encoder):
    """
    Unfuses LoRAs for the text encoder.

    Args:
        text_encoder (`nn.Cell`):
            The text encoder module to set the adapter layers for. If `None`, it will try to get the `text_encoder`
            attribute.
    """
    for _, module in text_encoder.cells_and_names():
        if isinstance(module, BaseTunerLayer):
            module.unmerge()


def set_adapters_for_text_encoder(
    adapter_names: Union[List[str], str],
    text_encoder: Optional["MSPreTrainedModel"] = None,  # noqa: F821
    text_encoder_weights: Optional[Union[float, List[float], List[None]]] = None,
):
    """
    Sets the adapter layers for the text encoder.

    Args:
        adapter_names (`List[str]` or `str`):
            The names of the adapters to use.
        text_encoder (`nn.Cell`, *optional*):
            The text encoder module to set the adapter layers for. If `None`, it will try to get the `text_encoder`
            attribute.
        text_encoder_weights (`List[float]`, *optional*):
            The weights to use for the text encoder. If `None`, the weights are set to `1.0` for all the adapters.
    """
    if text_encoder is None:
        raise ValueError(
            "The pipeline does not have a default `pipe.text_encoder` class. Please make sure to pass a `text_encoder` instead."
        )

    def process_weights(adapter_names, weights):
        # Expand weights into a list, one entry per adapter
        # e.g. for 2 adapters:  7 -> [7,7] ; [3, None] -> [3, None]
        if not isinstance(weights, list):
            weights = [weights] * len(adapter_names)

        if len(adapter_names) != len(weights):
            raise ValueError(
                f"Length of adapter names {len(adapter_names)} is not equal to the length of the weights {len(weights)}"
            )

        # Set None values to default of 1.0
        # e.g. [7,7] -> [7,7] ; [3, None] -> [3,1]
        weights = [w if w is not None else 1.0 for w in weights]

        return weights

    adapter_names = [adapter_names] if isinstance(adapter_names, str) else adapter_names
    text_encoder_weights = process_weights(adapter_names, text_encoder_weights)
    set_weights_and_activate_adapters(text_encoder, adapter_names, text_encoder_weights)


def disable_lora_for_text_encoder(text_encoder: Optional["MSPreTrainedModel"] = None):
    """
    Disables the LoRA layers for the text encoder.

    Args:
        text_encoder (`nn.Cell`, *optional*):
            The text encoder module to disable the LoRA layers for. If `None`, it will try to get the `text_encoder`
            attribute.
    """
    if text_encoder is None:
        raise ValueError("Text Encoder not found.")
    set_adapter_layers(text_encoder, enabled=False)


def enable_lora_for_text_encoder(text_encoder: Optional["MSPreTrainedModel"] = None):
    """
    Enables the LoRA layers for the text encoder.

    Args:
        text_encoder (`nn.Cell`, *optional*):
            The text encoder module to enable the LoRA layers for. If `None`, it will try to get the `text_encoder`
            attribute.
    """
    if text_encoder is None:
        raise ValueError("Text Encoder not found.")
    set_adapter_layers(text_encoder, enabled=True)


def _remove_text_encoder_monkey_patch(text_encoder):
    recurse_remove_peft_layers(text_encoder)
    if getattr(text_encoder, "peft_config", None) is not None:
        del text_encoder.peft_config
        text_encoder._hf_peft_config_loaded = None


def _fetch_state_dict(
    pretrained_model_name_or_path_or_dict,
    weight_name,
    use_safetensors,
    local_files_only,
    cache_dir,
    force_download,
    proxies,
    token,
    revision,
    subfolder,
    user_agent,
    allow_pickle,
):
    model_file = None
    if not isinstance(pretrained_model_name_or_path_or_dict, dict):
        # Let's first try to load .safetensors weights
        if (use_safetensors and weight_name is None) or (
            weight_name is not None and weight_name.endswith(".safetensors")
        ):
            try:
                # Here we're relaxing the loading check to enable more Inference API
                # friendliness where sometimes, it's not at all possible to automatically
                # determine `weight_name`.
                if weight_name is None:
                    weight_name = _best_guess_weight_name(
                        pretrained_model_name_or_path_or_dict,
                        file_extension=".safetensors",
                        local_files_only=local_files_only,
                    )
                model_file = _get_model_file(
                    pretrained_model_name_or_path_or_dict,
                    weights_name=weight_name or LORA_WEIGHT_NAME_SAFE,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    proxies=proxies,
                    local_files_only=local_files_only,
                    token=token,
                    revision=revision,
                    subfolder=subfolder,
                    user_agent=user_agent,
                )
                state_dict = load_file(model_file)
            except (IOError, safetensors.SafetensorError) as e:
                if not allow_pickle:
                    raise e
                # try loading non-safetensors weights
                model_file = None
                pass

        if model_file is None:
            if weight_name is None:
                weight_name = _best_guess_weight_name(
                    pretrained_model_name_or_path_or_dict, file_extension=".bin", local_files_only=local_files_only
                )
            model_file = _get_model_file(
                pretrained_model_name_or_path_or_dict,
                weights_name=weight_name or LORA_WEIGHT_NAME,
                cache_dir=cache_dir,
                force_download=force_download,
                proxies=proxies,
                local_files_only=local_files_only,
                token=token,
                revision=revision,
                subfolder=subfolder,
                user_agent=user_agent,
            )
            raise NotImplementedError(
                f"Only supports deserialization of weights file in safetensors format, but got {model_file}"
            )
    else:
        state_dict = pretrained_model_name_or_path_or_dict

    return state_dict


def _best_guess_weight_name(
    pretrained_model_name_or_path_or_dict, file_extension=".safetensors", local_files_only=False
):
    if local_files_only or HF_HUB_OFFLINE:
        raise ValueError("When using the offline mode, you must specify a `weight_name`.")

    targeted_files = []

    if os.path.isfile(pretrained_model_name_or_path_or_dict):
        return
    elif os.path.isdir(pretrained_model_name_or_path_or_dict):
        targeted_files = [f for f in os.listdir(pretrained_model_name_or_path_or_dict) if f.endswith(file_extension)]
    else:
        files_in_repo = model_info(pretrained_model_name_or_path_or_dict).siblings
        targeted_files = [f.rfilename for f in files_in_repo if f.rfilename.endswith(file_extension)]
    if len(targeted_files) == 0:
        return

    # "scheduler" does not correspond to a LoRA checkpoint.
    # "optimizer" does not correspond to a LoRA checkpoint
    # only top-level checkpoints are considered and not the other ones, hence "checkpoint".
    unallowed_substrings = {"scheduler", "optimizer", "checkpoint"}
    targeted_files = list(
        filter(lambda x: all(substring not in x for substring in unallowed_substrings), targeted_files)
    )

    if any(f.endswith(LORA_WEIGHT_NAME) for f in targeted_files):
        targeted_files = list(filter(lambda x: x.endswith(LORA_WEIGHT_NAME), targeted_files))
    elif any(f.endswith(LORA_WEIGHT_NAME_SAFE) for f in targeted_files):
        targeted_files = list(filter(lambda x: x.endswith(LORA_WEIGHT_NAME_SAFE), targeted_files))

    if len(targeted_files) > 1:
        raise ValueError(
            f"Provided path contains more than one weights file in the {file_extension} format. Either specify `weight_name` in `load_lora_weights` or make sure there's only one  `.safetensors` or `.bin` file in  {pretrained_model_name_or_path_or_dict}."  # noqa: E501
        )
    weight_name = targeted_files[0]
    return weight_name


def _load_lora_into_text_encoder(
    state_dict,
    network_alphas,
    text_encoder,
    prefix=None,
    lora_scale=1.0,
    text_encoder_name="text_encoder",
    adapter_name=None,
    _pipeline=None,
    hotswap: bool = False,
):
    from mindone.diffusers._peft import LoraConfig

    # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
    # then the `state_dict` keys should have `unet_name` and/or `text_encoder_name` as
    # their prefixes.
    prefix = text_encoder_name if prefix is None else prefix

    # Safe prefix to check with.
    if hotswap and any(text_encoder_name in key for key in state_dict.keys()):
        raise ValueError("At the moment, hotswapping is not supported for text encoders, please pass `hotswap=False`.")

    # Load the layers corresponding to text encoder and make necessary adjustments.
    if prefix is not None:
        state_dict = {k[len(f"{prefix}.") :]: v for k, v in state_dict.items() if k.startswith(f"{prefix}.")}

    if len(state_dict) > 0:
        logger.info(f"Loading {prefix}.")
        rank = {}
        state_dict = convert_state_dict_to_diffusers(state_dict)

        # convert state dict
        state_dict = convert_state_dict_to_peft(state_dict)

        for name, _ in text_encoder_attn_modules(text_encoder):
            for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                rank_key = f"{name}.{module}.lora_B.weight"
                if rank_key not in state_dict:
                    continue
                rank[rank_key] = state_dict[rank_key].shape[1]

        for name, _ in text_encoder_mlp_modules(text_encoder):
            for module in ("fc1", "fc2"):
                rank_key = f"{name}.{module}.lora_B.weight"
                if rank_key not in state_dict:
                    continue
                rank[rank_key] = state_dict[rank_key].shape[1]

        if network_alphas is not None:
            alpha_keys = [k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix]
            network_alphas = {k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys}

        lora_config_kwargs = get_peft_kwargs(rank, network_alphas, state_dict, is_unet=False)

        if "use_dora" in lora_config_kwargs:
            if lora_config_kwargs["use_dora"]:
                if is_peft_version("<", "0.9.0"):
                    raise ValueError(
                        "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                    )
            else:
                if is_peft_version("<", "0.9.0"):
                    lora_config_kwargs.pop("use_dora")

        if "lora_bias" in lora_config_kwargs:
            if lora_config_kwargs["lora_bias"]:
                if is_peft_version("<=", "0.13.2"):
                    raise ValueError(
                        "You need `peft` 0.14.0 at least to use `bias` in LoRAs. Please upgrade your installation of `peft`."
                    )
            else:
                if is_peft_version("<=", "0.13.2"):
                    lora_config_kwargs.pop("lora_bias")

        lora_config = LoraConfig(**lora_config_kwargs)

        # adapter_name
        if adapter_name is None:
            adapter_name = get_adapter_name(text_encoder)

        # inject LoRA layers and load the state dict
        # in transformers we automatically check whether the adapter name is already in use or not
        text_encoder.load_adapter(
            adapter_name=adapter_name,
            adapter_state_dict=state_dict,
            peft_config=lora_config,
        )

        # scale LoRA layers with `lora_scale`
        scale_lora_layers(text_encoder, weight=lora_scale)

        text_encoder.to(dtype=text_encoder.dtype)

    if prefix is not None and not state_dict:
        logger.warning(
            f"No LoRA keys associated to {text_encoder.__class__.__name__} found with the {prefix=}. "
            "This is safe to ignore if LoRA state dict didn't originally have any "
            f"{text_encoder.__class__.__name__} related params. You can also try specifying `prefix=None` "
            "to resolve the warning. Otherwise, open an issue if you think it's unexpected: "
            "https://github.com/huggingface/diffusers/issues/new"
        )


class LoraBaseMixin:
    """Utility class for handling LoRAs."""

    _lora_loadable_modules = []
    num_fused_loras = 0

    def load_lora_weights(self, **kwargs):
        raise NotImplementedError("`load_lora_weights()` is not implemented.")

    @classmethod
    def save_lora_weights(cls, **kwargs):
        raise NotImplementedError("`save_lora_weights()` not implemented.")

    @classmethod
    def lora_state_dict(cls, **kwargs):
        raise NotImplementedError("`lora_state_dict()` is not implemented.")

    @classmethod
    def _optionally_disable_offloading(cls, _pipeline):
        raise NotImplementedError("`_optionally_disable_offloading()` is not implemented.")

    @classmethod
    def _fetch_state_dict(cls, *args, **kwargs):
        deprecation_message = f"Using the `_fetch_state_dict()` method from {cls} has been deprecated and will be removed in a future version. Please use `from diffusers.loaders.lora_base import _fetch_state_dict`."  # noqa: E501
        deprecate("_fetch_state_dict", "0.35.0", deprecation_message)
        return _fetch_state_dict(*args, **kwargs)

    @classmethod
    def _best_guess_weight_name(cls, *args, **kwargs):
        deprecation_message = f"Using the `_best_guess_weight_name()` method from {cls} has been deprecated and will be removed in a future version. Please use `from diffusers.loaders.lora_base import _best_guess_weight_name`."  # noqa: E501
        deprecate("_best_guess_weight_name", "0.35.0", deprecation_message)
        return _best_guess_weight_name(*args, **kwargs)

    def unload_lora_weights(self):
        """
        Unloads the LoRA parameters.

        Examples:

        ```python
        >>> # Assuming `pipeline` is already loaded with the LoRA parameters.
        >>> pipeline.unload_lora_weights()
        >>> ...
        ```
        """
        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if model is not None:
                if issubclass(model.__class__, ModelMixin):
                    model.unload_lora()
                elif issubclass(model.__class__, MSPreTrainedModel):
                    _remove_text_encoder_monkey_patch(model)

    def fuse_lora(
        self,
        components: List[str] = [],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from mindone.diffusers import DiffusionPipeline
        import mindspore

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", mindspore_dtype=mindspore.float16
        )
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        if "fuse_unet" in kwargs:
            depr_message = "Passing `fuse_unet` to `fuse_lora()` is deprecated and will be ignored. Please use the `components` argument and provide a list of the components whose LoRAs are to be fused. `fuse_unet` will be removed in a future version."  # noqa: E501
            deprecate(
                "fuse_unet",
                "1.0.0",
                depr_message,
            )
        if "fuse_transformer" in kwargs:
            depr_message = "Passing `fuse_transformer` to `fuse_lora()` is deprecated and will be ignored. Please use the `components` argument and provide a list of the components whose LoRAs are to be fused. `fuse_transformer` will be removed in a future version."  # noqa: E501
            deprecate(
                "fuse_transformer",
                "1.0.0",
                depr_message,
            )
        if "fuse_text_encoder" in kwargs:
            depr_message = "Passing `fuse_text_encoder` to `fuse_lora()` is deprecated and will be ignored. Please use the `components` argument and provide a list of the components whose LoRAs are to be fused. `fuse_text_encoder` will be removed in a future version."  # noqa: E501
            deprecate(
                "fuse_text_encoder",
                "1.0.0",
                depr_message,
            )

        if len(components) == 0:
            raise ValueError("`components` cannot be an empty list.")

        for fuse_component in components:
            if fuse_component not in self._lora_loadable_modules:
                raise ValueError(f"{fuse_component} is not found in {self._lora_loadable_modules=}.")

            model = getattr(self, fuse_component, None)
            if model is not None:
                # check if diffusers model
                if issubclass(model.__class__, ModelMixin):
                    model.fuse_lora(lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names)
                # handle transformers models.
                if issubclass(model.__class__, MSPreTrainedModel):
                    fuse_text_encoder_lora(
                        model, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
                    )

        self.num_fused_loras += 1

    def unfuse_lora(self, components: List[str] = [], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
            unfuse_unet (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
        """
        if "unfuse_unet" in kwargs:
            depr_message = "Passing `unfuse_unet` to `unfuse_lora()` is deprecated and will be ignored. Please use the `components` argument. `unfuse_unet` will be removed in a future version."  # noqa: E501
            deprecate(
                "unfuse_unet",
                "1.0.0",
                depr_message,
            )
        if "unfuse_transformer" in kwargs:
            depr_message = "Passing `unfuse_transformer` to `unfuse_lora()` is deprecated and will be ignored. Please use the `components` argument. `unfuse_transformer` will be removed in a future version."  # noqa: E501
            deprecate(
                "unfuse_transformer",
                "1.0.0",
                depr_message,
            )
        if "unfuse_text_encoder" in kwargs:
            depr_message = "Passing `unfuse_text_encoder` to `unfuse_lora()` is deprecated and will be ignored. Please use the `components` argument. `unfuse_text_encoder` will be removed in a future version."  # noqa: E501
            deprecate(
                "unfuse_text_encoder",
                "1.0.0",
                depr_message,
            )

        if len(components) == 0:
            raise ValueError("`components` cannot be an empty list.")

        for fuse_component in components:
            if fuse_component not in self._lora_loadable_modules:
                raise ValueError(f"{fuse_component} is not found in {self._lora_loadable_modules=}.")

            model = getattr(self, fuse_component, None)
            if model is not None:
                if issubclass(model.__class__, (ModelMixin, MSPreTrainedModel)):
                    for _, module in model.cells_and_names():
                        if isinstance(module, BaseTunerLayer):
                            module.unmerge()

        self.num_fused_loras -= 1

    def set_adapters(
        self,
        adapter_names: Union[List[str], str],
        adapter_weights: Optional[Union[float, Dict, List[float], List[Dict]]] = None,
    ):
        if isinstance(adapter_weights, dict):
            components_passed = set(adapter_weights.keys())
            lora_components = set(self._lora_loadable_modules)

            invalid_components = sorted(components_passed - lora_components)
            if invalid_components:
                logger.warning(
                    f"The following components in `adapter_weights` are not part of the pipeline: {invalid_components}. "
                    f"Available components that are LoRA-compatible: {self._lora_loadable_modules}. So, weights belonging "
                    "to the invalid components will be removed and ignored."
                )
                adapter_weights = {k: v for k, v in adapter_weights.items() if k not in invalid_components}

        adapter_names = [adapter_names] if isinstance(adapter_names, str) else adapter_names
        adapter_weights = copy.deepcopy(adapter_weights)

        # Expand weights into a list, one entry per adapter
        if not isinstance(adapter_weights, list):
            adapter_weights = [adapter_weights] * len(adapter_names)

        if len(adapter_names) != len(adapter_weights):
            raise ValueError(
                f"Length of adapter names {len(adapter_names)} is not equal to the length of the weights {len(adapter_weights)}"
            )

        list_adapters = self.get_list_adapters()  # eg {"unet": ["adapter1", "adapter2"], "text_encoder": ["adapter2"]}
        # eg ["adapter1", "adapter2"]
        all_adapters = {adapter for adapters in list_adapters.values() for adapter in adapters}
        missing_adapters = set(adapter_names) - all_adapters
        if len(missing_adapters) > 0:
            raise ValueError(f"Adapter name(s) {missing_adapters} not in the list of present adapters: {all_adapters}.")

        # eg {"adapter1": ["unet"], "adapter2": ["unet", "text_encoder"]}
        invert_list_adapters = {
            adapter: [part for part, adapters in list_adapters.items() if adapter in adapters]
            for adapter in all_adapters
        }

        # Decompose weights into weights for denoiser and text encoders.
        _component_adapter_weights = {}
        for component in self._lora_loadable_modules:
            model = getattr(self, component)

            for adapter_name, weights in zip(adapter_names, adapter_weights):
                if isinstance(weights, dict):
                    component_adapter_weights = weights.pop(component, None)
                    if component_adapter_weights is not None and component not in invert_list_adapters[adapter_name]:
                        logger.warning(
                            (
                                f"Lora weight dict for adapter '{adapter_name}' contains {component},"
                                f"but this will be ignored because {adapter_name} does not contain weights for {component}."
                                f"Valid parts for {adapter_name} are: {invert_list_adapters[adapter_name]}."
                            )
                        )

                else:
                    component_adapter_weights = weights

                _component_adapter_weights.setdefault(component, [])
                _component_adapter_weights[component].append(component_adapter_weights)

            if issubclass(model.__class__, ModelMixin):
                model.set_adapters(adapter_names, _component_adapter_weights[component])
            elif issubclass(model.__class__, MSPreTrainedModel):
                set_adapters_for_text_encoder(adapter_names, model, _component_adapter_weights[component])

    def disable_lora(self):
        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if model is not None:
                if issubclass(model.__class__, ModelMixin):
                    model.disable_lora()
                elif issubclass(model.__class__, MSPreTrainedModel):
                    disable_lora_for_text_encoder(model)

    def enable_lora(self):
        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if model is not None:
                if issubclass(model.__class__, ModelMixin):
                    model.enable_lora()
                elif issubclass(model.__class__, MSPreTrainedModel):
                    enable_lora_for_text_encoder(model)

    def delete_adapters(self, adapter_names: Union[List[str], str]):
        """
        Args:
        Deletes the LoRA layers of `adapter_name` for the unet and text-encoder(s).
            adapter_names (`Union[List[str], str]`):
                The names of the adapter to delete. Can be a single string or a list of strings
        """
        if isinstance(adapter_names, str):
            adapter_names = [adapter_names]

        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if model is not None:
                if issubclass(model.__class__, ModelMixin):
                    model.delete_adapters(adapter_names)
                elif issubclass(model.__class__, MSPreTrainedModel):
                    for adapter_name in adapter_names:
                        delete_adapter_layers(model, adapter_name)

    def get_active_adapters(self) -> List[str]:
        """
        Gets the list of the current active adapters.

        Example:

        ```python
        from mindone.diffusers import DiffusionPipeline

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
        )
        pipeline.load_lora_weights("CiroN2022/toy-face", weight_name="toy_face_sdxl.safetensors", adapter_name="toy")
        pipeline.get_active_adapters()
        ```
        """
        active_adapters = []

        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if model is not None and issubclass(model.__class__, ModelMixin):
                for _, module in model.cells_and_names():
                    if isinstance(module, BaseTunerLayer):
                        active_adapters = module.active_adapters
                        break

        return active_adapters

    def get_list_adapters(self) -> Dict[str, List[str]]:
        """
        Gets the current list of all available adapters in the pipeline.
        """
        set_adapters = {}

        for component in self._lora_loadable_modules:
            model = getattr(self, component, None)
            if (
                model is not None
                and issubclass(model.__class__, (ModelMixin, MSPreTrainedModel))
                and hasattr(model, "peft_config")
            ):
                set_adapters[component] = list(model.peft_config.keys())

        return set_adapters

    @staticmethod
    def pack_weights(layers, prefix):
        layers_weights = layers.parameters_dict() if isinstance(layers, nn.Cell) else layers
        layers_state_dict = {f"{prefix}.{module_name}": param for module_name, param in layers_weights.items()}
        return layers_state_dict

    @staticmethod
    def write_lora_layers(
        state_dict: Dict[str, ms.Tensor],
        save_directory: str,
        is_main_process: bool,
        weight_name: str,
        save_function: Callable,
        safe_serialization: bool,
    ):
        if os.path.isfile(save_directory):
            logger.error(f"Provided path ({save_directory}) should be a directory, not a file")
            return

        if save_function is None:
            if safe_serialization:

                def save_function(weights, filename):
                    return save_file(weights, filename, metadata={"format": "np"})

            else:
                save_function = ms.save_checkpoint

        os.makedirs(save_directory, exist_ok=True)

        if weight_name is None:
            if safe_serialization:
                weight_name = LORA_WEIGHT_NAME_SAFE
            else:
                weight_name = LORA_WEIGHT_NAME

        save_path = Path(save_directory, weight_name).as_posix()
        save_function(state_dict, save_path)
        logger.info(f"Model weights saved in {save_path}")

    @property
    def lora_scale(self) -> float:
        # property function that returns the lora scale which can be set at run time by the pipeline.
        # if _lora_scale has not been set, return 1
        return self._lora_scale if hasattr(self, "_lora_scale") else 1.0

    def enable_lora_hotswap(self, **kwargs) -> None:
        """Enables the possibility to hotswap LoRA adapters.

        Calling this method is only required when hotswapping adapters and if the model is compiled or if the ranks of
        the loaded adapters differ.

        Args:
            target_rank (`int`):
                The highest rank among all the adapters that will be loaded.
            check_compiled (`str`, *optional*, defaults to `"error"`):
                How to handle the case when the model is already compiled, which should generally be avoided. The
                options are:
                  - "error" (default): raise an error
                  - "warn": issue a warning
                  - "ignore": do nothing
        """
        for key, component in self.components.items():
            if hasattr(component, "enable_lora_hotswap") and (key in self._lora_loadable_modules):
                component.enable_lora_hotswap(**kwargs)
