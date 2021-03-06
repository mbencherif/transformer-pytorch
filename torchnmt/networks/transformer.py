import copy
import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_packed_sequence

from torchnmt import networks
from torchnmt.datasets.utils import Vocab

from .encoders import TransformerEncoder
from .decoders import TransformerDecoder
from .utils import padding_mask, subsequent_mask


class Transformer(nn.Module):
    def __init__(self, encoder, decoder, vocab_share=False):
        super().__init__()
        assert encoder.model_dim == decoder.model_dim
        self.encoder = networks.get(encoder)
        self.decoder = networks.get(decoder)
        if vocab_share:
            self.decoder.embed = self.encoder.embed
        self.register_buffer('_device', torch.zeros(0))

    @property
    def device(self):
        return self._device.device

    def forward(self, src, tgt=None, max_len=None, beam_width=None, **_):
        """
        Args:
            src: packed sequence (*, input_dim)
            tgt: packed sequence (*, output_dim)
        """
        pad = Vocab.extra2idx('<pad>')
        src, src_len = pad_packed_sequence(src, True, pad)
        src = src.to(self.device)
        src_mask = padding_mask(src_len).to(self.device)
        mem = self.encoder(src, src_mask)

        ret = {}

        if tgt is not None:
            tgt, tgt_len = pad_packed_sequence(tgt, True, pad)
            tgt = tgt.to(self.device)
            tgt_mask = padding_mask(tgt_len) & subsequent_mask(tgt_len)
            tgt_mask = tgt_mask.to(self.device)

            outputs = self.decoder(tgt, mem, src_mask, tgt_mask)
            logp = F.log_softmax(outputs, dim=-1)

            chopped_outputs = outputs[:, :-1].reshape(-1, outputs.shape[-1])
            shifted_targets = tgt[:, 1:].reshape(-1)

            loss = F.cross_entropy(chopped_outputs,
                                   shifted_targets,
                                   ignore_index=pad)

            ret.update({
                'logp': logp,
                'loss': loss
            })

        if not self.training:
            # time step
            # inputs: hyps (bs, len), mem, mem_mask
            if beam_width == 1:
                hyps = self.greedy_inference(mem, src_mask, max_len)
            else:
                hyps = self.beam_search_inference(
                    mem, src_mask, max_len, beam_width)

            ret.update({
                'hyps': hyps,
            })

        return ret

    def greedy_inference(self, mem, mem_mask, max_len):
        """
        Args:
            mem: (bs, src_len, model_dim)
        Outputs:
            tgt_output: [(tgt_len,)]
        """
        bos = Vocab.extra2idx('<s>')
        eos = Vocab.extra2idx('</s>')

        batch_idx = torch.arange(len(mem))
        running = torch.full((len(mem), 1), bos).long().to(self.device)
        finished = []

        for l in range(1, max_len):
            tgt_mask = subsequent_mask([l]).to(self.device)
            outputs = self.decoder(running, mem, mem_mask, tgt_mask)
            outputs = outputs[:, -1].argmax(dim=-1)  # (bs,)
            running = torch.cat([running, outputs[:, None]], dim=-1)

            running_idx = (outputs != eos).nonzero().squeeze(1)
            finished_idx = (outputs == eos).nonzero().squeeze(1)

            finished += list(zip(batch_idx[finished_idx],
                                 running[finished_idx].tolist()))

            running = running[running_idx]
            batch_idx = batch_idx[running_idx]
            mem = mem[running_idx]
            mem_mask = mem_mask[running_idx]

            if len(running) == 0:
                break

        finished += list(zip(batch_idx, running.tolist()))
        finished = [x[1] for x in sorted(finished, key=lambda x: x[0])]

        return finished

    def beam_search_helper(self, memory, mem_mask, max_len, beam_width):
        """
        Args:
            mem: (bs, src_len, model_dim)
        Outputs:
            tgt_output: (tgt_len,)
        """
        bos = Vocab.extra2idx('<s>')
        eos = Vocab.extra2idx('</s>')

        # create k (beam_width) beams for simplicity
        # but need to set the first logp 0 and the rest -inf
        # otherwise the k beams will dominate during the whole decoding
        logps = torch.full((beam_width, ), -np.inf).to(self.device)
        logps[0] = 0
        hyps = torch.full((beam_width, 1), bos).long().to(self.device)

        finished = []

        memory = memory.expand(beam_width, *memory.shape[1:])
        mem_mask = mem_mask.expand(beam_width, *mem_mask.shape[1:])

        for l in range(1, max_len):
            k = len(logps)

            tgt_mask = subsequent_mask([l]).to(self.device)

            outputs = self.decoder(hyps,
                                   memory[:k],
                                   mem_mask[:k],
                                   tgt_mask)

            outputs = torch.log_softmax(outputs[:, -1], dim=-1)

            # for each beam, calculate top k
            tmp_logps, tmp_idxs = torch.topk(outputs, k)

            # calculate accumulated logps
            tmp_logps += logps[:, None]

            # calculate new top k
            tmp_logps = tmp_logps.view(-1)
            tmp_idxs = tmp_idxs.view(-1)

            logps, idxs = torch.topk(tmp_logps, k)

            words = tmp_idxs[idxs]
            hyps_idxs = idxs // k

            hyps = torch.cat([hyps[hyps_idxs], words[:, None]], dim=1)

            finished_idx = (words == eos).nonzero().squeeze(1)
            running_idx = (words != eos).nonzero().squeeze(1)

            finished += list(zip(logps[finished_idx], hyps[finished_idx]))

            logps = logps[running_idx]
            hyps = hyps[running_idx]

            if len(logps) <= 0:
                break

        finished = finished + list(zip(logps, hyps))

        hyp = max(finished, key=lambda t: t[0])[1]

        return hyp

    def beam_search_inference(self, mem, mem_mask, max_len, beam_width):
        return [self.beam_search_helper(mem[i:i + 1],
                                        mem_mask[i:i + 1],
                                        max_len,
                                        beam_width)
                for i in range(len(mem))]
