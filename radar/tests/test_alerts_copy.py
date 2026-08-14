"""Localized push copy.

``render`` composes the final notification strings from the frontend i18n files, so
foreground (alerts.js) and background notifications read identically.
"""

from __future__ import annotations

import json

import pytest
from django.test import override_settings

from radar.alerts import copy


def teardown_function() -> None:
    copy._dicts.cache_clear()  # keep the module cache from leaking a temp dir across tests


def test_render_en_uses_frontend_strings():
    out = copy.render("en", "outer", 8.4, "nw")
    assert out["title"] == "⚡ Storm approaching"
    assert out["body"] == "Lightning 8 km north-west of your alert point"


def test_render_fr_folds_in_preposition():
    out = copy.render("fr", "inner", 3.2, "e")
    assert out["title"] == "⚡ Orage sur place"
    assert out["body"] == "Foudre à 3 km à l'est de votre point d'alerte"


def test_distance_rounds_with_minimum_one():
    assert copy.render("en", "outer", 0.2, "n")["body"].startswith("Lightning 1 km")
    assert copy.render("en", "outer", 12.6, "n")["body"].startswith("Lightning 13 km")


def test_all_eight_directions_render():
    for d in ("n", "ne", "e", "se", "s", "sw", "w", "nw"):
        assert copy.render("en", "outer", 5, d)["body"]
        assert copy.render("fr", "outer", 5, d)["body"]


def test_unknown_locale_falls_back_to_en():
    assert "Lightning" in copy.render("de", "outer", 5, "n")["body"]


def test_missing_required_key_raises(tmp_path):
    i18n = tmp_path / "i18n"
    i18n.mkdir()
    for loc in ("en", "fr"):
        (i18n / f"{loc}.json").write_text(json.dumps({"app.title": "x"}), encoding="utf-8")
    copy._dicts.cache_clear()
    with override_settings(FRONTEND_DIR=tmp_path), pytest.raises(KeyError):
        copy.render("en", "outer", 5, "n")
