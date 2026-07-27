"""The single Unicode fold: what it removes, what it maps, what it leaves alone.

Every obfuscated string here is written with explicit `\\u` escapes. The
characters this module exists for render as nothing or as an ordinary Latin
letter, so a literal in the source would be invisible to a reviewer and easy
to corrupt on the way in.
"""

from __future__ import annotations

from app.safety.text_normalize import fold_confusables


def test_zero_width_and_joiners_are_removed():
    assert fold_confusables("ig\u200bnore") == "ignore"
    assert fold_confusables("ig\u2060nore") == "ignore"


def test_bidi_controls_are_removed():
    for ch in (
        "\u202a",  # left-to-right embedding
        "\u202b",  # right-to-left embedding
        "\u202c",  # pop directional formatting
        "\u202d",  # left-to-right override
        "\u202e",  # right-to-left override
        "\u2066",  # left-to-right isolate
        "\u2069",  # pop directional isolate
    ):
        assert fold_confusables(f"ig{ch}nore") == "ignore"


def test_cyrillic_lookalikes_fold_to_latin():
    assert fold_confusables("\u0456gnore") == "ignore"
    assert fold_confusables("\u0430ll") == "all"
    assert fold_confusables("\u0435very") == "every"


def test_full_width_still_folds():
    assert fold_confusables("\uff4a\uff41\uff49\uff4c") == "jail"


def test_ordinary_german_text_is_untouched():
    assert fold_confusables("Ich brauche einen Termin") == "Ich brauche einen Termin"
    assert fold_confusables("Grüße aus München") == "Grüße aus München"
    assert fold_confusables("Impfpass") == "Impfpass"


def test_whitespace_collapses_and_trims():
    assert fold_confusables("  ignore   all \n previous  ") == "ignore all previous"
