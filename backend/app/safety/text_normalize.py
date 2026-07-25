"""One Unicode fold, shared by the safety layers that match fixed patterns
against text somebody else wrote.

A pattern list is only as good as the spelling it is matched against. Two
cheap rewrites defeat a raw match while rendering identically on screen:
a zero-width character dropped inside a word ("ignore<zwsp> previous"), and
full-width Latin, which is a different code point for every letter
("ｊａｉｌｂｒｅａｋ"). NFKC folds the compatibility spellings back onto the
plain ones, and the zero-width characters have to be deleted outright because
NFKC keeps them.

Exotic spaces need no help here: `\\s` in a Python pattern already matches a
non-breaking space and friends, so the phrase patterns have that slack
whether or not anything is folded. The literals do not, which is the case
this fold covers.

The callers scan the folded reading in addition to the raw one rather than
instead of it, so folding can only ever add a match. That matters because a
fold is lossy by construction, and a check like the base64-run shape reads
better on the untouched text.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width space, the two joiners and the byte-order mark. They render as
# nothing at all, so a phrase can be broken across one of them and still read
# normally while matching none of the patterns.
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")


def fold_confusables(text: str) -> str:
    """Unicode NFKC, zero-width characters removed, runs of whitespace
    collapsed to a single space, ends trimmed.

    NFKC is what makes a compatibility spelling of a word the same string as
    the plain one, and it also folds the exotic space characters onto an
    ordinary space, which the collapse then joins with any other run of
    whitespace.
    """
    folded = unicodedata.normalize("NFKC", text)
    return _WHITESPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub("", folded)).strip()
