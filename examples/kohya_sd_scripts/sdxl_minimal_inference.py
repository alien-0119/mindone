# Adapted from https://github.com/kohya-ss/sd-scripts/blob/main/sdxl_minimal_inference.py
# 手元で推論を行うための最低限のコード。HuggingFace／DiffusersのCLIP、schedulerとVAEを使う
# Minimal code for performing inference at local. Use HuggingFace/Diffusers CLIP, scheduler and VAE

import argparse
import datetime
import os
import random

import networks.lora as lora
import numpy as np
from library import sdxl_model_util
from library.utils import setup_logging
from PIL import Image
from tqdm import tqdm
from transformers import CLIPTokenizer

import mindspore as ms
from mindspore import ops

from mindone.diffusers import EulerDiscreteScheduler

setup_logging()
import logging

logger = logging.getLogger(__name__)

# scheduler: このあたりの設定はSD1/2と同じでいいらしい
# scheduler: The settings around here seem to be the same as SD1/2
SCHEDULER_LINEAR_START = 0.00085
SCHEDULER_LINEAR_END = 0.0120
SCHEDULER_TIMESTEPS = 1000
SCHEDLER_SCHEDULE = "scaled_linear"


# Time EmbeddingはDiffusersからのコピー
# Time Embedding is copied from Diffusers


def timestep_embedding(timesteps, dim, max_period=10000, repeat_only=False, dtype=ms.float32):
    """
    Create sinusoidal timestep embeddings.
    :param timesteps: a 1-D Tensor of N indices, one per batch element.
                      These may be fractional.
    :param dim: the dimension of the output.
    :param max_period: controls the minimum frequency of the embeddings.
    :return: an [N x dim] Tensor of positional embeddings.
    """
    if not repeat_only:
        half = dim // 2
        freqs = ops.exp(
            -ops.log(ops.ones(1, dtype=ms.float32) * max_period)
            * ops.arange(start=0, end=half, dtype=ms.float32)
            / half
        )
        args = timesteps[:, None].astype(ms.float32) * freqs[None]
        embedding = ops.concat((ops.cos(args), ops.sin(args)), axis=-1)
        if dim % 2:
            embedding = ops.concat((embedding, ops.zeros_like(embedding[:, :1])), axis=-1)
    else:
        embedding = ops.broadcast_to(timesteps[:, None], (-1, dim))
    return embedding.astype(dtype)


def get_timestep_embedding(x, outdim):
    assert len(x.shape) == 2
    b, dims = x.shape[0], x.shape[1]
    # x = rearrange(x, "b d -> (b d)")
    x = x.flatten()
    emb = timestep_embedding(x, outdim)
    # emb = rearrange(emb, "(b d) d2 -> b (d d2)", b=b, d=dims, d2=outdim)
    emb = ops.reshape(emb, (b, dims * outdim))
    return emb


