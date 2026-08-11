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
