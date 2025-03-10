# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""Transformer."""
from contextlib import nullcontext
from typing import Any, Callable, Optional

import torch
from einops import rearrange

from nemo.collections.common.parts.adapter_modules import LinearAdapterConfig
from nemo.collections.nlp.modules.common.megatron.adapters.parallel_adapters import (
    AdapterName,
    ParallelLinearAdapterConfig,
)
from nemo.collections.nlp.modules.common.megatron.attention import ParallelAttention, ParallelChunkedCrossAttention
from nemo.collections.nlp.modules.common.megatron.fused_bias_dropout_add import (
    bias_dropout_add,
    bias_dropout_add_fused_inference,
    bias_dropout_add_fused_train,
    dropout_add,
)
from nemo.collections.nlp.modules.common.megatron.fused_layer_norm import get_layer_norm
from nemo.collections.nlp.modules.common.megatron.layer_norm_1p import LayerNorm1P
from nemo.collections.nlp.modules.common.megatron.layer_type import LayerType
from nemo.collections.nlp.modules.common.megatron.mlp import ParallelMLP, SwitchMLP
from nemo.collections.nlp.modules.common.megatron.module import MegatronModule
from nemo.collections.nlp.modules.common.megatron.utils import ApexGuardDefaults
from nemo.core import adapter_mixins
from nemo.utils import logging

try:
    from apex.transformer import parallel_state, tensor_parallel
    from apex.transformer.enums import AttnMaskType, AttnType, ModelType
    from apex.normalization import MixedFusedRMSNorm

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):

    HAVE_APEX = False

    # fake missing classes with None attributes
    ModelType = AttnMaskType = AttnType = LayerType = ApexGuardDefaults()

try:
    from transformer_engine.pytorch import TransformerLayer, fp8_autocast
    from transformer_engine.common import recipe
    from transformer_engine.pytorch.distributed import checkpoint as te_checkpoint

    HAVE_TE = True

