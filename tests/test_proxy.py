"""Tests for optional proxy configuration — env vars in, proxy objects out.

No network. Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import pytest
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

from mma_engine.proxy import ProxyConfigError, build_proxy_config, build_requests_proxies


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "WEBSHARE_PROXY_USERNAME",
        "WEBSHARE_PROXY_PASSWORD",
        "MMA_PROXY_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default_returns_none():
    settings = {"proxy": {"enabled": False, "provider": "webshare"}}
    assert build_proxy_config(settings) is None
    assert build_requests_proxies(settings) is None


def test_missing_proxy_key_treated_as_disabled():
    assert build_proxy_config({}) is None
    assert build_requests_proxies({}) is None


def test_webshare_config_built_from_env(monkeypatch):
    monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "user123")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "pass456")
    settings = {"proxy": {"enabled": True, "provider": "webshare"}}

    config = build_proxy_config(settings)
    assert isinstance(config, WebshareProxyConfig)

    proxies = build_requests_proxies(settings)
    assert proxies == {
        "http": "http://user123:pass456@p.webshare.io:80",
        "https": "http://user123:pass456@p.webshare.io:80",
    }


def test_webshare_enabled_without_credentials_raises():
    settings = {"proxy": {"enabled": True, "provider": "webshare"}}
    with pytest.raises(ProxyConfigError):
        build_proxy_config(settings)
    with pytest.raises(ProxyConfigError):
        build_requests_proxies(settings)


def test_generic_config_built_from_env(monkeypatch):
    monkeypatch.setenv("MMA_PROXY_URL", "http://user:pass@10.0.0.1:8080")
    settings = {"proxy": {"enabled": True, "provider": "generic"}}

    config = build_proxy_config(settings)
    assert isinstance(config, GenericProxyConfig)

    proxies = build_requests_proxies(settings)
    assert proxies == {
        "http": "http://user:pass@10.0.0.1:8080",
        "https": "http://user:pass@10.0.0.1:8080",
    }


def test_generic_enabled_without_url_raises():
    settings = {"proxy": {"enabled": True, "provider": "generic"}}
    with pytest.raises(ProxyConfigError):
        build_proxy_config(settings)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "u")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "p")
    settings = {"proxy": {"enabled": True, "provider": "bogus"}}
    with pytest.raises(ProxyConfigError):
        build_proxy_config(settings)


def test_provider_defaults_to_webshare(monkeypatch):
    monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "user123")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "pass456")
    settings = {"proxy": {"enabled": True}}
    assert isinstance(build_proxy_config(settings), WebshareProxyConfig)
