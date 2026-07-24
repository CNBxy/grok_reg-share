"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

import time
import sys
from pathlib import Path
from typing import Any, Callable

from .browser_confirm import mint_with_browser
from .probe import probe_mini_response, probe_models
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _safe_log_callback(callback: LogFn) -> LogFn:
    """Prevent Windows console encoding errors from aborting a mint run."""

    def _safe(message: str) -> None:
        text = str(message)
        try:
            callback(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            escaped = text.encode(encoding, errors="backslashreplace").decode(
                encoding, errors="replace"
            )
            callback(escaped)

    return _safe


def _is_browser_disconnect_error(exc: BaseException) -> bool:
    """Return True only for transient browser/page connection failures."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "pagedisconnected" in name
        or "browserdisconnected" in name
        or "与页面的连接已断开" in message
        or "page disconnected" in message
        or "browser disconnected" in message
        or "browser has disconnected" in message
        or "target closed" in message
        or "connection was closed" in message
    )


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    browser_retries: int = 2,
    screenshot: bool = False,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: device-auth → write CPA file → optional probe.

    Returns dict with keys: ok, path, email, probe, error?
    """
    log = _safe_log_callback(log or _noop)
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "email": email, "error": "missing email/password"}

    # Config/explicit proxy wins over shell https_proxy (common 7890 trap).
    # Thread-local pin — safe under concurrent mint workers.
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")
    retries = max(0, min(int(browser_retries or 0), 5))
    tokens = None
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            tokens = mint_with_browser(
                email=email,
                password=password,
                page=None if force_standalone else page,
                proxy=resolved or None,
                headless=headless,
                browser_timeout_sec=browser_timeout_sec,
                force_standalone=force_standalone,
                cookies=cookies,
                reuse_browser=reuse_browser,
                recycle_every=recycle_every,
                screenshot=screenshot,
                poll_log=log,
                cancel=cancel,
            )
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            if not _is_browser_disconnect_error(e) or attempt > retries:
                log(f"mint failed: {e}")
                return {"ok": False, "email": email, "error": str(e)}
            delay = min(1.5 * attempt, 4.0)
            log(
                f"browser disconnected; rebuilding Chromium and retrying "
                f"({attempt}/{retries + 1}) after {delay:.1f}s"
            )
            time.sleep(delay)

    if tokens is None:
        error = str(last_error or "browser mint failed without result")
        log(f"mint failed: {error}")
        return {"ok": False, "email": email, "error": error}

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
    }

    if probe:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved or None)
        result["probe_models"] = pr
        log(f"probe models: ok={pr.get('ok')} has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')}")
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                tokens["access_token"], base_url=base_url, proxy=resolved or None
            )
            result["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result["ok"] = False
                result["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"
    return result