except:
    HAVE_TE = False

    # fake missing class
    class TransformerLayer(ApexGuardDefaults):
        def __init__(self):
            super().__init__()

            logging.warning(
                "Transformer Engine was not found. transformer_engine.pytorch.transformer.TransformerLayer will not work. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )


""" We use the following notation throughout this file:
     h: hidden size
     n: number of attention heads
     p: number of model parallel partitions
     np: n/p
     hp: h/p
     hn: h/n
     b: batch size
     s: sequence length
     l: number of layers
    Transformer takes input of size [s, b, h] and returns a
    tensor of the same size. We use the following arguments:
        hyperparameters: transformer hyperparameters
"""


def get_bias_dropout_add(training):
    def _bias_dropout_add(x, bias, residual, prob):
        return bias_dropout_add(x, bias, residual, prob, training)

    return _bias_dropout_add


def get_dropout_add(training):
    def _dropout_add(x, bias, residual, prob):
        assert bias is None
        return dropout_add(x, bias, residual, prob, training)

    return _dropout_add


class ParallelTransformerLayer_(MegatronModule, adapter_mixins.AdapterModuleMixin):
    """A single transformer layer.

    Transformer layer takes input with size [s, b, h] and returns an
    output of the same size.
    """

    def __init__(
        self,
        init_method,
        output_layer_init_method,
        layer_number,
        hidden_size,
        ffn_hidden_size,
        num_attention_heads,
        layer_type=LayerType.encoder,
        self_attn_mask_type=AttnMaskType.padding,
        fp32_residual_connection=False,
        precision=16,
        apply_query_key_layer_scaling=True,
        kv_channels=None,
        layernorm_epsilon=1e-5,
        hidden_dropout=0.1,
        persist_layer_norm=False,
        use_cpu_initialization=False,
        bias_activation_fusion=True,
        bias_dropout_add_fusion=True,
        masked_softmax_fusion=True,
        gradient_accumulation_fusion=False,
        openai_gelu=False,
        onnx_safe=False,
        attention_dropout=0.1,
        ffn_dropout=0.0,
        activation='gelu',
        megatron_legacy=False,
        bias=True,
        chunk_size=64,
        normalization='layernorm',
        transformer_block_type='pre_ln',
        headscale=False,
        activations_checkpoint_granularity=None,
        sequence_parallel=False,
        normalize_attention_scores=True,
        num_moe_experts=1,
        moe_frequency=1,
        moe_dropout=0.0,
    ):
        super(ParallelTransformerLayer_, self).__init__()

        if kv_channels is None:
            assert (
                hidden_size % num_attention_heads == 0
            ), 'hidden_size must be divisible by num_attention_heads if kv_channels is None'
            kv_channels = hidden_size // num_attention_heads

        self.layer_number = layer_number
        self.layer_type = layer_type
        self.bias = bias
        self.transformer_block_type = transformer_block_type
        self.set_accepted_adapter_types([LinearAdapterConfig._target_, ParallelLinearAdapterConfig._target_])

        if not bias and bias_dropout_add_fusion:
            raise ValueError(
                'bias_dropout_add_fusion=True requires bias=True, found bias=False. Either set both to True or both to False.'
            )

        if normalization not in ['layernorm', 'layernorm1p', 'rmsnorm']:
            raise ValueError(f'normalization must be "layernorm", "layernorm1p" or "rmsnorm", found {normalization}')

        if transformer_block_type not in ['pre_ln', 'post_ln', 'normformer']:
            raise ValueError(
                f'transformer_block_type must be either "pre_ln" or "post_ln" or "normformer", found {transformer_block_type}'
            )

        self.fp32_residual_connection = fp32_residual_connection  # if true move residual connections to fp32
        self.hidden_dropout = hidden_dropout
        self.attention_dropout = attention_dropout
        self.bias_dropout_add_fusion = bias_dropout_add_fusion  # if true, enable bias dropout fusion

        # Self attention.
        # retrieval_decoder_after_self_attn skips the self attention
        if self.layer_type != LayerType.retrieval_decoder_after_self_attn:
            # Layernorm on the input data.
            if normalization == 'layernorm':
                self.input_layernorm = get_layer_norm(
                    hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                )
            elif normalization == 'layernorm1p':
                self.input_layernorm = LayerNorm1P(
                    hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                )
            else:
                self.input_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

            self.self_attention = ParallelAttention(
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                layer_number=layer_number,
                num_attention_heads=num_attention_heads,
                hidden_size=hidden_size,
                attention_type=AttnType.self_attn,
                attn_mask_type=self_attn_mask_type,
                precision=precision,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                kv_channels=kv_channels,
                use_cpu_initialization=use_cpu_initialization,
                masked_softmax_fusion=masked_softmax_fusion,
                attention_dropout=attention_dropout,
                layer_type=layer_type,
                megatron_legacy=megatron_legacy,
                bias=bias,
                headscale=headscale,
                activations_checkpoint_granularity=activations_checkpoint_granularity,
                sequence_parallel=sequence_parallel,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
                normalize_attention_scores=normalize_attention_scores,
            )

            if transformer_block_type == 'normformer':
                if normalization == 'layernorm':
                    self.post_attention_normformer_norm = get_layer_norm(
                        hidden_size, layernorm_epsilon, persist_layer_norm
                    )
                else:
                    self.post_attention_normformer_norm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

            if self.layer_type != LayerType.decoder_pre_mlp or self.transformer_block_type != 'post_ln':
                #  the post_attention_layernorm is used for layermorm after mlp
                # don't need it for decoder_pre_mlp and post_ln
                if normalization == 'layernorm':
                    self.post_attention_layernorm = get_layer_norm(
                        hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                    )
                elif normalization == 'layernorm1p':
                    self.post_attention_layernorm = LayerNorm1P(
                        hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                    )
                else:
                    self.post_attention_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

        if self.layer_type == LayerType.decoder_pre_mlp:
            # skip MLP and cross attention
            return

        # the post_attention_layernorm is used for layermorm after mlp
        # need it for post_ln
        if self.layer_type == LayerType.retrieval_decoder_after_self_attn and self.transformer_block_type == 'post_ln':
            # Layernorm on the attention output
            if normalization == 'layernorm':
                self.post_attention_layernorm = get_layer_norm(
                    hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                )
            elif normalization == 'layernorm1p':
                self.post_attention_layernorm = LayerNorm1P(
                    hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                )
            else:
                self.post_attention_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

        if self.layer_type == LayerType.decoder or self.layer_type == LayerType.retrieval_encoder:
            self.inter_attention = ParallelAttention(
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                layer_number=layer_number,
                num_attention_heads=num_attention_heads,
                hidden_size=hidden_size,
                attention_type=AttnType.cross_attn,
                attn_mask_type=AttnMaskType.padding,
                precision=precision,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                kv_channels=kv_channels,
                use_cpu_initialization=use_cpu_initialization,
                masked_softmax_fusion=masked_softmax_fusion,
                attention_dropout=attention_dropout,
                megatron_legacy=megatron_legacy,
                bias=bias,
                headscale=headscale,
                sequence_parallel=sequence_parallel,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
                normalize_attention_scores=normalize_attention_scores,
            )
            # Normformer normalization
            if transformer_block_type == 'normformer':
                if normalization == 'layernorm':
                    self.post_inter_attention_normformer_norm = get_layer_norm(
                        hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                    )
                elif normalization == 'layernorm1p':
                    self.post_inter_attention_normformer_norm = LayerNorm1P(
                        hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                    )
                else:
                    self.post_inter_attention_normformer_norm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

            # Layernorm on the attention output.
            if normalization == 'layernorm':
                self.post_inter_attention_layernorm = get_layer_norm(
                    hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                )
            elif normalization == 'layernorm1p':
                self.post_inter_attention_layernorm = LayerNorm1P(
                    hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                )
            else:
                self.post_inter_attention_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)
        elif (
            self.layer_type == LayerType.retrieval_decoder
            or self.layer_type == LayerType.retrieval_decoder_after_self_attn
        ):
            self.inter_attention = ParallelChunkedCrossAttention(
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                layer_number=layer_number,
                num_attention_heads=num_attention_heads,
                hidden_size=hidden_size,
                precision=precision,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                kv_channels=kv_channels,
                use_cpu_initialization=use_cpu_initialization,
                masked_softmax_fusion=masked_softmax_fusion,
                attention_dropout=attention_dropout,
                megatron_legacy=megatron_legacy,
                chunk_size=chunk_size,
                bias=bias,
                headscale=headscale,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
            )
            # Normformer normalization
            if transformer_block_type == 'normformer':
                if normalization == 'layernorm':
                    self.post_inter_attention_normformer_norm = get_layer_norm(
                        hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                    )
                elif normalization == 'layernorm1p':
                    self.post_inter_attention_normformer_norm = LayerNorm1P(
                        hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                    )
                else:
                    self.post_inter_attention_normformer_norm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

            # Layernorm on the attention output.
            if normalization == 'layernorm':
                self.post_inter_attention_layernorm = get_layer_norm(
                    hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel
                )
            elif normalization == 'layernorm1p':
                self.post_inter_attention_layernorm = LayerNorm1P(
                    hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                )
            else:
                self.post_inter_attention_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

        # MLP
        if num_moe_experts > 1 and self.layer_number % moe_frequency == 0:
            self.mlp = SwitchMLP(
                num_experts=num_moe_experts,
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                hidden_size=hidden_size,
                ffn_hidden_size=ffn_hidden_size,
                use_cpu_initialization=use_cpu_initialization,
                bias_activation_fusion=bias_activation_fusion,
                openai_gelu=openai_gelu,
                onnx_safe=onnx_safe,
                activation=activation,
                bias=bias,
                transformer_block_type=transformer_block_type,
                normalization=normalization,
                layernorm_epsilon=layernorm_epsilon,
                persist_layer_norm=persist_layer_norm,
                sequence_parallel=sequence_parallel,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
                dropout=moe_dropout,
            )
        else:
            self.mlp = ParallelMLP(
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                hidden_size=hidden_size,
                ffn_hidden_size=ffn_hidden_size,
                use_cpu_initialization=use_cpu_initialization,
                bias_activation_fusion=bias_activation_fusion,
                openai_gelu=openai_gelu,
                onnx_safe=onnx_safe,
                activation=activation,
                bias=bias,
                transformer_block_type=transformer_block_type,
                normalization=normalization,
                layernorm_epsilon=layernorm_epsilon,
                persist_layer_norm=persist_layer_norm,
                sequence_parallel=sequence_parallel,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
                dropout=ffn_dropout,
            )

    def _get_bias_droput_add_func(self, transformer_block_type='pre_ln', position_after='attention'):
        """
        Returns a function that potentially fuses the dropout and bias addition.

        This function is particularly helpful for the normformer architecture that does not the fused kernel after attention layers, but can after the MLP.
        """
        # Normformer activations at this point have no bias vector since they've gone through another normalization layer.
        if transformer_block_type == 'normformer' and position_after == 'attention':
            bias_dropout_add_func = get_dropout_add(self.training)
        # Bias dropout add fused kernel
        elif self.bias and self.bias_dropout_add_fusion:
            if self.training:
                bias_dropout_add_func = bias_dropout_add_fused_train
            else:
                bias_dropout_add_func = bias_dropout_add_fused_inference
        # Bias dropout add non-fused kernel
        elif self.bias and not self.bias_dropout_add_fusion:
            bias_dropout_add_func = get_bias_dropout_add(self.training)
        # Dropout add non-fused kernel for a model without bias terms.
        else:
            bias_dropout_add_func = get_dropout_add(self.training)

        return bias_dropout_add_func

    def forward(
        self,
        hidden_states,
        attention_mask,
        encoder_output=None,
        enc_dec_attn_mask=None,
        layer_past=None,
        get_key_value=False,
        set_inference_key_value_memory=False,
        inference_max_sequence_len=None,
        rotary_pos_emb=None,  # list of positional embedding tensors, first one self attention, second one and third one are for cross attention (q, k)
        self_attention_relative_position_bias=None,
        cross_attention_relative_position_bias=None,
        checkpoint_core_attention=False,
    ):
        # Self attention.
        if rotary_pos_emb is not None:
            # self attention pos_emb is (q, q)
            self_attention_pos_emb = (rotary_pos_emb[0], rotary_pos_emb[0])
            cross_attention_pos_emb = (rotary_pos_emb[1], rotary_pos_emb[2])
        else:
            self_attention_pos_emb = None
            cross_attention_pos_emb = None

        if self.layer_type != LayerType.retrieval_decoder_after_self_attn:
            # hidden_states: [b, s, h]

            # Pre-LN: x -> LN -> MHA -> Residual -> LN -> MLP -> Residual
            # Post-LN: x -> MHA -> Residual -> LN -> MLP -> Residual -> LN
            # Normformer: x -> LN -> MHA -> LN -> Residual -> MLP (w/LN) -> Residual

            residual = hidden_states
            # Layer norm at the beginning of the transformer layer.
            if self.transformer_block_type in ['pre_ln', 'normformer']:
                hidden_states = self.input_layernorm(hidden_states)

            attention_output, attention_bias = self.self_attention(
                hidden_states,
                attention_mask,
                layer_past=layer_past,
                get_key_value=get_key_value,
                set_inference_key_value_memory=set_inference_key_value_memory,
                inference_max_sequence_len=inference_max_sequence_len,
                rotary_pos_emb=self_attention_pos_emb,
                relative_position_bias=self_attention_relative_position_bias,
                checkpoint_core_attention=checkpoint_core_attention,
            )

            if get_key_value:
                attention_output, presents = attention_output

            # If normformer, apply norm on the output of the self attention.
            if self.transformer_block_type == 'normformer':
                # Normformer normalization
                attention_output = (
                    attention_output + attention_bias if attention_bias is not None else attention_output
                )
                attention_output = self.post_attention_normformer_norm(attention_output)
                attention_bias = None

            # jit scripting for a nn.module (with dropout) is not
            # trigerring the fusion kernel. For now, we use two
            # different nn.functional routines to account for varying
            # dropout semantics during training and inference phases.

            bias_dropout_add_func = self._get_bias_droput_add_func(
                transformer_block_type=self.transformer_block_type, position_after='attention'
            )
            if attention_bias is not None:
                attention_bias = attention_bias.expand_as(residual)

            if self.is_adapter_available():
                adapter_1 = self.get_adapter_module(AdapterName.PRE_ATTN_ADAPTER)
                if adapter_1:
                    strategy = adapter_1.adapter_strategy
                    attention_output = self.forward_single_enabled_adapter_(
                        attention_output,
                        adapter_1,
                        adapter_name=AdapterName.PRE_ATTN_ADAPTER,
                        adapter_strategy=strategy,
                    )

            layernorm_input = bias_dropout_add_func(attention_output, attention_bias, residual, self.hidden_dropout)
            # print(f"Layer: {self.layer_number} Attention checksum {layernorm_input.sum()}")

            # Post-LN normalization after residual
            if self.transformer_block_type == 'post_ln':
                normalization_output = self.input_layernorm(layernorm_input)
                layernorm_input = normalization_output
            elif self.transformer_block_type in ['pre_ln', 'normformer']:
                # Layer norm post the self attention.
                normalization_output = self.post_attention_layernorm(layernorm_input)
        else:
            layernorm_input, normalization_output = hidden_states

        if self.layer_type == LayerType.decoder_pre_mlp:
            return layernorm_input, normalization_output

        if (
            self.layer_type == LayerType.decoder
            or self.layer_type == LayerType.retrieval_decoder
            or self.layer_type == LayerType.retrieval_encoder
            or self.layer_type == LayerType.retrieval_decoder_after_self_attn
        ):
            if (
                self.layer_type == LayerType.retrieval_decoder
                or self.layer_type == LayerType.retrieval_decoder_after_self_attn
            ):
                attention_output, attention_bias = self.inter_attention(
                    normalization_output,
                    enc_dec_attn_mask,
                    encoder_output=encoder_output,
                    rotary_pos_emb=cross_attention_pos_emb,
                    set_inference_key_value_memory=set_inference_key_value_memory,
                    inference_max_sequence_len=inference_max_sequence_len,
                    checkpoint_core_attention=checkpoint_core_attention,
                )
            else:

                attention_output, attention_bias = self.inter_attention(
                    normalization_output,
                    enc_dec_attn_mask,
                    encoder_output=encoder_output,
                    rotary_pos_emb=cross_attention_pos_emb,
                    relative_position_bias=cross_attention_relative_position_bias,
                    checkpoint_core_attention=checkpoint_core_attention,
                )

            # If normformer, apply norm on the output of the self attention.
            if self.transformer_block_type == 'normformer':
                # Normformer normalization
                attention_output = (
                    attention_output + attention_bias if attention_bias is not None else attention_output
                )
                attention_output = self.post_inter_attention_normformer_norm(attention_output)
                attention_bias = None

            residual = layernorm_input

            bias_dropout_add_func = self._get_bias_droput_add_func(
                transformer_block_type=self.transformer_block_type, position_after='attention'
            )

            layernorm_input = bias_dropout_add_func(attention_output, attention_bias, residual, self.hidden_dropout)
            # print(f"Layer: {self.layer_number} Cross-Attention checksum {layernorm_input.sum()}")
            normalization_output = self.post_inter_attention_layernorm(layernorm_input)
            # Post-LN normalization after residual
            if self.transformer_block_type == 'post_ln':
                layernorm_input = normalization_output
        # MLP.
        mlp_output, mlp_bias = self.mlp(normalization_output)
        if (
            self.is_adapter_available()
        ):  # TODO: (@adithyre) was able to move adapter_2 back to the end of the transformer after ptl 1.7 update.
            adapter_2 = self.get_adapter_module(AdapterName.POST_ATTN_ADAPTER)
            if adapter_2:
                strategy = adapter_2.adapter_strategy
                mlp_output = self.forward_single_enabled_adapter_(
                    mlp_output, adapter_2, adapter_name=AdapterName.POST_ATTN_ADAPTER, adapter_strategy=strategy
                )
        residual = layernorm_input

        bias_dropout_add_func = self._get_bias_droput_add_func(
            transformer_block_type=self.transformer_block_type, position_after='mlp'
        )

        output = bias_dropout_add_func(mlp_output, mlp_bias, residual, self.hidden_dropout)
        # print(f"Layer: {self.layer_number} MLP + Dropout + Residual checksum {output.sum()}")

        if self.transformer_block_type == 'post_ln':
            output = self.post_attention_layernorm(output)

        if get_key_value:
            output = [output, presents]

        return output