if __name__ == "__main__":
    ms.set_context(mode=ms.GRAPH_MODE, jit_syntax_level=ms.STRICT)

    # 画像生成条件を変更する場合はここを変更 / change here to change image generation conditions

    # SDXLの追加のvector embeddingへ渡す値 / Values to pass to additional vector embedding of SDXL
    target_height = 1024
    target_width = 1024
    original_height = target_height
    original_width = target_width
    crop_top = 0
    crop_left = 0

    steps = 50
    guidance_scale = 7
    seed = None  # 1

    # DEVICE = get_preferred_device()
    DTYPE = ms.float16  # bfloat16 may work

    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt_path", type=str, required=True)
    parser.add_argument("--prompt", type=str, default="A photo of a cat")
    parser.add_argument("--prompt2", type=str, default=None)
    parser.add_argument("--negative_prompt", type=str, default="")
    parser.add_argument("--output_dir", type=str, default=".")
    parser.add_argument(
        "--lora_weights",
        type=str,
        nargs="*",
        default=[],
        help="LoRA weights, only supports networks.lora, each argument is a `path;multiplier` (semi-colon separated)",
    )
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()

    if args.prompt2 is None:
        args.prompt2 = args.prompt

    # HuggingFaceのmodel id
    text_encoder_1_name = "openai/clip-vit-large-patch14"
    text_encoder_2_name = "laion/CLIP-ViT-bigG-14-laion2B-39B-b160k"

    # checkpointを読み込む。モデル変換についてはそちらの関数を参照
    # Load checkpoint. For model conversion, see this function

    # 本体RAMが少ない場合はGPUにロードするといいかも
    # If the main RAM is small, it may be better to load it on the GPU
    text_model1, text_model2, vae, unet, _, _ = sdxl_model_util.load_models_from_sdxl_checkpoint(
        sdxl_model_util.MODEL_VERSION_SDXL_BASE_V1_0, args.ckpt_path
    )

    # Text Encoder 1はSDXL本体でもHuggingFaceのものを使っている
    # In SDXL, Text Encoder 1 is also using HuggingFace's

    # Text Encoder 2はSDXL本体ではopen_clipを使っている
    # それを使ってもいいが、SD2のDiffusers版に合わせる形で、HuggingFaceのものを使う
    # 重みの変換コードはSD2とほぼ同じ
    # In SDXL, Text Encoder 2 is using open_clip
    # It's okay to use it, but to match the Diffusers version of SD2, use HuggingFace's
    # The weight conversion code is almost the same as SD2

    # VAEの構造はSDXLもSD1/2と同じだが、重みは異なるようだ。何より謎のscale値が違う
    # fp16でNaNが出やすいようだ
    # The structure of VAE is the same as SD1/2, but the weights seem to be different. Above all, the mysterious scale value is different.
    # NaN seems to be more likely to occur in fp16

    unet.to(dtype=DTYPE)
    unet.set_train(False)

    vae_dtype = DTYPE
    if DTYPE == ms.float16:
        logger.info("use float32 for vae")
        vae_dtype = ms.float32
    vae.to(dtype=vae_dtype)
    vae.set_train(False)

    text_model1.to(dtype=DTYPE)
    text_model1.set_train(False)
    text_model2.to(dtype=DTYPE)
    text_model2.set_train(False)

    unet.set_use_memory_efficient_attention(True, False)
    # if torch.__version__ >= "2.0.0":  # PyTorch 2.0.0 以上対応のxformersなら以下が使える
    #     vae.set_use_memory_efficient_attention_xformers(True)

    # Tokenizers
    tokenizer1 = CLIPTokenizer.from_pretrained(text_encoder_1_name)
    # tokenizer2 = lambda x: open_clip.tokenize(x, context_length=77)
    tokenizer2 = CLIPTokenizer.from_pretrained(text_encoder_2_name)

    # LoRA
    for weights_file in args.lora_weights:
        if ";" in weights_file:
            weights_file, multiplier = weights_file.split(";")
            multiplier = float(multiplier)
        else:
            multiplier = 1.0

        # lora weights are directly loaded and merged here
        lora.create_network_from_weights(
            multiplier, weights_file, vae, [text_model1, text_model2], unet, weights_sd=None, dtype=DTYPE
        )

    # scheduler
    scheduler = EulerDiscreteScheduler(
        num_train_timesteps=SCHEDULER_TIMESTEPS,
        beta_start=SCHEDULER_LINEAR_START,
        beta_end=SCHEDULER_LINEAR_END,
        beta_schedule=SCHEDLER_SCHEDULE,
    )

    def generate_image(prompt, prompt2, negative_prompt, seed=None):
        # 将来的にサイズ情報も変えられるようにする / Make it possible to change the size information in the future
        # prepare embedding
        # vector
        emb1 = get_timestep_embedding(ms.Tensor([original_height, original_width]).unsqueeze(0), 256)
        emb2 = get_timestep_embedding(ms.Tensor([crop_top, crop_left]).unsqueeze(0), 256)
        emb3 = get_timestep_embedding(ms.Tensor([target_height, target_width]).unsqueeze(0), 256)
        # logger.info("emb1", emb1.shape)
        c_vector = ops.cat([emb1, emb2, emb3], axis=1).to(dtype=DTYPE)
        uc_vector = c_vector.copy().to(dtype=DTYPE)  # ちょっとここ正しいかどうかわからない I'm not sure if this is right

        # crossattn

        # Text Encoderを二つ呼ぶ関数  Function to call two Text Encoders
        def call_text_encoder(text, text2):
            # text encoder 1
            batch_encoding = tokenizer1(
                text,
                truncation=True,
                return_length=True,
                return_overflowing_tokens=False,
                padding="max_length",
                return_tensors="np",
            )
            tokens = batch_encoding.input_ids

            enc_out = text_model1(ms.Tensor(tokens), output_hidden_states=True, return_dict=False)
            text_embedding1 = enc_out[-1][-2]

            # text encoder 2
            tokens = tokenizer2(
                text,
                truncation=True,
                return_length=True,
                return_overflowing_tokens=False,
                padding="max_length",
                return_tensors="np",
            )
            tokens = batch_encoding.input_ids

            enc_out = text_model2(ms.Tensor(tokens), output_hidden_states=True, return_dict=False)
            text_embedding2_penu = enc_out[-1][-2]
            text_embedding2_pool = enc_out[0]  # do not support Textual Inversion

            # 連結して終了 concat and finish
            text_embedding = ops.cat([text_embedding1, text_embedding2_penu], axis=2)
            return text_embedding, text_embedding2_pool

        # cond
        c_ctx, c_ctx_pool = call_text_encoder(prompt, prompt2)
        c_vector = ops.cat([c_ctx_pool, c_vector], axis=1)

        # uncond
        uc_ctx, uc_ctx_pool = call_text_encoder(negative_prompt, negative_prompt)
        uc_vector = ops.cat([uc_ctx_pool, uc_vector], axis=1)

        text_embeddings = ops.cat([uc_ctx, c_ctx])
        vector_embeddings = ops.cat([uc_vector, c_vector])

        # メモリ使用量を減らすにはここでText Encoderを削除するかCPUへ移動する

        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        # get the initial random noise unless the user supplied it
        # SDXLはCPUでlatentsを作成しているので一応合わせておく、Diffusersはtarget deviceでlatentsを作成している
        # SDXL creates latents in CPU, Diffusers creates latents in target device
        latents_shape = (1, 4, target_height // 8, target_width // 8)
        latents = ops.randn(
            latents_shape,
            seed=seed,
            dtype=ms.float32,
        ).to(dtype=DTYPE)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * scheduler.init_noise_sigma

        # set timesteps
        scheduler.set_timesteps(steps)

        # このへんはDiffusersからのコピペ
        # Copy from Diffusers
        timesteps = scheduler.timesteps  # .to(DTYPE)
        num_latent_input = 2
        for i, t in enumerate(tqdm(timesteps)):
            # expand the latents if we are doing classifier free guidance
            # latent_model_input = latents.repeat((num_latent_input, 1, 1, 1))
            latent_model_input = ops.cat([latents] * num_latent_input)
            latent_model_input = scheduler.scale_model_input(latent_model_input, t)

            noise_pred = unet(latent_model_input, t, text_embeddings, vector_embeddings)

            noise_pred_uncond, noise_pred_text = noise_pred.chunk(num_latent_input)  # uncond by negative prompt
            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = scheduler.step(noise_pred, t, latents, return_dict=False)[0]

        latents = 1 / sdxl_model_util.VAE_SCALE_FACTOR * latents
        latents = latents.to(vae_dtype)
        image = vae.decode(latents, return_dict=False)[0]
        image = (image / 2 + 0.5).clamp(0, 1)

        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloa16
        image = image.permute(0, 2, 3, 1).float().asnumpy()

        image = (image * 255).round().astype("uint8")
        image = [Image.fromarray(im) for im in image]

        # 保存して終了 save and finish
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for i, img in enumerate(image):
            img.save(os.path.join(args.output_dir, f"image_{timestamp}_{i:03d}.png"))

    if not args.interactive:
        generate_image(args.prompt, args.prompt2, args.negative_prompt, seed)
    else:
        # loop for interactive
        while True:
            prompt = input("prompt: ")
            if prompt == "":
                break
            prompt2 = input("prompt2: ")
            if prompt2 == "":
                prompt2 = prompt
            negative_prompt = input("negative prompt: ")
            seed = input("seed: ")
            if seed == "":
                seed = None
            else:
                seed = int(seed)
            generate_image(prompt, prompt2, negative_prompt, seed)

    logger.info("Done!")
