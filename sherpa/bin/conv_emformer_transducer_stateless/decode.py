# Copyright      2022  Xiaomi Corp.        (authors: Fangjun Kuang
#                                                    Zengwei Yao)
# See LICENSE for clarification regarding multiple authors
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

import math
from typing import List, Optional, Tuple

import k2
import torch
from kaldifeat import FbankOptions, OnlineFbank, OnlineFeature


def unstack_states(
    states: Tuple[List[List[torch.Tensor]], List[torch.Tensor]]
) -> List[Tuple[List[List[torch.Tensor]], List[torch.Tensor]]]:
    """Unstack the emformer state corresponding to a batch of utterances
    into a list of states, where the i-th entry is the state from the i-th
    utterance in the batch.

    Args:
      states:
        A tuple of 2 elements.
        ``states[0]`` is the attention caches of a batch of utterance.
        ``states[1]`` is the convolution caches of a batch of utterance.
        ``len(states[0])`` and ``len(states[1])`` both eqaul to number of layers.  # noqa

    Returns:
      A list of states.
      ``states[i]`` is a tuple of 2 elements of i-th utterance.
      ``states[i][0]`` is the attention caches of i-th utterance.
      ``states[i][1]`` is the convolution caches of i-th utterance.
      ``len(states[i][0])`` and ``len(states[i][1])`` both eqaul to number of layers.  # noqa
    """

    attn_caches, conv_caches = states
    batch_size = conv_caches[0].size(0)
    num_layers = len(attn_caches)

    list_attn_caches = [None] * batch_size
    for i in range(batch_size):
        list_attn_caches[i] = [[] for _ in range(num_layers)]
    for li, layer in enumerate(attn_caches):
        for s in layer:
            s_list = s.unbind(dim=1)
            for bi, b in enumerate(list_attn_caches):
                b[li].append(s_list[bi])

    list_conv_caches = [None] * batch_size
    for i in range(batch_size):
        list_conv_caches[i] = [None] * num_layers
    for li, layer in enumerate(conv_caches):
        c_list = layer.unbind(dim=0)
        for bi, b in enumerate(list_conv_caches):
            b[li] = c_list[bi]

    ans = [None] * batch_size
    for i in range(batch_size):
        ans[i] = [list_attn_caches[i], list_conv_caches[i]]

    return ans


def stack_states(
    state_list: List[Tuple[List[List[torch.Tensor]], List[torch.Tensor]]]
) -> Tuple[List[List[torch.Tensor]], List[torch.Tensor]]:
    """Stack list of emformer states that correspond to separate utterances
    into a single emformer state so that it can be used as an input for
    emformer when those utterances are formed into a batch.

    Note:
      It is the inverse of :func:`unstack_states`.

    Args:
      state_list:
        Each element in state_list corresponding to the internal state
        of the emformer model for a single utterance.
        ``states[i]`` is a tuple of 2 elements of i-th utterance.
        ``states[i][0]`` is the attention caches of i-th utterance.
        ``states[i][1]`` is the convolution caches of i-th utterance.
        ``len(states[i][0])`` and ``len(states[i][1])`` both eqaul to number of layers.  # noqa

    Returns:
      A new state corresponding to a batch of utterances.
      See the input argument of :func:`unstack_states` for the meaning
      of the returned tensor.
    """
    batch_size = len(state_list)

    attn_caches = []
    for layer in state_list[0][0]:
        if batch_size > 1:
            # Note: We will stack attn_caches[layer][s][] later to get attn_caches[layer][s]  # noqa
            attn_caches.append([[s] for s in layer])
        else:
            attn_caches.append([s.unsqueeze(1) for s in layer])
    for b, states in enumerate(state_list[1:], 1):
        for li, layer in enumerate(states[0]):
            for si, s in enumerate(layer):
                attn_caches[li][si].append(s)
                if b == batch_size - 1:
                    attn_caches[li][si] = torch.stack(attn_caches[li][si], dim=1)

    conv_caches = []
    for layer in state_list[0][1]:
        if batch_size > 1:
            # Note: We will stack conv_caches[layer][] later to get conv_caches[layer]  # noqa
            conv_caches.append([layer])
        else:
            conv_caches.append(layer.unsqueeze(0))
    for b, states in enumerate(state_list[1:], 1):
        for li, layer in enumerate(states[1]):
            conv_caches[li].append(layer)
            if b == batch_size - 1:
                conv_caches[li] = torch.stack(conv_caches[li], dim=0)

    return [attn_caches, conv_caches]