class ParallelTransformerLayer(ParallelTransformerLayer_):
    def __init__(
        self,
        init_method,
        output_layer_init_method,
        layer_number,
        hidden_size,
        ffn_hidden_size,
        num_attention_heads,
        layer_type=LayerType.encoder,
        self_attn_mask_type=AttnMaskType.padding,
        fp32_residual_connection=False,
        precision=16,
        apply_query_key_layer_scaling=True,
        kv_channels=None,
        layernorm_epsilon=1e-5,
        hidden_dropout=0.1,
        bias_dropout_add_fusion=True,
        persist_layer_norm=False,
        use_cpu_initialization=False,
        bias_activation_fusion=True,
        openai_gelu=False,
        onnx_safe=False,
        masked_softmax_fusion=True,
        attention_dropout=0.1,
        ffn_dropout=0.0,
        activation='gelu',
        megatron_legacy=False,
        bias=True,
        chunk_size=64,
        normalization='layernorm',
        transformer_block_type='pre_ln',
        headscale=False,
        activations_checkpoint_granularity=None,
        sequence_parallel=False,
        gradient_accumulation_fusion=False,
        normalize_attention_scores=True,
        num_moe_experts=1,
        moe_frequency=1,
        moe_dropout=0.0,
    ):
        super(ParallelTransformerLayer, self).__init__(
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            layer_number=layer_number,
            hidden_size=hidden_size,
            ffn_hidden_size=ffn_hidden_size,
            num_attention_heads=num_attention_heads,
            layer_type=layer_type,
            self_attn_mask_type=self_attn_mask_type,
            fp32_residual_connection=fp32_residual_connection,
            precision=precision,
            apply_query_key_layer_scaling=apply_query_key_layer_scaling,
            kv_channels=kv_channels,
            layernorm_epsilon=layernorm_epsilon,
            hidden_dropout=hidden_dropout,
            bias_dropout_add_fusion=bias_dropout_add_fusion,
            persist_layer_norm=persist_layer_norm,
            use_cpu_initialization=use_cpu_initialization,
            bias_activation_fusion=bias_activation_fusion,
            openai_gelu=openai_gelu,
            onnx_safe=onnx_safe,
            masked_softmax_fusion=masked_softmax_fusion,
            attention_dropout=attention_dropout,
            ffn_dropout=ffn_dropout,
            activation=activation,
            megatron_legacy=megatron_legacy,
            bias=bias,
            chunk_size=chunk_size,
            normalization=normalization,
            transformer_block_type=transformer_block_type,
            headscale=headscale,
            activations_checkpoint_granularity=activations_checkpoint_granularity,
            sequence_parallel=sequence_parallel,
            gradient_accumulation_fusion=gradient_accumulation_fusion,
            normalize_attention_scores=normalize_attention_scores,
            num_moe_experts=num_moe_experts,
            moe_frequency=moe_frequency,
            moe_dropout=moe_dropout,
        )

        if precision == 'bf16':
            self.dtype = torch.bfloat16
        elif int(precision) == 16:
            self.dtype = torch.float16
        elif int(precision) == 32:
            self.dtype = torch.float32
        else:
            raise ValueError

    def forward(
        self,
        hidden_states,
        attention_mask,
        encoder_output=None,
        enc_dec_attn_mask=None,
        rotary_pos_emb=None,
        layer_past=None,
        get_key_value=False,
        set_inference_key_value_memory=False,
        inference_max_sequence_len=None,
        self_attention_relative_position_bias=None,
        cross_attention_relative_position_bias=None,
        checkpoint_core_attention=False,
    ):
        if self.dtype == torch.float32:
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output,
                enc_dec_attn_mask,
                layer_past,
                get_key_value,
                set_inference_key_value_memory,
                inference_max_sequence_len,
                rotary_pos_emb,
                self_attention_relative_position_bias,
                cross_attention_relative_position_bias,
                checkpoint_core_attention,
            )
        with torch.autocast(device_type="cuda", dtype=self.dtype):
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output,
                enc_dec_attn_mask,
                layer_past,
                get_key_value,
                set_inference_key_value_memory,
                inference_max_sequence_len,
                rotary_pos_emb,
                self_attention_relative_position_bias,
                cross_attention_relative_position_bias,
                checkpoint_core_attention,
            )


