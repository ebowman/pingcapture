"""Tiny i18n module. Loads dict-based translations from sibling .toml files.

Adding a language: drop ``<lang>.toml`` next to this file, mirror keys in en.toml.
``Translator(lang).gettext(key)`` raises if a key is missing — surfaced by tests.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib import resources
from typing import Any


class MissingTranslationError(KeyError):
    pass


@lru_cache(maxsize=8)
def _load(lang: str) -> dict[str, str]:
    pkg = resources.files(__package__)
    path = pkg / f"{lang}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"unsupported language: {lang}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


class Translator:
    def __init__(self, lang: str):
        self.lang = lang
        self._strings = _load(lang)

    def t(self, key: str, **fmt: Any) -> str:
        if key not in self._strings:
            raise MissingTranslationError(f"{self.lang}:{key}")
        return self._strings[key].format(**fmt) if fmt else self._strings[key]


def supported_languages() -> list[str]:
    return ["en", "de", "ga"]


__all__ = ["MissingTranslationError", "Translator", "supported_languages"]