def _create_streaming_feature_extractor() -> OnlineFeature:
    """Create a CPU streaming feature extractor.

    At present, we assume it returns a fbank feature extractor with
    fixed options. In the future, we will support passing in the options
    from outside.

    Returns:
      Return a CPU streaming feature extractor.
    """
    opts = FbankOptions()
    opts.device = "cpu"
    opts.frame_opts.dither = 0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq = 16000
    opts.mel_opts.num_bins = 80
    return OnlineFbank(opts)


class Stream(object):
    def __init__(
        self,
        context_size: int,
        blank_id: int,
        initial_states: List[List[torch.Tensor]],
        decoding_method: str = "greedy_search",
        decoding_graph: Optional[k2.Fsa] = None,
        decoder_out: Optional[torch.Tensor] = None,
    ) -> None:
        """
        Args:
          context_size:
            Context size of the RNN-T decoder model.
          blank_id:
            Blank token ID of the BPE model.
          initial_states:
            The initial states of the Emformer model. Note that the state
            does not contain the batch dimension.
          decoding_method:
            The decoding method to use, currently, only greedy_search and
            fast_beam_search are supported.
          decoding_graph:
            The Fsa based decoding graph for fast_beam_search.
          decoder_out:
            The initial decoder out corresponding to the decoder input
            `[blank_id]*context_size`
        """
        self.feature_extractor = _create_streaming_feature_extractor()
        # It contains a list of 2-D tensors representing the feature frames.
        # Each entry is of shape (1, feature_dim)
        self.features: List[torch.Tensor] = []
        self.num_fetched_frames = 0

        self.states = initial_states
        self.decoding_graph = decoding_graph

        if decoding_method == "fast_beam_search":
            assert decoding_graph is not None
            self.rnnt_decoding_stream = k2.RnntDecodingStream(decoding_graph)
            self.hyp = []
        elif decoding_method == "greedy_search":
            assert decoder_out is not None
            self.decoder_out = decoder_out
            self.hyp = [blank_id] * context_size
        else:
            raise ValueError(f"Decoding method : {decoding_method} is not supported.")

        self.processed_frames = 0
        self.context_size = context_size
        self.hyp = [blank_id] * context_size
        self.log_eps = math.log(1e-10)

    def accept_waveform(
        self,
        sampling_rate: float,
        waveform: torch.Tensor,
    ) -> None:
        """Feed audio samples to the feature extractor and compute features
        if there are enough samples available.

        Caution:
          The range of the audio samples should match the one used in the
          training. That is, if you use the range [-1, 1] in the training, then
          the input audio samples should also be normalized to [-1, 1].

        Args
          sampling_rate:
            The sampling rate of the input audio samples. It is used for sanity
            check to ensure that the input sampling rate equals to the one
            used in the extractor. If they are not equal, then no resampling
            will be performed; instead an error will be thrown.
          waveform:
            A 1-D torch tensor of dtype torch.float32 containing audio samples.
            It should be on CPU.
        """
        self.feature_extractor.accept_waveform(
            sampling_rate=sampling_rate,
            waveform=waveform,
        )
        self._fetch_frames()

    def input_finished(self) -> None:
        """Signal that no more audio samples available and the feature
        extractor should flush the buffered samples to compute frames.
        """
        self.feature_extractor.input_finished()
        self._fetch_frames()

    def _fetch_frames(self) -> None:
        """Fetch frames from the feature extractor"""
        while self.num_fetched_frames < self.feature_extractor.num_frames_ready:
            frame = self.feature_extractor.get_frame(self.num_fetched_frames)
            self.features.append(frame)
            self.num_fetched_frames += 1

    def add_tail_paddings(self, n: int = 20) -> None:
        """Add some tail paddings so that we have enough context to process
        frames at the very end of an utterance.

        Args:
          n:
            Number of tail padding frames to be added. You can increase it if
            it happens that there are many missing tokens for the last word of
            an utterance.
        """
        tail_padding = torch.full(
            (1, self.feature_extractor.opts.mel_opts.num_bins),
            fill_value=self.log_eps,
            dtype=torch.float32,
        )

        self.features += [tail_padding] * n