class AutocastTransformerLayer(TransformerLayer):
    def __init__(
        self,
        hidden_size: int,
        ffn_hidden_size: int,
        layernorm_epsilon: float,
        num_attention_heads: int,
        init_method: Callable,
        output_layer_init_method: Callable,
        hidden_dropout: float,
        attention_dropout: float,
        layer_number: Optional[int] = None,
        kv_channels: Optional[int] = None,
        self_attn_mask_type: str = "causal",
        tp_group: Optional[Any] = None,
        tp_size: int = 1,
        params_dtype: torch.dtype = torch.float32,
        get_rng_state_tracker: Optional[Callable] = None,
        fuse_wgrad_accumulation: bool = False,
        apply_query_key_layer_scaling: bool = True,
        attention_softmax_in_fp32: bool = False,
        seq_length: Optional[int] = None,
        micro_batch_size: Optional[int] = None,
        sequence_parallel: bool = False,
        apply_residual_connection_post_layernorm: bool = False,
        output_layernorm: bool = False,
        layer_type: str = "encoder",
        drop_path_rate: float = 0,
        use_emha: bool = False,
        autocast_dtype: Any = 16,
    ) -> None:
        super().__init__(
            hidden_size=hidden_size,
            ffn_hidden_size=ffn_hidden_size,
            layernorm_epsilon=layernorm_epsilon,
            num_attention_heads=num_attention_heads,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            hidden_dropout=hidden_dropout,
            attention_dropout=attention_dropout,
            layer_number=layer_number,
            kv_channels=kv_channels,
            self_attn_mask_type=self_attn_mask_type,
            tp_group=tp_group,
            tp_size=tp_size,
            params_dtype=params_dtype,
            get_rng_state_tracker=get_rng_state_tracker,
            fuse_wgrad_accumulation=fuse_wgrad_accumulation,
            apply_query_key_layer_scaling=apply_query_key_layer_scaling,
            attention_softmax_in_fp32=attention_softmax_in_fp32,
            seq_length=seq_length,
            micro_batch_size=micro_batch_size,
            sequence_parallel=sequence_parallel,
            apply_residual_connection_post_layernorm=apply_residual_connection_post_layernorm,
            output_layernorm=output_layernorm,
            layer_type=layer_type,
            drop_path_rate=drop_path_rate,
            set_parallel_mode=tp_size > 1,
            fuse_qkv_params=True,
        )
        # use_emha=use_emha,

        if autocast_dtype == 32:
            self.dtype = torch.float32
        elif autocast_dtype == 16:
            self.dtype = torch.float16
        elif autocast_dtype == 'bf16':
            self.dtype = torch.bfloat16
        else:
            raise ValueError

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: torch.Tensor,
        encoder_output: Optional[torch.Tensor] = None,
        enc_dec_attn_mask: Optional[torch.Tensor] = None,
        inference_params: Optional[Any] = None,
        is_first_microbatch: Optional[bool] = None,
        checkpoint_core_attention: Optional[bool] = False,
    ) -> torch.Tensor:
        if self.dtype == torch.float32:
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output=encoder_output,
                enc_dec_attn_mask=enc_dec_attn_mask,
                inference_params=inference_params,
                is_first_microbatch=is_first_microbatch,
                checkpoint_core_attention=checkpoint_core_attention,
            )
        with torch.autocast(device_type="cuda", dtype=self.dtype):
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output=encoder_output,
                enc_dec_attn_mask=enc_dec_attn_mask,
                inference_params=inference_params,
                is_first_microbatch=is_first_microbatch,
                checkpoint_core_attention=checkpoint_core_attention,
            )


