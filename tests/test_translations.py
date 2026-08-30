"""Structural parity guard for the translation files (§11).

Values are intentionally NOT compared (some technical terms stay English in a few
locales), only the leaf-KEY sets:

* ``strings.json`` and ``translations/en.json`` must be structurally identical —
  catches "edited strings.json, forgot to sync en.json" (or vice versa).
* every ``translations/<locale>.json`` must carry the exact same leaf keys as
  ``en.json`` — so a newly-added source string fails CI until every locale has
  been given the key, making straggler drift impossible to reintroduce silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "entity_state_tracker"
_STRINGS = _COMPONENT / "strings.json"
_TRANSLATIONS = _COMPONENT / "translations"


def _leaf_keys(obj: object, prefix: str = "") -> set[str]:
    """Flatten a nested dict to its dotted leaf-key paths."""
    if not isinstance(obj, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        keys |= _leaf_keys(value, path)
    return keys


def _load(path: Path) -> set[str]:
    return _leaf_keys(json.loads(path.read_text(encoding="utf-8")))


def test_strings_and_en_are_structurally_identical() -> None:
    """strings.json (source) and translations/en.json share the exact key set."""
    strings = _load(_STRINGS)
    en = _load(_TRANSLATIONS / "en.json")
    assert strings == en, {
        "only_in_strings": sorted(strings - en),
        "only_in_en": sorted(en - strings),
    }


def _locale_files() -> list[Path]:
    return sorted(p for p in _TRANSLATIONS.glob("*.json") if p.stem != "en")


@pytest.mark.parametrize("locale", _locale_files(), ids=lambda p: p.stem)
def test_locale_key_parity_with_en(locale: Path) -> None:
    """Every locale carries exactly en.json's leaf keys — no missing, no extra."""
    en = _load(_TRANSLATIONS / "en.json")
    keys = _load(locale)
    assert keys == en, {
        "missing_from_locale": sorted(en - keys),
        "extra_in_locale": sorted(keys - en),
    }
