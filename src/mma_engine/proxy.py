"""Optional outbound proxy for YouTube requests.

GitHub Actions' free/shared `ubuntu-latest` runners sit on cloud-provider IP
ranges that YouTube blocks outright — confirmed via youtube-transcript-api's
own `RequestBlocked` error, which explicitly names cloud IPs as blocked. That
is not something retries or alternate URL forms can work around: running the
pipeline unattended on a schedule requires routing YouTube requests through a
proxy with a residential (non-cloud) exit IP instead.

This module is the single place that turns `settings.proxy` + environment
variables into the two proxy objects the rest of the pipeline needs:

- a `youtube_transcript_api` `ProxyConfig`, for transcript fetching
- a plain `requests`-style `{"http": ..., "https": ...}` dict, for the
  channel-discovery RSS/page requests

Credentials are read from the environment, never from `config.json`, so they
can be stored as GitHub Actions repo secrets instead of committed. Nothing
here does anything unless `settings.proxy.enabled` is `true` — by default the
pipeline behaves exactly as before.
"""

from __future__ import annotations

import os
from typing import Any

from youtube_transcript_api.proxies import GenericProxyConfig, ProxyConfig, WebshareProxyConfig

WEBSHARE_USERNAME_ENV = "WEBSHARE_PROXY_USERNAME"
WEBSHARE_PASSWORD_ENV = "WEBSHARE_PROXY_PASSWORD"
GENERIC_URL_ENV = "MMA_PROXY_URL"


class ProxyConfigError(ValueError):
    """Raised when settings.proxy.enabled is true but credentials are missing."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _webshare_credentials() -> tuple[str, str]:
    username = _env(WEBSHARE_USERNAME_ENV)
    password = _env(WEBSHARE_PASSWORD_ENV)
    if not username or not password:
        raise ProxyConfigError(
            "settings.proxy.enabled is true (provider=webshare) but "
            f"{WEBSHARE_USERNAME_ENV} / {WEBSHARE_PASSWORD_ENV} are not set."
        )
    return username, password


def _generic_url() -> str:
    url = _env(GENERIC_URL_ENV)
    if not url:
        raise ProxyConfigError(
            f"settings.proxy.enabled is true (provider=generic) but {GENERIC_URL_ENV} "
            "is not set. It must be a full proxy URL, e.g. "
            "http://user:pass@host:port"
        )
    return url


def build_proxy_config(settings: dict[str, Any]) -> ProxyConfig | None:
    """A `youtube_transcript_api` `ProxyConfig` for transcript fetching, or None."""
    proxy = settings.get("proxy") or {}
    if not proxy.get("enabled"):
        return None

    provider = (proxy.get("provider") or "webshare").strip().lower()
    if provider == "webshare":
        username, password = _webshare_credentials()
        return WebshareProxyConfig(proxy_username=username, proxy_password=password)
    if provider == "generic":
        url = _generic_url()
        return GenericProxyConfig(http_url=url, https_url=url)
    raise ProxyConfigError(f"Unknown settings.proxy.provider: {provider!r}")


def build_requests_proxies(settings: dict[str, Any]) -> dict[str, str] | None:
    """An `http`/`https` proxies dict for plain `requests` calls, or None.

    Uses the same credentials as `build_proxy_config` so channel discovery and
    transcript fetching exit through the same non-cloud IP.
    """
    proxy = settings.get("proxy") or {}
    if not proxy.get("enabled"):
        return None

    provider = (proxy.get("provider") or "webshare").strip().lower()
    if provider == "webshare":
        username, password = _webshare_credentials()
        url = f"http://{username}:{password}@p.webshare.io:80"
        return {"http": url, "https": url}
    if provider == "generic":
        url = _generic_url()
        return {"http": url, "https": url}
    raise ProxyConfigError(f"Unknown settings.proxy.provider: {provider!r}")
