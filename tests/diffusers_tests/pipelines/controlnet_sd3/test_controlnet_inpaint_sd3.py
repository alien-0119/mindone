# coding=utf-8
# Copyright 2025 HuggingFace Inc.
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

import unittest

import numpy as np
import torch
from ddt import data, ddt, unpack
from diffusers.utils.torch_utils import randn_tensor
from transformers import CLIPTextConfig

import mindspore as ms

from mindone.diffusers.utils.testing_utils import load_downloaded_image_from_hf_hub, load_numpy_from_local_file, slow

from ..pipeline_test_utils import (
    THRESHOLD_FP16,
    THRESHOLD_FP32,
    THRESHOLD_PIXEL,
    PipelineTesterMixin,
    get_module,
    get_pipeline_components,
)

test_cases = [
    {"mode": ms.PYNATIVE_MODE, "dtype": "float32"},
    {"mode": ms.PYNATIVE_MODE, "dtype": "float16"},
    {"mode": ms.GRAPH_MODE, "dtype": "float32"},
    {"mode": ms.GRAPH_MODE, "dtype": "float16"},
]


@ddt
class StableDiffusion3ControlInpaintNetPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_config = [
        [
            "transformer",
            "diffusers.models.transformers.transformer_sd3.SD3Transformer2DModel",
            "mindone.diffusers.models.transformers.transformer_sd3.SD3Transformer2DModel",
            dict(
                sample_size=32,
                patch_size=1,
                in_channels=8,
                num_layers=4,
                attention_head_dim=8,
                num_attention_heads=4,
                joint_attention_dim=32,
                caption_projection_dim=32,
                pooled_projection_dim=64,
                out_channels=8,
            ),
        ],
        [
            "controlnet",
            "diffusers.models.controlnets.controlnet_sd3.SD3ControlNetModel",
            "mindone.diffusers.models.controlnets.controlnet_sd3.SD3ControlNetModel",
            dict(
                sample_size=32,
                patch_size=1,
                in_channels=8,
                num_layers=1,
                attention_head_dim=8,
                num_attention_heads=4,
                joint_attention_dim=32,
                caption_projection_dim=32,
                pooled_projection_dim=64,
                out_channels=8,
                extra_conditioning_channels=1,
            ),
        ],
        [
            "text_encoder",
            "transformers.models.clip.modeling_clip.CLIPTextModelWithProjection",
            "mindone.transformers.models.clip.modeling_clip.CLIPTextModelWithProjection",
            dict(
                config=CLIPTextConfig(
                    bos_token_id=0,
                    eos_token_id=2,
                    hidden_size=32,
                    intermediate_size=37,
                    layer_norm_eps=1e-05,
                    num_attention_heads=4,
                    num_hidden_layers=5,
                    pad_token_id=1,
                    vocab_size=1000,
                    hidden_act="gelu",
                    projection_dim=32,
                ),
            ),
        ],
        [
            "text_encoder_2",
            "transformers.models.clip.modeling_clip.CLIPTextModelWithProjection",
            "mindone.transformers.models.clip.modeling_clip.CLIPTextModelWithProjection",
            dict(
                config=CLIPTextConfig(
                    bos_token_id=0,
                    eos_token_id=2,
                    hidden_size=32,
                    intermediate_size=37,
                    layer_norm_eps=1e-05,
                    num_attention_heads=4,
                    num_hidden_layers=5,
                    pad_token_id=1,
                    vocab_size=1000,
                    hidden_act="gelu",
                    projection_dim=32,
                ),
            ),
        ],
        [
            "text_encoder_3",
            "transformers.models.t5.modeling_t5.T5EncoderModel",
            "mindone.transformers.models.t5.modeling_t5.T5EncoderModel",
            dict(
                pretrained_model_name_or_path="hf-internal-testing/tiny-random-t5",
                revision="refs/pr/1",
            ),
        ],
        [
            "tokenizer",
            "transformers.models.clip.tokenization_clip.CLIPTokenizer",
            "transformers.models.clip.tokenization_clip.CLIPTokenizer",
            dict(
                pretrained_model_name_or_path="hf-internal-testing/tiny-random-clip",
            ),
        ],
        [
            "tokenizer_2",
            "transformers.models.clip.tokenization_clip.CLIPTokenizer",
            "transformers.models.clip.tokenization_clip.CLIPTokenizer",
            dict(
                pretrained_model_name_or_path="hf-internal-testing/tiny-random-clip",
            ),
        ],
        [
            "tokenizer_3",
            "transformers.models.auto.tokenization_auto.AutoTokenizer",
            "transformers.models.auto.tokenization_auto.AutoTokenizer",
            dict(
                pretrained_model_name_or_path="hf-internal-testing/tiny-random-t5",
            ),
        ],
        [
            "vae",
            "diffusers.models.autoencoders.autoencoder_kl.AutoencoderKL",
            "mindone.diffusers.models.autoencoders.autoencoder_kl.AutoencoderKL",
            dict(
                sample_size=32,
                in_channels=3,
                out_channels=3,
                block_out_channels=(4,),
                layers_per_block=1,
                latent_channels=8,
                norm_num_groups=1,
                use_quant_conv=False,
                use_post_quant_conv=False,
                shift_factor=0.0609,
                scaling_factor=1.5035,
            ),
        ],
        [
            "scheduler",
            "diffusers.schedulers.scheduling_flow_match_euler_discrete.FlowMatchEulerDiscreteScheduler",
            "mindone.diffusers.schedulers.scheduling_flow_match_euler_discrete.FlowMatchEulerDiscreteScheduler",
            dict(),
        ],
    ]

    def get_dummy_components(self):
        components = {
            key: None
            for key in [
                "scheduler",
                "text_encoder",
                "text_encoder_2",
                "text_encoder_3",
                "tokenizer",
                "tokenizer_2",
                "tokenizer_3",
                "transformer",
                "vae",
                "controlnet",
            ]
        }

        return get_pipeline_components(components, self.pipeline_config)

    def get_dummy_inputs(self, seed=0):
        generator = torch.manual_seed(seed)

        pt_control_image = randn_tensor((1, 3, 32, 32), generator=generator)
        pt_control_mask = randn_tensor((1, 1, 32, 32), generator=generator)

        ms_control_image = ms.tensor(pt_control_image.numpy())
        ms_control_mask = ms.tensor(pt_control_mask.numpy())

        controlnet_conditioning_scale = 0.95

        pt_inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "num_inference_steps": 2,
            "guidance_scale": 7.0,
            "output_type": "np",
            "control_image": pt_control_image,
            "control_mask": pt_control_mask,
            "controlnet_conditioning_scale": controlnet_conditioning_scale,
        }

        ms_inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "num_inference_steps": 2,
            "guidance_scale": 7.0,
            "output_type": "np",
            "control_image": ms_control_image,
            "control_mask": ms_control_mask,
            "controlnet_conditioning_scale": controlnet_conditioning_scale,
        }

        return pt_inputs, ms_inputs

    @data(*test_cases)
    @unpack
    def test_controlnet_inpaint_sd3(self, mode, dtype):
        ms.set_context(mode=mode)

        pt_components, ms_components = self.get_dummy_components()
        pt_pipe_cls = get_module(
            "diffusers.pipelines.controlnet_sd3.pipeline_stable_diffusion_3_controlnet_inpainting.StableDiffusion3ControlNetInpaintingPipeline"
        )
        ms_pipe_cls = get_module(
            "mindone.diffusers.pipelines.controlnet_sd3.pipeline_stable_diffusion_3_controlnet_inpainting.StableDiffusion3ControlNetInpaintingPipeline"
        )

        pt_pipe = pt_pipe_cls(**pt_components)
        ms_pipe = ms_pipe_cls(**ms_components)

        pt_pipe.set_progress_bar_config(disable=None)
        ms_pipe.set_progress_bar_config(disable=None)

        ms_dtype, pt_dtype = getattr(ms, dtype), getattr(torch, dtype)
        pt_pipe = pt_pipe.to(pt_dtype)
        ms_pipe = ms_pipe.to(ms_dtype)

        pt_inputs, ms_inputs = self.get_dummy_inputs()

        torch.manual_seed(0)
        pt_image = pt_pipe(**pt_inputs)
        torch.manual_seed(0)
        ms_image = ms_pipe(**ms_inputs)

        pt_image_slice = pt_image.images[0, -3:, -3:, -1]
        ms_image_slice = ms_image[0][0, -3:, -3:, -1]

        threshold = THRESHOLD_FP32 if dtype == "float32" else THRESHOLD_FP16
        assert np.max(np.linalg.norm(pt_image_slice - ms_image_slice) / np.linalg.norm(pt_image_slice)) < threshold


