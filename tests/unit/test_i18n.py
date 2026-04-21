"""Translation completeness tests."""

from __future__ import annotations

import pytest

from pingcapture.i18n import MissingTranslationError, Translator, supported_languages


def test_all_languages_have_same_keys() -> None:
    en = Translator("en")
    keys = set(en._strings.keys())
    for lang in supported_languages():
        t = Translator(lang)
        missing = keys - set(t._strings.keys())
        extra = set(t._strings.keys()) - keys
        assert not missing, f"{lang} missing keys: {missing}"
        assert not extra, f"{lang} extra keys: {extra}"


def test_translator_raises_on_missing_key() -> None:
    t = Translator("en")
    with pytest.raises(MissingTranslationError):
        t.t("not_a_real_key")


def test_format_substitution() -> None:
    t = Translator("en")
    s = t.t("unit_seconds", n=3.5)
    assert "3.5" in s


def test_german_not_just_english() -> None:
    en = Translator("en").t("report_title")
    de = Translator("de").t("report_title")
    assert en != de
    assert "DSL" in de  # 'DSL' is preserved
