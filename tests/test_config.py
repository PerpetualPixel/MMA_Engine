"""Tests for config.json loading — defaults, merging, and env overrides.

No network. Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json

import pytest

from mma_engine.config import load_config

MINIMAL = {
    "cappers": [{"id": "artem_mma", "name": "Artem MMA"}],
}


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_proxy_defaults_when_omitted(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.settings["proxy"] == {"enabled": False, "provider": "webshare"}


def test_partial_proxy_block_keeps_other_defaults(tmp_path):
    data = {**MINIMAL, "settings": {"proxy": {"enabled": True}}}
    config = load_config(write_config(tmp_path, data))
    assert config.settings["proxy"] == {"enabled": True, "provider": "webshare"}


def test_mma_proxy_enabled_env_overrides_config(tmp_path, monkeypatch):
    data = {**MINIMAL, "settings": {"proxy": {"enabled": False}}}
    monkeypatch.setenv("MMA_PROXY_ENABLED", "true")
    config = load_config(write_config(tmp_path, data))
    assert config.settings["proxy"]["enabled"] is True


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_mma_proxy_enabled_env_false_values_disable(tmp_path, monkeypatch, value):
    data = {**MINIMAL, "settings": {"proxy": {"enabled": True}}}
    if value:
        monkeypatch.setenv("MMA_PROXY_ENABLED", value)
    else:
        monkeypatch.delenv("MMA_PROXY_ENABLED", raising=False)
    config = load_config(write_config(tmp_path, data))
    # An empty env var is treated as unset (falls through to config.json's value);
    # any other non-truthy string explicitly disables it.
    expected = True if value == "" else False
    assert config.settings["proxy"]["enabled"] is expected


def test_transcript_cookies_defaults_when_omitted(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.settings["transcript_cookies"] == {
        "enabled": False,
        "from_browser": "",
        "file": "",
    }


def test_partial_transcript_cookies_block_keeps_other_defaults(tmp_path):
    data = {**MINIMAL, "settings": {"transcript_cookies": {"from_browser": "chrome"}}}
    config = load_config(write_config(tmp_path, data))
    assert config.settings["transcript_cookies"] == {
        "enabled": False,
        "from_browser": "chrome",
        "file": "",
    }


def test_transcript_cookies_file_env_sets_path_and_enables(tmp_path, monkeypatch):
    monkeypatch.setenv("MMA_TRANSCRIPT_COOKIES_FILE", "/run/secrets/cookies.txt")
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.settings["transcript_cookies"]["file"] == "/run/secrets/cookies.txt"
    assert config.settings["transcript_cookies"]["enabled"] is True


def test_transcript_cookies_enabled_env_can_disable(tmp_path, monkeypatch):
    data = {**MINIMAL, "settings": {"transcript_cookies": {"enabled": True, "from_browser": "chrome"}}}
    monkeypatch.setenv("MMA_TRANSCRIPT_COOKIES_ENABLED", "false")
    config = load_config(write_config(tmp_path, data))
    assert config.settings["transcript_cookies"]["enabled"] is False


# -- more than one card in a run -------------------------------------------


def test_event_specs_lists_the_primary_card_then_the_extras(tmp_path):
    from mma_engine.config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "event": {"name": "Nurmagomedov vs. Song", "league": "UFC"},
                "events": [
                    {"name": "PFL 9", "league": "pfl", "label": "PFL 9: Playoffs"},
                    {"name": ""},  # unnamed entries are ignored
                ],
                "cappers": [{"id": "a", "name": "A"}],
            }
        ),
        encoding="utf-8",
    )

    specs = load_config(path).event_specs
    assert specs == [
        {"name": "Nurmagomedov vs. Song", "league": "ufc", "label": ""},
        {"name": "PFL 9", "league": "pfl", "label": "PFL 9: Playoffs"},
    ]


def test_a_config_with_one_event_still_yields_one_spec(tmp_path):
    from mma_engine.config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"event": {"name": "UFC 300"}, "cappers": [{"id": "a", "name": "A"}]}),
        encoding="utf-8",
    )
    assert load_config(path).event_specs == [
        {"name": "UFC 300", "league": "", "label": ""}
    ]
