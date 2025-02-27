# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import math
from dataclasses import dataclass
from typing import Optional, OrderedDict, Tuple, Union

import torch
import torch.nn.functional as F
from torch import nn
from torch.distributed import all_gather as all_gather_no_backprop
from torch.distributed.nn.functional import all_gather as all_gather_with_backprop


@dataclass
class ContrastiveLossOutput(OrderedDict):
    loss: torch.Tensor
    image_logits: torch.Tensor
    text_logits: torch.Tensor
    image_loss: torch.Tensor
    text_loss: torch.Tensor


def _gather_embeddings_and_labels(
    image_embeddings: torch.Tensor,
    text_embeddings: torch.Tensor,
    backprop_in_gather: bool = True,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    if not torch.distributed.is_available() or not torch.distributed.is_initialized():
        labels = torch.arange(image_embeddings.size(0), device=image_embeddings.device)
        return image_embeddings, text_embeddings, labels

    # image_embeddings has shape [local_batch_size, embedding_dim]
    local_batch_size = image_embeddings.size(0)

    world_size = torch.distributed.get_world_size()

    # This uses the all_gather from torch.distributed.nn.functional,
    # which backpropagates gradients to all workers
    if backprop_in_gather:
        img_embeddings_all_gpus = all_gather_with_backprop(image_embeddings)
        text_embeddings_all_gpus = all_gather_with_backprop(text_embeddings)

    # Otherwise just backprop to the current worker
    # This means that the image gradients on a given worker will only
    # consider the text samples from the same worker
    else:
        text_embeddings_all_gpus = [
            torch.zeros_like(text_embeddings) for _ in range(world_size)
        ]
        img_embeddings_all_gpus = [
            torch.zeros_like(image_embeddings) for _ in range(world_size)
        ]
        all_gather_no_backprop(img_embeddings_all_gpus, image_embeddings)
        all_gather_no_backprop(text_embeddings_all_gpus, text_embeddings)

    labels = local_batch_size * torch.distributed.get_rank() + torch.arange(
        local_batch_size, device=image_embeddings.device
    )

    return (
        torch.cat(img_embeddings_all_gpus),
        torch.cat(text_embeddings_all_gpus),
        labels,
    )


def contrastive_loss_with_temperature(
    image_embeddings: torch.Tensor,
    text_embeddings: torch.Tensor,
    logit_scale: nn.Parameter,
    mask: Optional[torch.Tensor] = None,
    backprop_in_gather: bool = True,
) -> ContrastiveLossOutput:
    """Functional component for the ContrastiveLossWithTemperature. Please
    check the class for more details

    Args:
        image_embeddings (Tensor): Tensor containing image features.
            (In the CLIP model, these are the outputs of the image encoder.)
        text_embeddings (Tensor): Tensor containing text features.
            (In the CLIP model, these are the outputs of the text encoder.)
        logit_scale (nn.Parameter): Parameter with value of log of the learned temperature
        mask (Optional[torch.Tensor], optional): If certain elements of the inputs shouldn't
            be considered in the loss calculation use this option to pass a boolean
            mask. Size is (BatchSize,). Defaults to None.
        backprop_in_gather (bool): Whether to backpropagate the gradients from
            all_gather to all workers (versus just the local worker).

    Returns:
        ContrastiveLossOutput: instance of ContrastiveLossOutput with all of the
            relevant fields.
    """

    # this temperature implementation follows CLIP Figure 3
    temperature = torch.exp(logit_scale)

    (
        img_embeddings_all_gpus,
        text_embeddings_all_gpus,
        labels,
    ) = _gather_embeddings_and_labels(
        image_embeddings, text_embeddings, backprop_in_gather
    )

    # logits_per_image has shape [local_batch_size, global_batch_size]
    logits_per_image = (
        torch.matmul(image_embeddings, text_embeddings_all_gpus.transpose(0, 1))
        * temperature
    )
    logits_per_text = (
        torch.matmul(text_embeddings, img_embeddings_all_gpus.transpose(0, 1))
        * temperature
    )

    if mask is not None:
        logits_per_image = logits_per_image[mask]
        logits_per_text = logits_per_text[mask]
        labels = labels[mask]

    loss_i = F.cross_entropy(logits_per_image, labels)
    loss_t = F.cross_entropy(logits_per_text, labels)
    loss = (loss_i + loss_t) / 2

    return ContrastiveLossOutput(
        loss=loss,
        image_logits=logits_per_image,
        text_logits=logits_per_text,
        image_loss=loss_i,
        text_loss=loss_t,
    )


DEFAULT_LOGIT_SCALE = nn.Parameter(math.log(1 / 0.07) * torch.ones([]))


class ContrastiveLossWithTemperature(nn.Module):
    """Contrastive loss with a temperature parameter, as used in CLIP and FLAVA.
    CLIP: https://arxiv.org/pdf/2103.00020.pdf
    FLAVA: https://arxiv.org/pdf/2112.04482.pdf


    A contrastive loss over pairs of image and text embeddings. For each image
    embedding, we compute a weighted cosine similarity with all text embeddings,
    then calculate the cross entropy loss against the true (image, text) pairing.
    Each text embedding is evaluated against all image embeddings similarly.
    The batch's loss is the average cross entropy over all image and text embeddings
    in the batch.

    Temperature is a learned parameter clamped to ``[1, 100]`` and
    initialized to 1 / 0.07 as in the CLIP paper.


    Args:
        logit_scale (Union[float, nn.Module]): Log of the learnable temperature parameter value
            A nn.Parameter instantiation can also be passed directly in case parent class
            is handling the initialization.

    Inputs: image_embeddings (Tensor): Tensor containing image features.
                (In the CLIP model, these are the outputs of the image encoder.)
            text_embeddings (Tensor): Tensor containing text features.
                (In the CLIP model, these are the outputs of the text encoder.)
            backprop_in_gather (bool): Whether to backpropagate the gradients from
                all_gather to all workers (versus just the local worker).
    """

    def __init__(self, logit_scale: Union[float, nn.Parameter] = DEFAULT_LOGIT_SCALE):
        super().__init__()

        # If already initialized, set to what was passed
        if isinstance(logit_scale, nn.Parameter):
            self.logit_scale = logit_scale
        else:
            self.logit_scale = nn.Parameter(logit_scale * torch.ones([]))

    def forward(
        self,
        image_embeddings: torch.Tensor,
        text_embeddings: torch.Tensor,
        backprop_in_gather: bool = True,
    ):

        # Note: we clamp to 4.6052 = ln(100), as in the original paper.
        self.logit_scale.data.clamp_(0, 4.6052)
        return contrastive_loss_with_temperature(
            image_embeddings=image_embeddings,
            text_embeddings=text_embeddings,
            logit_scale=self.logit_scale,
            backprop_in_gather=backprop_in_gather,
        ).loss