class ParallelTransformer(MegatronModule):
    """Transformer class."""

    def __init__(
        self,
        init_method,
        output_layer_init_method,
        num_layers,
        hidden_size,
        ffn_hidden_size,
        num_attention_heads,
        apply_query_key_layer_scaling=True,
        kv_channels=None,
        layer_type=LayerType.encoder,  # it can be a list of types or single type
        self_attn_mask_type=AttnMaskType.padding,
        pre_process=True,
        post_process=True,
        precision=16,
        fp32_residual_connection=False,
        activations_checkpoint_method=None,
        activations_checkpoint_num_layers=None,
        layernorm_epsilon=1e-5,
        hidden_dropout=0.1,
        attention_dropout=0.1,
        ffn_dropout=0.0,
        use_cpu_initialization=False,
        bias_activation_fusion=True,
        bias_dropout_add_fusion=True,
        masked_softmax_fusion=True,
        gradient_accumulation_fusion=False,
        persist_layer_norm=False,
        openai_gelu=False,
        onnx_safe=False,
        activation='gelu',
        model_type=ModelType.encoder_or_decoder,
        megatron_legacy=False,
        bias=True,
        chunk_size=64,
        normalization='layernorm',
        transformer_block_type='pre_ln',
        headscale=False,
        layer_number_offset=0,  # this is use only for attention norm_factor scaling
        activations_checkpoint_granularity=None,
        activations_checkpoint_layers_per_pipeline=None,
        sequence_parallel=False,
        transformer_engine=False,
        fp8=False,
        fp8_e4m3=False,
        fp8_hybrid=False,
        fp8_margin=0,
        fp8_interval=1,
        fp8_amax_history_len=1,
        fp8_amax_compute_algo='most_recent',
        reduce_amax=True,
        use_emha=False,
        normalize_attention_scores=True,
        num_moe_experts=1,
        moe_frequency=1,
        moe_dropout=0.0,
    ):
        super(ParallelTransformer, self).__init__()

        if kv_channels is None:
            assert (
                hidden_size % num_attention_heads == 0
            ), 'hidden_size must be divisible by num_attention_heads if kv_channels is None'
            kv_channels = hidden_size // num_attention_heads

        self.fp32_residual_connection = fp32_residual_connection
        self.pre_process = pre_process
        self.post_process = post_process
        self.input_tensor = None
        self.self_attn_mask_type = self_attn_mask_type
        self.model_type = model_type
        self.normalization = normalization
        self.transformer_block_type = transformer_block_type
        self.layer_type = layer_type

        self.activations_checkpoint_method = activations_checkpoint_method
        self.activations_checkpoint_num_layers = activations_checkpoint_num_layers
        self.activations_checkpoint_granularity = activations_checkpoint_granularity
        self.activations_checkpoint_layers_per_pipeline = activations_checkpoint_layers_per_pipeline

        if self.activations_checkpoint_granularity:
            if self.activations_checkpoint_granularity == 'selective':
                if self.activations_checkpoint_method == 'uniform':
                    logging.info(
                        (
                            f'Using uniform activation checkpointing with granularity selective forces all layers to use checkpointing.'
                        )
                    )
                elif self.activations_checkpoint_method == 'block':
                    logging.info(
                        (
                            f'Using block activation checkpointing requires activations_checkpoint_num_layers to be set.'
                            f'Got: {self.activations_checkpoint_num_layers}. Setting to 1 by default.'
                        )
                    )
                else:
                    raise ValueError(
                        f'activations_checkpoint_method should be "uniform" or "block" when using granularity selective.'
                    )
            elif self.activations_checkpoint_granularity == 'full':
                if self.activations_checkpoint_method in ['uniform', 'block']:
                    if not self.activations_checkpoint_num_layers:
                        logging.info(
                            (
                                f'Using uniform or block activation checkpointing requires activations_checkpoint_num_layers to be set.'
                                f'Got: {self.activations_checkpoint_num_layers}. Setting to 1 by default.'
                            )
                        )
                else:
                    raise ValueError(
                        f'activations_checkpoint_method should be "uniform" or "block" when using granularity full.'
                    )
            else:
                raise ValueError(f'activations_checkpoint_granularity should be "selective" or "full".')

        self.sequence_parallel = sequence_parallel
        self.transformer_engine = transformer_engine
        self.fp8 = fp8
        self.fp8_e4m3 = fp8_e4m3
        self.fp8_hybrid = fp8_hybrid
        self.fp8_margin = fp8_margin
        self.fp8_interval = fp8_interval
        self.fp8_amax_history_len = fp8_amax_history_len
        self.fp8_amax_compute_algo = fp8_amax_compute_algo
        self.reduce_amax = reduce_amax

        self.fp8_recipe = None

        if self.fp8:
            if self.fp8_e4m3:
                fp8_format = recipe.Format.E4M3
            elif self.fp8_hybrid:
                fp8_format = recipe.Format.HYBRID
            self.fp8_recipe = recipe.DelayedScaling(
                margin=self.fp8_margin,
                interval=self.fp8_interval,
                fp8_format=fp8_format,
                amax_history_len=self.fp8_amax_history_len,
                amax_compute_algo=self.fp8_amax_compute_algo,
                reduce_amax=reduce_amax,
            )

        self.is_first_microbatch = True
        self.microbatch_count = 0  # transformer engine forward needs to know if it is working on the first microbatch
        self.checkpoint_core_attention = (
            activations_checkpoint_granularity == 'selective'
        )  # transformer engine forward allows for more granular selective checkpointing

        if self.model_type == ModelType.encoder_or_decoder:
            assert (
                num_layers % parallel_state.get_pipeline_model_parallel_world_size() == 0
            ), 'num_layers must be divisible by pipeline_model_parallel_size'

        assert moe_frequency <= num_layers, 'MoE frequency must be <= number of transformer layers'
        # TODO: Add similar assert for encoder-decoder.

        self.num_layers = self.get_num_layers(num_layers)
        # Transformer layers.
        def build_layer(layer_number):
            if isinstance(layer_type, list):
                lt = layer_type[layer_number - 1]
            else:
                lt = layer_type

            if self.transformer_engine:
                return AutocastTransformerLayer(
                    hidden_size=hidden_size,
                    ffn_hidden_size=ffn_hidden_size,
                    layernorm_epsilon=layernorm_epsilon,
                    num_attention_heads=num_attention_heads,
                    init_method=init_method,
                    output_layer_init_method=output_layer_init_method,
                    hidden_dropout=hidden_dropout,
                    attention_dropout=attention_dropout,
                    layer_number=layer_number + layer_number_offset,
                    kv_channels=kv_channels,
                    self_attn_mask_type=self_attn_mask_type.name,
                    tp_size=parallel_state.get_tensor_model_parallel_world_size(),
                    params_dtype=torch.float32,  # dtype params are initialized in
                    get_rng_state_tracker=tensor_parallel.random.get_cuda_rng_tracker,
                    fuse_wgrad_accumulation=gradient_accumulation_fusion,
                    apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                    seq_length=None,  # used for jit warmup
                    micro_batch_size=None,  # used for jit warmup
                    sequence_parallel=sequence_parallel,
                    apply_residual_connection_post_layernorm=False,
                    autocast_dtype=precision,
                    use_emha=use_emha,
                )
            else:
                return ParallelTransformerLayer(
                    init_method=init_method,
                    output_layer_init_method=output_layer_init_method,
                    layer_number=layer_number + layer_number_offset,
                    hidden_size=hidden_size,
                    ffn_hidden_size=ffn_hidden_size,
                    num_attention_heads=num_attention_heads,
                    apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                    kv_channels=kv_channels,
                    layer_type=lt,
                    self_attn_mask_type=self_attn_mask_type,
                    precision=precision,
                    fp32_residual_connection=fp32_residual_connection,
                    layernorm_epsilon=layernorm_epsilon,
                    hidden_dropout=hidden_dropout,
                    attention_dropout=attention_dropout,
                    ffn_dropout=ffn_dropout,
                    use_cpu_initialization=use_cpu_initialization,
                    bias_activation_fusion=bias_activation_fusion,
                    bias_dropout_add_fusion=bias_dropout_add_fusion,
                    masked_softmax_fusion=masked_softmax_fusion,
                    gradient_accumulation_fusion=gradient_accumulation_fusion,
                    persist_layer_norm=persist_layer_norm,
                    openai_gelu=openai_gelu,
                    onnx_safe=onnx_safe,
                    activation=activation,
                    megatron_legacy=megatron_legacy,
                    bias=bias,
                    chunk_size=chunk_size,
                    normalization=normalization,
                    transformer_block_type=transformer_block_type,
                    headscale=headscale,
                    activations_checkpoint_granularity=activations_checkpoint_granularity,
                    sequence_parallel=sequence_parallel,
                    normalize_attention_scores=normalize_attention_scores,
                    num_moe_experts=num_moe_experts,
                    moe_frequency=moe_frequency,
                    moe_dropout=moe_dropout,
                )

        if parallel_state.get_virtual_pipeline_model_parallel_world_size() is not None:
            assert num_layers % parallel_state.get_virtual_pipeline_model_parallel_world_size() == 0, (
                'num_layers_per_stage must be divisible by ' 'virtual_pipeline_model_parallel_size'
            )

            assert self.model_type.value != 2, f'virtual pipeline parallel currently only supported for GPT'

            # Number of layers in each model chunk is the number of layers in the stage,
            # divided by the number of model chunks in a stage.
            self.num_layers = self.num_layers // parallel_state.get_virtual_pipeline_model_parallel_world_size()
            # With 8 layers, 2 stages, and 4 model chunks, we want an assignment of
            # layers to stages like (each list is a model chunk):
            # Stage 0: [0]  [2]  [4]  [6]
            # Stage 1: [1]  [3]  [5]  [7]
            # With 8 layers, 2 stages, and 2 virtual stages, we want an assignment of
            # layers to stages like (each list is a model chunk):
            # Stage 0: [0, 1]  [4, 5]
            # Stage 1: [2, 3]  [6, 7]
            offset = parallel_state.get_virtual_pipeline_model_parallel_rank() * (
                num_layers // parallel_state.get_virtual_pipeline_model_parallel_world_size()
            ) + (parallel_state.get_pipeline_model_parallel_rank() * self.num_layers)
        else:
            # Each stage gets a contiguous set of layers.
            if (
                self.model_type == ModelType.encoder_and_decoder
                and parallel_state.get_pipeline_model_parallel_world_size() > 1
            ):
                pipeline_rank = parallel_state.get_pipeline_model_parallel_rank()
                if layer_type == LayerType.encoder:
                    offset = pipeline_rank * self.num_layers
                else:
                    num_ranks_in_enc = parallel_state.get_pipeline_model_parallel_split_rank()
                    offset = (pipeline_rank - num_ranks_in_enc) * self.num_layers
            else:
                offset = parallel_state.get_pipeline_model_parallel_rank() * self.num_layers

        self.layers = torch.nn.ModuleList([build_layer(i + 1 + offset) for i in range(self.num_layers)])

        if self.post_process and self.transformer_block_type != 'post_ln':
            # Final layer norm before output.
            if normalization == 'layernorm':
                self.final_layernorm = get_layer_norm(
                    hidden_size, layernorm_epsilon, persist_layer_norm, sequence_parallel=sequence_parallel
                )
            elif normalization == 'layernorm1p':
                self.final_layernorm = LayerNorm1P(
                    hidden_size, layernorm_epsilon, sequence_parallel_enabled=sequence_parallel
                )
            else:
                self.final_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

    def _get_layer(self, layer_number):
        return self.layers[layer_number]

    def get_num_layers(self, num_layers):
        """Compute the number of transformer layers resident on the current rank."""
        if parallel_state.get_pipeline_model_parallel_world_size() > 1:
            if self.model_type == ModelType.encoder_and_decoder:
                assert parallel_state.get_pipeline_model_parallel_split_rank() is not None
                num_ranks_in_encoder = parallel_state.get_pipeline_model_parallel_split_rank()
                num_ranks_in_decoder = parallel_state.get_pipeline_model_parallel_world_size() - num_ranks_in_encoder
                if self.layer_type == LayerType.encoder:
                    assert (
                        num_layers % num_ranks_in_encoder == 0
                    ), 'num_layers must be divisible by number of ranks given to encoder'
                elif self.layer_type == LayerType.decoder:
                    assert (
                        num_layers % num_ranks_in_decoder == 0
                    ), 'num_layers must be divisible by number of ranks given to decoder'
                else:
                    raise ValueError(f"Unknown layer type {self.layer_type}")

                if parallel_state.is_pipeline_stage_before_split():
                    num_layers = num_layers // num_ranks_in_encoder
                else:
                    num_layers = num_layers // num_ranks_in_decoder
            elif self.model_type == ModelType.encoder_or_decoder:
                assert (
                    num_layers % parallel_state.get_pipeline_model_parallel_world_size() == 0
                ), 'num_layers must be divisible by pipeline_model_parallel_size'
                num_layers = num_layers // parallel_state.get_pipeline_model_parallel_world_size()

        return num_layers

    def _checkpointed_forward(
        self,
        hidden_states,
        attention_mask,
        encoder_output,
        enc_dec_attn_mask,
        rotary_pos_emb,
        self_attention_relative_position_bias,
        cross_attention_relative_position_bias,
        checkpoint_activations_all_layers,
    ):
        """Forward method with activation checkpointing."""

        def custom(start, end):
            if self.transformer_engine:

                def custom_forward(*inputs):
                    hidden_states = inputs[0]
                    attention_mask = inputs[1]
                    encoder_output = inputs[2]
                    enc_dec_attn_mask = inputs[3]
                    for index in range(start, end):
                        layer = self._get_layer(index)
                        hidden_states = layer(
                            hidden_states,
                            attention_mask,
                            encoder_output=encoder_output,
                            enc_dec_attn_mask=enc_dec_attn_mask,
                            inference_params=None,
                            is_first_microbatch=self.is_first_microbatch,
                            checkpoint_core_attention=False,
                        )

                    return hidden_states

            else:

                def custom_forward(*inputs):
                    if len(inputs) == 9:
                        hidden_states = inputs[0]
                        attention_mask = inputs[1]
                        encoder_output = inputs[2]
                        enc_dec_attn_mask = inputs[3]
                        rotary_pos_emb = (inputs[4], inputs[5], inputs[6])
                        self_attention_relative_position_bias = inputs[7]
                        cross_attention_relative_position_bias = inputs[8]
                    elif len(inputs) == 10:
                        hidden_states = (inputs[0], inputs[1])
                        attention_mask = inputs[2]
                        encoder_output = inputs[3]
                        enc_dec_attn_mask = inputs[4]
                        rotary_pos_emb = (inputs[5], inputs[6], inputs[7])
                        self_attention_relative_position_bias = inputs[8]
                        cross_attention_relative_position_bias = inputs[9]
                    else:
                        hidden_states = inputs[0]
                        attention_mask = inputs[1]
                        encoder_output = inputs[2]
                        enc_dec_attn_mask = inputs[3]
                        rotary_pos_emb = inputs[4]
                        self_attention_relative_position_bias = inputs[5]
                        cross_attention_relative_position_bias = inputs[6]
                    for index in range(start, end):
                        layer = self._get_layer(index)
                        hidden_states = layer(
                            hidden_states=hidden_states,
                            attention_mask=attention_mask,
                            encoder_output=encoder_output,
                            enc_dec_attn_mask=enc_dec_attn_mask,
                            rotary_pos_emb=rotary_pos_emb,
                            self_attention_relative_position_bias=self_attention_relative_position_bias,
                            cross_attention_relative_position_bias=cross_attention_relative_position_bias,
                        )
                        if isinstance(hidden_states, tuple):
                            pass
                        else:
                            hidden_states = hidden_states.contiguous()
                    return hidden_states

            return custom_forward

        # Make sure memory is freed.
        tensor_parallel.reset_checkpointed_activations_memory_buffer()

        if self.activations_checkpoint_method == 'uniform':
            # Uniformly divide the total number of Transformer layers and checkpoint
            # the input activation of each divided chunk.
            # A method to further reduce memory usage reducing checkpoints.
            l = 0
            while l < self.num_layers:
                if isinstance(hidden_states, tuple):
                    hidden_tuple = (hidden_states[0], hidden_states[1])
                else:
                    hidden_tuple = (hidden_states,)
                middle_tuple = (
                    attention_mask,
                    encoder_output,
                    enc_dec_attn_mask,
                )

                if rotary_pos_emb is None:
                    rot_tuple = (rotary_pos_emb,)
                else:
                    rot_tuple = (rotary_pos_emb[0], rotary_pos_emb[1], rotary_pos_emb[2])

                final_tuple = (self_attention_relative_position_bias, cross_attention_relative_position_bias)
                arg_tuple = hidden_tuple + middle_tuple + rot_tuple + final_tuple

                if self.transformer_engine:
                    hidden_states = te_checkpoint(
                        custom(l, l + self.activations_checkpoint_num_layers),
                        False,
                        tensor_parallel.random.get_cuda_rng_tracker,
                        parallel_state.get_tensor_model_parallel_group(),
                        *arg_tuple,
                    )
                else:
                    hidden_states = tensor_parallel.checkpoint(
                        custom(l, l + self.activations_checkpoint_num_layers), False, *arg_tuple
                    )
                l += self.activations_checkpoint_num_layers
        elif self.activations_checkpoint_method == 'block':
            # When pipeline-parallel size > 1 and 'num_micro_batches_with_partial_activation_checkpoints' = int,
            # pipeline scheduling can force to checkpoint all layers or partial layers in a micro-batch.
            if checkpoint_activations_all_layers:
                activations_checkpoint_num_layers = self.num_layers
            else:
                activations_checkpoint_num_layers = self.activations_checkpoint_num_layers
                if (
                    parallel_state.get_pipeline_model_parallel_world_size() > 0
                    and self.activations_checkpoint_layers_per_pipeline is not None
                ):
                    # Decrease the number of layers to checkpoint at later pipeline stages
                    activations_checkpoint_num_layers -= int(
                        parallel_state.get_pipeline_model_parallel_rank()
                        * self.activations_checkpoint_layers_per_pipeline
                    )
            # Checkpoint the input activation of only a set number of individual
            # Transformer layers and skip the rest.
            # A method fully use the device memory removing redundant re-computation.
            for l in range(self.num_layers):
                if isinstance(hidden_states, tuple):
                    hidden_tuple = (hidden_states[0], hidden_states[1])
                else:
                    hidden_tuple = (hidden_states,)
                middle_tuple = (
                    attention_mask,
                    encoder_output,
                    enc_dec_attn_mask,
                )

                if rotary_pos_emb is None:
                    rot_tuple = (rotary_pos_emb,)
                else:
                    rot_tuple = (rotary_pos_emb[0], rotary_pos_emb[1], rotary_pos_emb[2])

                final_tuple = (self_attention_relative_position_bias, cross_attention_relative_position_bias)
                arg_tuple = hidden_tuple + middle_tuple + rot_tuple + final_tuple

                if l < activations_checkpoint_num_layers:
                    if self.transformer_engine:
                        hidden_states = te_checkpoint(
                            custom(l, l + 1),
                            False,
                            tensor_parallel.random.get_cuda_rng_tracker,
                            parallel_state.get_tensor_model_parallel_group(),
                            *arg_tuple,
                        )
                    else:
                        hidden_states = tensor_parallel.checkpoint(custom(l, l + 1), False, *arg_tuple)
                else:
                    hidden_states = custom(l, l + 1)(*arg_tuple)
        else:
            raise ValueError("Invalid activation checkpoint method.")

        return hidden_states

    def set_input_tensor(self, input_tensor):
        """Set input tensor to be used instead of forward()'s input.

        When doing pipeline parallelism the input from the previous
        stage comes from communication, not from the input, so the
        model's forward_step_func won't have it. This function is thus
        used by internal code to bypass the input provided by the
        forward_step_func"""
        self.input_tensor = input_tensor

    def forward(
        self,
        hidden_states,
        attention_mask,
        layer_past=None,
        get_key_value=False,
        encoder_output=None,
        enc_dec_attn_mask=None,
        set_inference_key_value_memory=False,
        inference_max_sequence_len=None,
        rotary_pos_emb=None,  # list of positional embedding tensors, first one self attention, second one and third one are for cross attention (q, k)
        retrieved_emb=None,  # tensor of retrieved embedding of shape [b, k, r, n, d]
        self_attention_relative_position_bias=None,
        cross_attention_relative_position_bias=None,
        checkpoint_activations_all_layers=None,
    ):
        # Checks.
        if inference_max_sequence_len:
            assert self.activations_checkpoint_method is None, 'inference does not work with activation checkpointing'

        if layer_past is not None:
            assert get_key_value, 'for not None values in layer_past, ' 'expected get_key_value to be set'
        if get_key_value:
            assert self.activations_checkpoint_method is None, (
                'get_key_value does not work with ' 'activation checkpointing'
            )

        if not self.pre_process:
            # See set_input_tensor()
            hidden_states = self.input_tensor

        # TODO: @Yi Dong, what should this be?
        if retrieved_emb is not None:
            assert len(retrieved_emb.shape) == 5
            # this is retrieval decoder, need special transpose
            encoder_output = rearrange(retrieved_emb, 'b k r n d -> k r n b d').contiguous()

        """
        is_first_microbatch is an optimization parameter for transformer engine.
        It indicates if the current step in the forward pass is the first in a gradient accumulation cycle.
        If set, FP8 weights are cached and some minor optimizations are applied to fuse_wgrad_accumulation
        """
        from apex.transformer.pipeline_parallel.utils import _GLOBAL_NUM_MICROBATCHES_CALCULATOR

        num_micro_batches = getattr(_GLOBAL_NUM_MICROBATCHES_CALCULATOR, 'num_micro_batches', 1)

        if self.sequence_parallel:
            rng_context = tensor_parallel.random.get_cuda_rng_tracker().fork()
        else:
            rng_context = nullcontext()

        with rng_context:
            # fp8_autocast will not do anything if TE or FP8 isn't used
            fp8_group = None
            if parallel_state.model_parallel_is_initialized():
                fp8_group = parallel_state.get_data_parallel_group()

            if HAVE_TE:
                # if TE is installed but fp8 is not available then this will do nothing
                fp8_context = fp8_autocast(enabled=self.fp8, fp8_recipe=self.fp8_recipe, fp8_group=fp8_group)

            else:
                fp8_context = nullcontext()

            with fp8_context:
                if self.activations_checkpoint_granularity == 'full' and self.activations_checkpoint_num_layers > 0:
                    hidden_states = self._checkpointed_forward(
                        hidden_states,
                        attention_mask,
                        encoder_output,
                        enc_dec_attn_mask,
                        rotary_pos_emb,
                        self_attention_relative_position_bias,
                        cross_attention_relative_position_bias,
                        checkpoint_activations_all_layers,
                    )
                else:
                    if get_key_value:
                        presents = []

                    for index in range(self.num_layers):
                        layer = self._get_layer(index)
                        past = None

                        if layer_past is not None:
                            past = layer_past[index]

                        if self.activations_checkpoint_granularity == 'selective':
                            # When pipeline-parallel size > 1 and 'num_micro_batches_with_partial_activation_checkpoints' = int,
                            # pipeline scheduling can force to checkpoint all layers or partial layers in a micro-batch.
                            if (
                                checkpoint_activations_all_layers == True
                                or self.activations_checkpoint_method == 'uniform'
                            ):
                                checkpoint_core_attention = True
                            elif self.activations_checkpoint_method == 'block':
                                activations_checkpoint_num_layers = self.activations_checkpoint_num_layers
                                # Decrease the number of layers to checkpoint at later pipeline stages
                                if self.activations_checkpoint_layers_per_pipeline is not None:
                                    activations_checkpoint_num_layers -= int(
                                        parallel_state.get_pipeline_model_parallel_rank()
                                        * self.activations_checkpoint_layers_per_pipeline
                                    )
                                checkpoint_core_attention = index < activations_checkpoint_num_layers
                        else:
                            checkpoint_core_attention = False

                        if self.transformer_engine:

                            inference_params = None

                            hidden_states = layer(
                                hidden_states,
                                attention_mask,
                                encoder_output=encoder_output,
                                enc_dec_attn_mask=enc_dec_attn_mask,
                                inference_params=inference_params,
                                is_first_microbatch=self.is_first_microbatch,
                                checkpoint_core_attention=checkpoint_core_attention,
                            )

                        else:
                            hidden_states = layer(
                                hidden_states,
                                attention_mask,
                                encoder_output=encoder_output,
                                enc_dec_attn_mask=enc_dec_attn_mask,
                                layer_past=past,
                                get_key_value=get_key_value,
                                set_inference_key_value_memory=set_inference_key_value_memory,
                                inference_max_sequence_len=inference_max_sequence_len,
                                rotary_pos_emb=rotary_pos_emb,
                                self_attention_relative_position_bias=self_attention_relative_position_bias,
                                cross_attention_relative_position_bias=cross_attention_relative_position_bias,
                                checkpoint_core_attention=checkpoint_core_attention,
                            )

        # Skip counter update for eval and activation checkpointing
        if torch.is_grad_enabled() and self.training:
            self.microbatch_count += 1
            if self.microbatch_count % num_micro_batches == 0:
                self.microbatch_count = 0
                self.is_first_microbatch = True
            else:
                self.is_first_microbatch = False

        output = hidden_states

        # Final layer norm.
        if self.post_process:
            # only apply the final_layernorm for pre-ln
            if self.transformer_block_type != 'post_ln':
                output = self.final_layernorm(hidden_states)

        if get_key_value:
            output = [output, presents]

        return output
