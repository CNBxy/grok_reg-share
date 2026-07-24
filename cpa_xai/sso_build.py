"""Pure-HTTP SSO -> Build Device OAuth conversion.

Replicates grok2api's web-side sso_build.go flow using SSO cookies
to automate the Device OAuth consent approval without a browser.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write workspaces:read workspaces:write"
ACCOUNTS_URL = "https://accounts.x.ai/"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CLIENT_VERSION = "0.2.111"
CLIENT_SURFACE = "ui"
REFERRER = "grok-build"

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


class SSOBuildError(RuntimeError):
    pass


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def _claim_string(claims: dict[str, Any], key: str) -> str:
    val = claims.get(key)
    return str(val).strip() if val is not None else ""


@dataclass
class SSOConversionResult:
    access_token: str
    refresh_token: str
    id_token: str | None
    expires_in: int
    user_id: str
    email: str
    team_id: str
    user_code: str


def _normalize_sso_token(value: str) -> str:
    value = (value or "").strip()
    m = re.match(r"^sso=(.+)$", value, re.IGNORECASE)
    if m:
        value = m.group(1).strip()
    value = value.split(";")[0].strip()
    return value.replace("\r", "").replace("\n", "").replace("\x00", "")


class _CookieCaptureHandler(urllib.request.BaseHandler):
    """Extracts Set-Cookie from ALL responses, including 3xx redirects.

    urllib.request.HTTPCookieProcessor.http_response is only called for 2xx,
    so any session cookie set during a 302 redirect is silently dropped.
    This handler captures cookies from every response status.
    """

    def __init__(self, cookiejar: http.cookiejar.CookieJar) -> None:
        self.cookiejar = cookiejar

    def http_response(self, request: Any, response: Any) -> Any:
        self.cookiejar.extract_cookies(response, request)
        return response

    http_error_301 = http_response
    http_error_302 = http_response
    http_error_303 = http_response
    http_error_307 = http_response
    http_error_308 = http_response


def _make_opener(
    sso_token: str,
    proxy: str | None = None,
) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    cookiejar = http.cookiejar.CookieJar()
    # seed SSO cookies for all relevant domains
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
        for name in ("sso", "sso-rw"):
            c = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=sso_token,
                port=None, port_specified=False,
                domain=domain, domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/", path_specified=True,
                secure=True, expires=None, discard=False,
                comment=None, comment_url=None, rest={},
            )
            cookiejar.set_cookie(c)

    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPCookieProcessor(cookiejar),
        _CookieCaptureHandler(cookiejar),
    ]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy, "https": proxy,
        }))
    return urllib.request.build_opener(*handlers), cookiejar


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    form: dict[str, str] | None = None,
    timeout: float = 60.0,
    device_flow: bool = False,
) -> tuple[int, str, bytes]:
    data = urllib.parse.urlencode(form).encode() if form else None
    headers = {
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": USER_AGENT,
    }
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if device_flow:
        headers["x-grok-client-version"] = CLIENT_VERSION
        headers["x-grok-client-surface"] = CLIENT_SURFACE

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=timeout)
        final_url = resp.geturl() if hasattr(resp, "geturl") else url
        return resp.status, final_url, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read()
    except urllib.error.URLError as e:
        raise SSOBuildError(f"request failed: {e.reason}") from e
    except OSError as e:
        raise SSOBuildError(f"connection error: {e}") from e


def _poll_token(
    opener: urllib.request.OpenerDirector,
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    log = log or _noop_log
    if interval < 1:
        interval = 1
    deadline = time.time() + max(expires_in - 5, 75)

    while time.time() < deadline:
        if cancel and cancel():
            raise SSOBuildError("cancelled during token poll")

        status, _, body = _request(
            opener, "POST", TOKEN_URL,
            {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
             "device_code": device_code, "client_id": client_id},
            timeout=timeout, device_flow=True,
        )

        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            payload = {}

        if status >= 200 and status < 300 and payload.get("access_token"):
            access = str(payload["access_token"]).strip()
            refresh = str(payload.get("refresh_token") or "").strip()
            if not refresh:
                raise SSOBuildError("token response missing refresh_token")
            expires = int(payload.get("expires_in") or 3600)
            return {
                "access_token": access,
                "refresh_token": refresh,
                "id_token": str(payload.get("id_token")).strip() if payload.get("id_token") else None,
                "expires_in": expires,
            }

        err = str(payload.get("error") or "").strip()
        desc = str(payload.get("error_description") or "").strip()

        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval = min(interval + 5, 30)
            log(f"token poll: slow_down, interval -> {interval}s")
            time.sleep(interval)
            continue
        if err in ("access_denied", "expired_token"):
            raise SSOBuildError(f"device auth denied: {err}: {desc}" if desc else f"device auth denied: {err}")
        if err:
            raise SSOBuildError(f"token error: {err}: {desc}" if desc else f"token error: {err}")

        time.sleep(interval)

    raise SSOBuildError("token poll timed out")


def convert_sso_to_build(
    sso_token: str,
    *,
    proxy: str | None = None,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> SSOConversionResult:
    """Full SSO-to-Build Device OAuth conversion via HTTP.

    Steps:
      1. Validate SSO cookie against accounts.x.ai
      2. Start Device Code flow
      3. Visit verification URI (establishes SSO session)
      4. Auto-verify device code
      5. Auto-approve consent
      6. Poll for OAuth tokens

    Raises SSOBuildError on failure.
    """
    log = log or _noop_log
    sso_val = _normalize_sso_token(sso_token)
    if not sso_val:
        raise SSOBuildError("SSO token is empty")

    opener, _cookiejar = _make_opener(sso_val, proxy=proxy)

    # 1. Validate SSO
    log("validate SSO at accounts.x.ai")
    if cancel and cancel():
        raise SSOBuildError("cancelled during SSO validation")
    status, final_url, _ = _request(opener, "GET", ACCOUNTS_URL)
    if status == 401 or "sign-in" in final_url.lower() or "sign-up" in final_url.lower():
        raise SSOBuildError("SSO token invalid or expired")
    if status < 200 or status >= 400:
        raise SSOBuildError(f"SSO validation failed: HTTP {status}")

    # 2. Start Device Code
    log("start device code flow")
    if cancel and cancel():
        raise SSOBuildError("cancelled during device code request")
    status, _, body = _request(
        opener, "POST", DEVICE_CODE_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": REFERRER},
        device_flow=True,
    )
    if status < 200 or status >= 300:
        raise SSOBuildError(f"device code request failed: HTTP {status}")
    try:
        device = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        raise SSOBuildError("device code response: invalid JSON") from e
    device_code = (device.get("device_code") or "").strip()
    user_code = (device.get("user_code") or "").strip()
    verification_uri_complete = (device.get("verification_uri_complete") or "").strip()
    if not device_code or not user_code or not verification_uri_complete:
        raise SSOBuildError(f"device code response missing fields: {device}")
    interval = max(int(device.get("interval") or 5), 1)
    expires_in = int(device.get("expires_in") or 1800)

    # 3. Visit verification URI (establishes SSO session)
    log(f"visit verification page for {user_code}")
    if cancel and cancel():
        raise SSOBuildError("cancelled during verification visit")
    status, final_url, _ = _request(opener, "GET", verification_uri_complete)
    if status < 200 or status >= 400:
        raise SSOBuildError(f"verification page visit failed: HTTP {status}")

    # 4. Verify device code
    log("auto-verify device code")
    if cancel and cancel():
        raise SSOBuildError("cancelled during verify")
    status, final_url, _ = _request(opener, "POST", VERIFY_URL, {"user_code": user_code})
    if status < 200 or status >= 400:
        raise SSOBuildError(f"device code verification failed: HTTP {status}")
    if "consent" not in final_url.lower():
        raise SSOBuildError("device code verification did not lead to consent page")

    # 5. Approve consent
    log("auto-approve consent")
    if cancel and cancel():
        raise SSOBuildError("cancelled during approve")
    status, final_url, _ = _request(
        opener, "POST", APPROVE_URL,
        {"user_code": user_code, "action": "allow", "principal_type": "User", "principal_id": ""},
    )
    if status < 200 or status >= 400:
        raise SSOBuildError(f"consent approval failed: HTTP {status}")
    if "done" not in final_url.lower():
        raise SSOBuildError("consent approval did not lead to done page")

    # 6. Poll token
    log("poll for OAuth token")
    token = _poll_token(
        opener, device_code,
        interval=interval, expires_in=expires_in,
        log=log, cancel=cancel,
    )

    claims = _jwt_claims(token.get("id_token") or token.get("access_token") or "")
    user_id = _claim_string(claims, "sub")
    email = _claim_string(claims, "email")
    team_id = _claim_string(claims, "team_id")

    return SSOConversionResult(
        access_token=token["access_token"],
        refresh_token=token["refresh_token"],
        id_token=token.get("id_token"),
        expires_in=token.get("expires_in", 3600),
        user_id=user_id,
        email=email,
        team_id=team_id,
        user_code=user_code,
    )
