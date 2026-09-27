"""PERSON and ADDRESS spans from the GLiNER PII model, run directly on onnxruntime (no torch)."""

import re
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from redactit.types import Span

# Model-card label -> our entity type. The model scores each label separately.
LABELS = {"name": "PERSON", "location address": "ADDRESS", "location street": "ADDRESS"}
MAX_WIDTH = 12  # gliner_config.json max_width: the longest span, in words, the model scores
WINDOW, STRIDE = 200, 150  # words per model call; the overlap keeps boundary entities whole
FLOOR = 0.30  # lowest threshold any dial position uses; the policy decides the rest
WORD = re.compile(r"\w+(?:[-_]\w+)*|\S")  # GLiNER's own word splitter
PROMPT = [t for label in LABELS for t in ("<<ENT>>", label)] + ["<<SEP>>"]


class GlinerNer:
    def __init__(self, model: Path, tokenizer: Path):
        self.session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(tokenizer))
        self.tok.no_truncation()  # silently dropping words would silently drop entities
        self.types = list(LABELS.values())

    def detect(self, text: str) -> list[Span]:
        words = [(m.start(), m.end()) for m in WORD.finditer(text)]
        spans = []
        for start in range(0, len(words), STRIDE):
            spans += self._predict(text, words[start:start + WINDOW])
            if start + WINDOW >= len(words):
                break
        return _greedy(spans)

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


def _greedy(spans: list[Span]) -> list[Span]:
    """Highest score first, dropping anything that overlaps a kept span (GLiNER's flat NER)."""
    kept: list[Span] = []
    for s in sorted(spans, key=lambda s: -s.score):
        if all(s.end <= k.start or s.start >= k.end for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s.start)
