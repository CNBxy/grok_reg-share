"""High-level: mint CPA xai-*.json for one SSO-authenticated registered account.

Uses pure-HTTP SSO->Build Device OAuth flow (no browser).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from .proxyutil import proxy_log_label, resolve_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .sso_build import SSOBuildError, convert_sso_to_build
from .probe import probe_mini_response, probe_models
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _safe_log_callback(callback: LogFn) -> LogFn:
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


def mint_and_export(
    *,
    email: str,
    sso_token: str = "",
    auth_dir: str | Path,
    proxy: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Full pipeline: SSO->Build conversion -> write CPA file -> optional probe.

    Uses pure-HTTP Device OAuth flow with SSO cookie (no browser).

    Accepts legacy kwargs (password, page, headless, etc.) for backward compat; ignored.

    Returns dict with keys: ok, path, email, user_code, probe, error?
    """
    log = _safe_log_callback(log or _noop)
    email = (email or "").strip()
    if not email:
        return {"ok": False, "email": email, "error": "missing email"}
    sso_val = (sso_token or "").strip()
    if not sso_val:
        return {"ok": False, "email": email, "error": "missing sso_token"}

    resolved = resolve_proxy(proxy)
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")

    try:
        result = convert_sso_to_build(
            sso_val,
            proxy=resolved or None,
            log=log,
            cancel=cancel,
        )
    except SSOBuildError as e:
        log(f"mint failed: {e}")
        return {"ok": False, "email": email, "error": str(e)}

    payload = build_cpa_xai_auth(
        email=email,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        id_token=result.id_token,
        expires_in=result.expires_in,
        base_url=base_url,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")

    result_dict: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": result.user_code,
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
    }

    if probe:
        pr = probe_models(result.access_token, base_url=base_url, proxy=resolved or None)
        result_dict["probe_models"] = pr
        log(f"probe models: ok={pr.get('ok')} has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')}")
        if not pr.get("has_grok_45"):
            result_dict["ok"] = False
            result_dict["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                result.access_token, base_url=base_url, proxy=resolved or None
            )
            result_dict["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result_dict["ok"] = False
                result_dict["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"

    return result_dict
