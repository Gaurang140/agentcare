"""One Unicode fold, shared by the safety layers that match fixed patterns
against text somebody else wrote.

A pattern list is only as good as the spelling it is matched against. Four
cheap rewrites defeat a raw match while rendering identically on screen:
a zero-width character dropped inside a word ("ignore<zwsp> previous"); any
of the other invisible format characters used the same way, the word joiner
and the bidi controls that reorder a line among them; a Latin-lookalike
letter borrowed from another script, where a Cyrillic small i stands in for
the Latin one and the word still reads as "ignore"; and full-width Latin,
which is a different code point for every letter
("ｊａｉｌｂｒｅａｋ"). NFKC folds the compatibility spellings back onto the
plain ones. The invisible characters have to be deleted outright because NFKC
keeps every one of them, and the lookalikes need a small skeleton map because
to Unicode they are different letters and not different spellings of one
letter.

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

# Characters that render as nothing and survive NFKC: the soft hyphen, the
# zero-width space and joiners, the directional marks, the bidi controls that
# reorder a line without changing what it says, the word joiner and the rest
# of the invisible operators, the isolates and the byte-order mark. A phrase
# broken across any of them reads normally and matches none of the patterns.
# Written as escapes on purpose. A literal here would be invisible in a diff.
_INVISIBLE_RE = re.compile(
    "["
    "\u00ad"  # soft hyphen
    "\u200b-\u200f"  # zero-width space, joiners, LTR and RTL marks
    "\u202a-\u202e"  # bidi embeddings and overrides
    "\u2060-\u2064"  # word joiner and the invisible operators
    "\u2066-\u206f"  # bidi isolates and the deprecated format characters
    "\ufeff"  # byte-order mark
    "]"
)
_WHITESPACE_RE = re.compile(r"\s+")

# Non-Latin letters that are drawn like Latin ones. NFKC keeps them apart
# because they are different letters, not compatibility spellings, so the map
# is explicit and deliberately small: the Cyrillic and Greek letters whose
# forms are visually identical to a Latin letter in a normal font. Anything
# wider (a full confusable table per Unicode UTS 39) is a dependency this repo
# does not carry, and the safety layers scan the raw text too. Escapes again:
# written as literals every key on the left would look like the value on its
# right, and a reviewer would have no way to tell the two apart.
_SKELETON = str.maketrans(
    {
        # Cyrillic lower case
        "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
        "\u0441": "c", "\u0443": "y", "\u0445": "x", "\u0456": "i",
        "\u0458": "j", "\u0455": "s", "\u04bb": "h",
        # Cyrillic upper case
        "\u0405": "S", "\u0410": "A", "\u0412": "B", "\u0415": "E",
        "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O",
        "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X",
        # Greek
        "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z",
        "\u0397": "H", "\u0399": "I", "\u039a": "K", "\u039c": "M",
        "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T",
        "\u03a5": "Y", "\u03a7": "X", "\u03bf": "o", "\u03c1": "p",
    }
)


def fold_confusables(text: str) -> str:
    """Unicode NFKC, invisible and bidi characters removed, Latin lookalikes
    mapped back to Latin, runs of whitespace collapsed to a single space, ends
    trimmed.

    NFKC makes a compatibility spelling of a word the same string as the plain
    one and folds exotic spaces onto an ordinary space. The invisible class has
    to be deleted outright because NFKC keeps it, and the skeleton map exists
    because a Cyrillic letter is a different letter to Unicode however
    identical it looks on screen.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _INVISIBLE_RE.sub("", folded).translate(_SKELETON)
    return _WHITESPACE_RE.sub(" ", folded).strip()
