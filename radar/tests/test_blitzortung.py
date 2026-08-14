"""Blitzortung decode + Strike construction.

Pure — no network. The decode routine is exercised two ways: (1) all-ASCII
frames pass through the LZW expansion unchanged (the documented identity
property), so a readable JSON fixture round-trips into the expected ``Strike``;
(2) a reference LZW *encoder* (test-only) compresses a string into one that uses
code points ≥ 256, proving the production decoder expands the dictionary path.
A malformed frame must log ``parse_failed`` and be skipped (no raise).
"""

from __future__ import annotations

import json
import logging

import pytest

from radar.lightning import blitzortung
from radar.lightning import get_active_source
from radar.lightning.base import Strike


def _lzw_encode(text: str) -> str:
    """Reference LZW encoder matching :func:`blitzortung._lzw_decode` (test-only)."""
    dictionary = {chr(i): i for i in range(256)}
    code = 256
    w = ""
    result: list[int] = []
    for ch in text:
        wc = w + ch
        if wc in dictionary:
            w = wc
        else:
            result.append(dictionary[w])
            dictionary[wc] = code
            code += 1
            w = ch
    if w:
        result.append(dictionary[w])
    return "".join(chr(c) for c in result)


def _frame(**overrides) -> str:
    obj = {
        "time": 1718960400512000000,  # ns epoch -> 1718960400.512 s
        "lat": 45.1,
        "lon": 3.2,
        "sig": [{"sta": 1}, {"sta": 2}, {"sta": 3}],
        **overrides,
    }
    return json.dumps(obj)


# -- LZW decode ---------------------------------------------------------------


def test_ascii_passes_through_unchanged():
    text = _frame()
    assert blitzortung._lzw_decode(text) == text


def test_empty_input_decodes_to_empty():
    assert blitzortung._lzw_decode("") == ""


def test_decode_expands_compressed_dictionary_path():
    # A repetitive string compresses to a stream that uses code points >= 256.
    plain = "ABABABABABABABAB_strike_strike_strike"
    compressed = _lzw_encode(plain)
    assert any(ord(c) >= 256 for c in compressed)  # compression actually happened
    assert blitzortung._lzw_decode(compressed) == plain


def test_decode_roundtrips_a_compressed_json_frame():
    text = _frame()
    compressed = _lzw_encode(text)
    strike = blitzortung.decode_frame(compressed)
    assert strike.lat == pytest.approx(45.1)


# -- frame -> Strike ----------------------------------------------------------


def test_decode_frame_builds_expected_strike():
    strike = blitzortung.decode_frame(_frame())
    assert isinstance(strike, Strike)
    assert strike.struck_at == pytest.approx(1718960400.512, abs=1e-3)
    assert strike.lat == pytest.approx(45.1)
    assert strike.lon == pytest.approx(3.2)
    assert strike.intensity == 3  # detecting-station count proxy


def test_decode_frame_accepts_bytes():
    strike = blitzortung.decode_frame(_frame().encode("utf-8"))
    assert strike.lon == pytest.approx(3.2)


def test_intensity_none_without_stations():
    strike = blitzortung.decode_frame(_frame(sig=None))
    assert strike.intensity is None


def test_intensity_uses_stations_key_alias():
    obj = {"time": 1, "lat": 1.0, "lon": 2.0, "stations": [{"sta": 1}, {"sta": 2}]}
    strike = blitzortung.decode_frame(json.dumps(obj))
    assert strike.intensity == 2


def test_intensity_capped_to_smallint():
    obj = {"time": 1, "lat": 1.0, "lon": 2.0, "sig": [{} for _ in range(40000)]}
    strike = blitzortung.decode_frame(json.dumps(obj))
    assert strike.intensity == blitzortung._SMALLINT_MAX


# -- malformed-frame handling (adapter swallows + logs, never raises) ---------


def test_source_decode_skips_malformed_frame_and_logs(caplog):
    source = get_active_source()
    with caplog.at_level(logging.WARNING, logger="radar.lightning"):
        result = source._decode("}{ not json")
    assert result is None
    assert any(r.event == "parse_failed" for r in caplog.records)


def test_source_decode_skips_frame_missing_fields():
    source = get_active_source()
    assert source._decode(json.dumps({"hello": "world"})) is None  # no time/lat/lon


def test_decode_frame_raises_on_non_numeric_time():
    with pytest.raises((TypeError, ValueError)):
        blitzortung.decode_frame(json.dumps({"time": "soon", "lat": 1.0, "lon": 2.0}))


# -- source registry + attribution --------------------------------------------


def test_get_active_source_is_blitzortung():
    source = get_active_source()
    assert source.name == "blitzortung"


def test_attribution_mentions_blitzortung_with_link():
    attribution = get_active_source().attribution()
    assert "Blitzortung.org" in attribution
    assert "https://www.blitzortung.org" in attribution
