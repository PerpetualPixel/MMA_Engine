"""Tests for the transcript fetcher's yt-dlp cookie fallback.

No network and no real yt-dlp calls — subprocess is monkeypatched so the
fallback's decision logic, command building, and json3 parsing are exercised
in isolation. Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from youtube_transcript_api import AgeRestricted, PoTokenRequired
from youtube_transcript_api.proxies import GenericProxyConfig

from mma_engine.transcripts import (
    CookieConfig,
    TranscriptFetcher,
    _parse_json3,
    _preferred_sub_file,
    build_cookie_config,
)


# -- CookieConfig / build_cookie_config --------------------------------------


def test_cookie_config_browser_wins_over_file():
    cfg = CookieConfig(from_browser="firefox", file="cookies.txt")
    assert cfg.ytdlp_args() == ["--cookies-from-browser", "firefox"]


def test_cookie_config_file_when_no_browser():
    assert CookieConfig(file="cookies.txt").ytdlp_args() == ["--cookies", "cookies.txt"]


def test_cookie_config_unconfigured_is_empty():
    assert CookieConfig().ytdlp_args() == []
    assert not CookieConfig().is_configured
    assert CookieConfig(from_browser="  ").ytdlp_args() == []


def test_build_cookie_config_disabled_returns_none():
    assert build_cookie_config({"transcript_cookies": {"enabled": False, "from_browser": "chrome"}}) is None
    assert build_cookie_config({}) is None


def test_build_cookie_config_enabled_without_source_returns_none():
    # Enabled but with nothing to authenticate with is the same as off, rather
    # than a misconfiguration that surfaces only as a yt-dlp error per video.
    assert build_cookie_config({"transcript_cookies": {"enabled": True}}) is None


def test_build_cookie_config_enabled_with_browser():
    cfg = build_cookie_config({"transcript_cookies": {"enabled": True, "from_browser": "chrome"}})
    assert cfg == CookieConfig(from_browser="chrome", file="")


# -- json3 parsing -----------------------------------------------------------


def test_parse_json3_joins_segments_and_skips_empty():
    raw = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Islam"}, {"utf8": " Makhachev"}]},
                {"segs": [{"utf8": "  "}]},  # whitespace-only, dropped
                {"foo": "no segs key"},
                {"segs": [{"utf8": "wins"}]},
            ]
        }
    )
    assert _parse_json3(raw) == "Islam Makhachev wins"


def test_parse_json3_empty_events():
    assert _parse_json3(json.dumps({"events": []})) == ""
    assert _parse_json3(json.dumps({})) == ""


def test_preferred_sub_file_matches_language_prefix():
    files = [Path("vid.es.json3"), Path("vid.en-US.json3")]
    assert _preferred_sub_file(files, ["en"]).name == "vid.en-US.json3"


def test_preferred_sub_file_falls_back_to_first():
    files = [Path("vid.fr.json3"), Path("vid.de.json3")]
    assert _preferred_sub_file(files, ["en"]) == files[0]


# -- command building --------------------------------------------------------


def test_ytdlp_command_shape():
    fetcher = TranscriptFetcher(cookie_config=CookieConfig(from_browser="chrome"), languages=["en", "es"])
    cmd = fetcher._ytdlp_command("VIDEOID1234", "/tmp/%(id)s.%(ext)s")
    # Invoked as `python -m yt_dlp` through the running interpreter, not the
    # bare console script — weekly.ps1 runs the venv python without activating
    # it, so .venv\Scripts isn't on PATH and a bare "yt-dlp" would 404.
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--cookies-from-browser" in cmd and "chrome" in cmd
    assert "--skip-download" in cmd
    assert "--write-subs" in cmd and "--write-auto-subs" in cmd
    assert cmd[cmd.index("--sub-langs") + 1] == "en,es"
    assert cmd[cmd.index("--sub-format") + 1] == "json3"
    assert cmd[-1] == "https://www.youtube.com/watch?v=VIDEOID1234"


def test_ytdlp_command_includes_proxy_when_configured():
    proxy = GenericProxyConfig(http_url="http://u:p@host:80", https_url="http://u:p@host:80")
    fetcher = TranscriptFetcher(cookie_config=CookieConfig(file="c.txt"), proxy_config=proxy)
    cmd = fetcher._ytdlp_command("X", "/tmp/%(id)s.%(ext)s")
    assert cmd[cmd.index("--proxy") + 1] == "http://u:p@host:80"


def test_ytdlp_command_omits_proxy_when_none():
    fetcher = TranscriptFetcher(cookie_config=CookieConfig(file="c.txt"))
    assert "--proxy" not in fetcher._ytdlp_command("X", "/tmp/o")


# -- the fallback path through fetch() ---------------------------------------


class _FakeTranscriptApi:
    """Stands in for YouTubeTranscriptApi.fetch, raising a chosen error so the
    fallback branch runs without touching the network."""

    def __init__(self, exc):
        self._exc = exc

    def fetch(self, video_id, languages):  # noqa: ARG002 - signature match
        raise self._exc


def _fetcher_with_primary_error(tmp_path, exc, cookie_config):
    fetcher = TranscriptFetcher(
        cache_dir=tmp_path / "cache",
        cookie_config=cookie_config,
        min_delay=0,
        max_delay=0,
    )
    fetcher._api = _FakeTranscriptApi(exc)
    return fetcher


def test_age_restricted_without_cookies_reports_the_gate(tmp_path):
    # No cookies configured: behaves exactly as before — a clean failure, no
    # subprocess attempted — but the message points at the fix.
    fetcher = _fetcher_with_primary_error(tmp_path, AgeRestricted("v"), cookie_config=None)
    result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "AgeRestricted" in result.error
    assert "transcript_cookies" in result.error


def test_age_restricted_with_cookies_uses_ytdlp(tmp_path, monkeypatch):
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    captured = {}

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        captured["cmd"] = cmd
        # Emulate yt-dlp writing a json3 sub into the -o directory.
        out_template = cmd[cmd.index("-o") + 1]
        out_dir = Path(out_template).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "vid00000001.en.json3").write_text(
            json.dumps({"events": [{"segs": [{"utf8": "cookie"}, {"utf8": " transcript"}]}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("vid00000001")

    assert result.ok
    assert result.text == "cookie transcript"
    assert "--cookies-from-browser" in captured["cmd"]
    # And it was cached, so a re-fetch needs no second subprocess call.
    assert (tmp_path / "cache" / "vid00000001.json").is_file()


def test_potoken_required_also_triggers_fallback(tmp_path, monkeypatch):
    fetcher = _fetcher_with_primary_error(
        tmp_path, PoTokenRequired("v"), cookie_config=CookieConfig(file="c.txt")
    )
    ran = {"count": 0}

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        ran["count"] += 1
        out_dir = Path(cmd[cmd.index("-o") + 1]).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "v.en.json3").write_text(json.dumps({"events": [{"segs": [{"utf8": "ok"}]}]}), "utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("videoidpoto")
    assert result.ok and result.text == "ok"
    assert ran["count"] == 1


def test_ytdlp_failure_returns_clean_error(tmp_path, monkeypatch):
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=b"Sign in to confirm your age")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "did not return captions" in result.error


def test_ytdlp_not_installed_returns_clean_error(tmp_path, monkeypatch):
    # `python -m yt_dlp` with the package missing exits non-zero with
    # "No module named yt_dlp" on stderr — a CalledProcessError, not a
    # FileNotFoundError (that would only fire if the interpreter itself were
    # gone). Either way the fetch degrades cleanly.
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        raise subprocess.CalledProcessError(
            1, cmd, output=b"", stderr=b"C:\\py.exe: No module named yt_dlp\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "did not return captions" in result.error


def test_ytdlp_dpapi_decrypt_failure_returns_clean_error(tmp_path, monkeypatch, caplog):
    # Chrome 127+ App-Bound Encryption: --cookies-from-browser fails to decrypt.
    # Degrades cleanly, and the log points at the cookies.txt workaround.
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        raise subprocess.CalledProcessError(
            1, cmd, output=b"", stderr=b"ERROR: Failed to decrypt with DPAPI. See ...\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    import logging
    with caplog.at_level(logging.ERROR):
        result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "did not return captions" in result.error
    assert any("cookies.txt" in r.message for r in caplog.records)


def test_ytdlp_missing_interpreter_returns_clean_error(tmp_path, monkeypatch):
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        raise FileNotFoundError("python")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "did not return captions" in result.error


def test_ytdlp_no_captions_written_returns_error(tmp_path, monkeypatch):
    fetcher = _fetcher_with_primary_error(
        tmp_path, AgeRestricted("v"), cookie_config=CookieConfig(from_browser="chrome")
    )

    def fake_run(cmd, check, capture_output, timeout):  # noqa: ARG001
        # Success exit, but no sub file — video simply has no captions at all.
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetcher.fetch("vid00000001")
    assert not result.ok
    assert "did not return captions" in result.error