@slow
@ddt
class StableDiffusion3ControlNetInpaintNetPipelineIntegrationTests(PipelineTesterMixin, unittest.TestCase):
    @data(*test_cases)
    @unpack
    def test_controlnet_inpaint_sd3(self, mode, dtype):
        ms.set_context(mode=mode)
        ms_dtype = getattr(ms, dtype)
        controlnet_cls = get_module("mindone.diffusers.models.controlnet_sd3.SD3ControlNetModel")
        pipe_cls = get_module(
            "mindone.diffusers.pipelines.controlnet_sd3.pipeline_stable_diffusion_3_controlnet_inpainting.StableDiffusion3ControlNetInpaintingPipeline"
        )
        controlnet = controlnet_cls.from_pretrained(
            "alimama-creative/SD3-Controlnet-Inpainting", use_safetensors=True, extra_conditioning_channels=1
        )
        pipe = pipe_cls.from_pretrained(
            "stabilityai/stable-diffusion-3-medium-diffusers",
            controlnet=controlnet,
            mindspore_dtype=ms_dtype,
        )

        pipe.text_encoder.to(ms_dtype)
        pipe.controlnet.to(ms_dtype)

        image = load_downloaded_image_from_hf_hub(
            "alimama-creative/SD3-Controlnet-Inpainting",
            "dog.png",
            subfolder="images",
            repo_type="model",
        )
        mask = load_downloaded_image_from_hf_hub(
            "alimama-creative/SD3-Controlnet-Inpainting",
            "dog_mask.png",
            subfolder="images",
            repo_type="model",
        )

        prompt = "A cat is sitting next to a puppy."
        negative_prompt = (
            "deformed, distorted, disfigured, poorly drawn, bad anatomy, wrong anatomy, extra limb, "
            "missing limb, floating limbs, mutated hands and fingers, disconnected limbs, mutation, "
            "mutated, ugly, disgusting, blurry, amputation, NSFW"
        )
        torch.manual_seed(0)
        image = pipe(
            negative_prompt=negative_prompt,
            prompt=prompt,
            height=1024,
            width=1024,
            control_image=image,
            control_mask=mask,
            num_inference_steps=28,
            controlnet_conditioning_scale=0.95,
            guidance_scale=7,
        )[0][0]

        expected_image = load_numpy_from_local_file(
            "mindone-testing-arrays",
            f"controlnet_sd3_inpaint_{dtype}.npy",
            subfolder="controlnet_sd3",
        )
        assert np.mean(np.abs(np.array(image, dtype=np.float32) - expected_image)) < THRESHOLD_PIXEL
