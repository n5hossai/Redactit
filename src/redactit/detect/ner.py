"""PERSON and ADDRESS spans from the GLiNER PII model, run directly on onnxruntime (no torch)."""

import re
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from redactit.policy import MODEL_FLOOR as FLOOR  # lowest threshold any dial uses
from redactit.types import Span

# Model-card label -> our entity type. The model scores each label separately.
LABELS = {"name": "PERSON", "location address": "ADDRESS", "location street": "ADDRESS"}
MAX_WIDTH = 12  # gliner_config.json max_width: the longest span, in words, the model scores
# Windows are cut by sub-word tokens, not words. A 12 KB base64 blob once made one
# 8,745-token window that ran for 15+ minutes at 5 GB, and a name's score fell from 0.98
# to 0.51 as its window grew to 4,000 tokens; short windows keep both in bounds.
WINDOW_TOKENS, OVERLAP_TOKENS = 320, 64
LONG_WORD_TOKENS = 40  # one "word" this long (base64, a URL) cannot be a name; skip it
# GLiNER's own word splitter, plus a split where a lower-case letter meets a capital: OCR
# drops spaces ("Chat withJenniferRice"), and a glued name is one unknown word to the model.
WORD = re.compile(r"[A-Z]?[a-z]+(?=[A-Z])|\w+(?:[-_]\w+)*|\S")
PROMPT = [t for label in LABELS for t in ("<<ENT>>", label)] + ["<<SEP>>"]


class GlinerNer:
    def __init__(self, model: Path, tokenizer: Path):
        self.session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(tokenizer))
        self.tok.no_truncation()  # silently dropping words would silently drop entities
        self.types = list(LABELS.values())

    def detect(self, text: str) -> list[Span]:
        words = [(m.start(), m.end()) for m in WORD.finditer(text)]
        if not words:
            return []
        lens = [len(e.ids) for e in self.tok.encode_batch([text[s:e] for s, e in words], add_special_tokens=False)]
        budget = WINDOW_TOKENS - len(self.tok.encode(PROMPT, is_pretokenized=True).ids)
        spans = [s for a, b in _windows(lens, budget) for s in self._predict(text, words[a:b])]
        return [s for t in set(self.types) for s in _greedy([x for x in spans if x.entity_type == t])]

    def _predict(self, text: str, words: list[tuple[int, int]]) -> list[Span]:
        enc = self.tok.encode(PROMPT + [text[s:e] for s, e in words], is_pretokenized=True)
        # words_mask marks the first sub-token of each text word with its 1-based index;
        # prompt tokens and continuation sub-tokens are 0 (GLiNER's "first" pooling).
        mask, prev = [], None
        for wid in enc.word_ids:
            first = wid is not None and wid != prev and wid >= len(PROMPT)
            mask.append(wid - len(PROMPT) + 1 if first else 0)
            prev = wid
        n = len(words)
        idx = np.array([(i, i + k) for i in range(n) for k in range(MAX_WIDTH)], dtype=np.int64)
        logits = self.session.run(None, {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "words_mask": np.array([mask], dtype=np.int64),
            "text_lengths": np.array([[n]], dtype=np.int64),
            "span_idx": idx[None],
            "span_mask": (idx[:, 1] < n)[None],
        })[0][0]  # [words, MAX_WIDTH, labels]
        probs = 1 / (1 + np.exp(-logits))
        return [
            Span(words[i][0], words[i + k][1], self.types[c], float(probs[i, k, c]), "gliner")
            for i, k, c in zip(*np.nonzero(probs > FLOOR))
            if i + k < n
        ]


def _windows(lens: list[int], budget: int) -> list[tuple[int, int]]:
    """Word ranges of at most `budget` tokens, overlapping by about OVERLAP_TOKENS, that
    skip over-long words so each window stays small."""
    out, start = [], 0
    while start < len(lens):
        if lens[start] > LONG_WORD_TOKENS:
            start += 1
            continue
        end, used = start, 0
        while end < len(lens) and lens[end] <= LONG_WORD_TOKENS and used + lens[end] <= budget:
            used += lens[end]
            end += 1
        out.append((start, end))
        if end >= len(lens) or lens[end] > LONG_WORD_TOKENS:
            start = end
            continue
        back = end  # step back so an entity cut at the edge is seen whole next time
        while back > start + 1 and sum(lens[back - 1:end]) <= OVERLAP_TOKENS:
            back -= 1
        start = back
    return out


def _greedy(spans: list[Span]) -> list[Span]:
    """Highest score first, dropping anything that overlaps a kept span (GLiNER's flat NER).

    Run per entity type, so a name below its own threshold cannot suppress an address.
    """
    kept: list[Span] = []
    for s in sorted(spans, key=lambda s: -s.score):
        if all(s.end <= k.start or s.start >= k.end for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s.start)
