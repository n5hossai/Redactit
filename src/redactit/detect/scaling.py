"""Presidio's de-duplication and context passes, with the same output, in time that grows with
the text instead of with its square.

On a 200 KB paste (4,790 raw matches) the two passes took 6.7 s and 12.6 s, against 0.03 s
and 0.12 s on 20 KB: de-duplication compared every match with every kept one, and the
context pass found each match's token by scanning every token from the start, then looked
nearby lemmas up in a list of every keyword in the text.
"""

from __future__ import annotations

from bisect import bisect_right

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer

PRESIDIO_REMOVE_DUPLICATES = EntityRecognizer.remove_duplicates  # the original, which the tests compare against


def remove_duplicates(results: list[RecognizerResult]) -> list[RecognizerResult]:
    """`EntityRecognizer.remove_duplicates`, kept result for kept result and in the same order.

    Presidio keeps a result unless it has score 0 or lies inside an already kept result of
    its own type (an equal one included), taking results by score, then start, then length.
    The order and the set() before it are Presidio's own, so ties fall the same way. "Inside
    a kept one" is answered per type from the furthest end kept among starts at or before
    this start (a Fenwick tree over the starts), instead of by comparing with each kept one.
    """
    results = sorted(set(results), key=lambda x: (-x.score, x.start, -(x.end - x.start)))
    starts = sorted({r.start for r in results})
    furthest: dict[str, list[int]] = {}  # entity type -> tree of the furthest kept end
    kept = []
    for result in results:
        if result.score == 0:
            continue
        tree = furthest.setdefault(result.entity_type, [-1] * (len(starts) + 1))
        i = bisect_right(starts, result.start)  # 1-based position of this start
        j, reach = i, -1
        while j:  # the furthest end among kept results starting at or before this one
            reach, j = max(reach, tree[j]), j & (j - 1)
        if reach >= result.end:
            continue
        while i < len(tree):
            tree[i], i = max(tree[i], result.end), i + (i & -i)
        kept.append(result)
    return kept


class LinearContextEnhancer(LemmaContextAwareEnhancer):
    """Presidio's lemma context enhancer with the same defaults and results.

    A match's token is found by binary search on token ends, and lemmas are looked up in a
    set of the keywords: the same answers as Presidio's scans, which made the pass quadratic.
    """

    def enhance_using_context(self, text, raw_results, nlp_artifacts, recognizers, context=None):
        indexed = _Indexed(nlp_artifacts) if nlp_artifacts is not None else None
        return super().enhance_using_context(text, raw_results, indexed, recognizers, context)

    def _extract_surrounding_words(self, nlp_artifacts: "_Indexed", word: str, start: int) -> list[str]:
        if not nlp_artifacts.tokens:
            return [""]
        # Presidio takes the first token that starts at `start` or ends after it. Token ends only
        # grow (spaCy tokens are non-empty and in order), so that is the first end past `start`.
        index = bisect_right(nlp_artifacts.ends, start)
        if index == len(nlp_artifacts.ends):
            raise ValueError("Did not find the match in the list of tokens although it is expected to be found")
        words = self._add_n_words_backward(index, self.context_prefix_count, nlp_artifacts.lemmas,
                                           nlp_artifacts.keywords)
        words += self._add_n_words_forward(index, self.context_suffix_count, nlp_artifacts.lemmas,
                                           nlp_artifacts.keywords)
        return list(set(words))


class _Indexed:
    """What the enhancer reads from Presidio's NLP artifacts, plus token ends and a keyword set."""

    def __init__(self, artifacts) -> None:
        self.tokens, self.lemmas = artifacts.tokens, artifacts.lemmas
        self.keywords = frozenset(artifacts.keywords)
        self.ends = [i + len(token) for i, token in zip(artifacts.tokens_indices, artifacts.tokens)]
