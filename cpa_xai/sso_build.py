"""Pure-HTTP SSO -> Build Device OAuth conversion.

Replicates grok2api's web-side sso_build.go flow using SSO cookies
to automate the Device OAuth consent approval without a browser.
Manual cookie/redirect handling mirrors the Go code exactly.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

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


def _resolve_proxy(explicit: str | None = None) -> str:
    from .proxyutil import resolve_proxy
    return resolve_proxy(explicit)


def _make_connection(
    host: str,
    port: int,
    scheme: str,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> http.client.HTTPConnection:
    """Create and return an already-connected HTTPConnection."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    if not proxy:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.connect()
        return conn

    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    pscheme = p.scheme.lower()
    phost = p.hostname or ""
    pport = p.port or (443 if pscheme == "https" else 1080 if pscheme.startswith("socks") else 80)
    pauth = (p.username, p.password) if p.username else None

    if pscheme in ("socks5", "socks5h", "socks4", "socks4a"):
        try:
            import socks as socks_module
        except ImportError:
            raise SSOBuildError("SOCKS proxy requires PySocks: pip install pysocks")

        actual_sock = socks_module.socksocket()
        actual_sock.set_proxy(
            socks_module.SOCKS5 if pscheme in ("socks5", "socks5h") else socks_module.SOCKS4,
            phost, pport,
            username=pauth[0] if pauth else None,
            password=pauth[1] if pauth else None,
            rdns=pscheme in ("socks5h", "socks4a"),
        )
        actual_sock.settimeout(timeout)
        actual_sock.connect((host, port))

        if scheme == "https":
            ssl_sock = ctx.wrap_socket(actual_sock, server_hostname=host)
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
            conn.sock = ssl_sock
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.sock = actual_sock
        return conn

    if pscheme in ("https",):
        raise SSOBuildError("HTTPS proxy not supported")

    if scheme == "https":
        conn = http.client.HTTPSConnection(phost, pport, timeout=timeout, context=ctx)
        conn.set_tunnel(host, port)
    else:
        conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
    conn.connect()
    return conn


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


def _safe_xai_url(raw: str) -> bool:
    if not raw:
        return False
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "https" or parsed.username or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host == "x.ai" or host.endswith(".x.ai")


def _set_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in sorted(cookies.items()))


def _capture_cookies(cookies: dict[str, str], set_cookie_header: str | None) -> None:
    if not set_cookie_header:
        return
    for part in set_cookie_header.split(","):
        part = part.strip()
        m = re.match(r"^\s*([^=]+)=([^;]*)", part)
        if not m:
            continue
        name = m.group(1).strip()
        value = m.group(2).strip()
        if not name or len(name) > 128 or len(value) > 16384:
            continue
        if "; max-age=0" in part.lower() or "; max-age=-" in part:
            cookies.pop(name, None)
        else:
            cookies[name] = value


def _request(
    cookies: dict[str, str],
    method: str,
    url: str,
    form: dict[str, str] | None = None,
    timeout: float = 60.0,
    device_flow: bool = False,
    proxy: str | None = None,
) -> tuple[int, str, bytes]:
    """Manual request with redirect following, matching Go code exactly."""
    if not _safe_xai_url(url):
        raise SSOBuildError(f"unsafe xAI URL: {url[:80]}")

    current_url = url
    current_method = method
    current_form = form

    for _redirect in range(9):
        parsed = urllib.parse.urlparse(current_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        body_data = urllib.parse.urlencode(current_form).encode() if current_form else None

        conn = _make_connection(host, port, parsed.scheme, proxy=proxy, timeout=timeout)

        try:
            conn.putrequest(current_method, path, skip_accept_encoding=False, skip_host=True)
            conn.putheader("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
            conn.putheader("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
            conn.putheader("User-Agent", USER_AGENT)
            conn.putheader("Host", host)
            if device_flow:
                conn.putheader("x-grok-client-version", CLIENT_VERSION)
                conn.putheader("x-grok-client-surface", CLIENT_SURFACE)
            cookie_val = _set_cookie_header(cookies)
            if cookie_val:
                conn.putheader("Cookie", cookie_val)
            if current_form and body_data:
                conn.putheader("Content-Type", "application/x-www-form-urlencoded")
                conn.putheader("Content-Length", str(len(body_data)))
            conn.endheaders(body_data)

            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
            # capture Set-Cookie from this response (including redirects)
            sc = resp.getheader("Set-Cookie")
            if sc:
                _capture_cookies(cookies, sc)

            if status < 300 or status >= 400:
                conn.close()
                return status, current_url, body

            location = resp.getheader("Location")
            conn.close()

            if not location:
                return status, current_url, body

            next_url = urllib.parse.urljoin(current_url, location.strip())
            if not _safe_xai_url(next_url):
                return status, next_url, body

            current_url = next_url
            if status == 303 or (status in (301, 302) and current_method not in ("GET", "HEAD")):
                current_method = "GET"
                current_form = None
        except (OSError, http.client.HTTPException) as e:
            try:
                conn.close()
            except Exception:
                pass
            raise SSOBuildError(f"request failed: {e}") from e

    raise SSOBuildError("too many redirects")


def _poll_token(
    cookies: dict[str, str],
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    log = log or _noop_log
    if interval < 1:
        interval = 1
    deadline = time.time() + max(expires_in - 5, 75)

    while time.time() < deadline:
        if cancel and cancel():
            raise SSOBuildError("cancelled during token poll")

        status, _, body = _request(
            cookies, "POST", TOKEN_URL,
            {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
             "device_code": device_code, "client_id": client_id},
            timeout=timeout,
            device_flow=True,
            proxy=proxy,
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

    resolved_proxy = _resolve_proxy(proxy) or None

    cookies: dict[str, str] = {"sso": sso_val, "sso-rw": sso_val}

    # 1. Validate SSO
    log("validate SSO at accounts.x.ai")
    if cancel and cancel():
        raise SSOBuildError("cancelled during SSO validation")
    status, final_url, _ = _request(cookies, "GET", ACCOUNTS_URL, proxy=resolved_proxy)
    if status == 401 or "sign-in" in final_url.lower() or "sign-up" in final_url.lower():
        raise SSOBuildError("SSO token invalid or expired")
    if status < 200 or status >= 400:
        raise SSOBuildError(f"SSO validation failed: HTTP {status}")

    # 2. Start Device Code
    log("start device code flow")
    if cancel and cancel():
        raise SSOBuildError("cancelled during device code request")
    status, _, body = _request(
        cookies, "POST", DEVICE_CODE_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": REFERRER},
        device_flow=True, proxy=resolved_proxy,
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
    status, final_url, _ = _request(cookies, "GET", verification_uri_complete, proxy=resolved_proxy)
    if status < 200 or status >= 400:
        raise SSOBuildError(f"verification page visit failed: HTTP {status}")

    # 4. Verify device code
    log("auto-verify device code")
    if cancel and cancel():
        raise SSOBuildError("cancelled during verify")
    status, final_url, _ = _request(cookies, "POST", VERIFY_URL, {"user_code": user_code}, proxy=resolved_proxy)
    if status < 200 or status >= 400:
        raise SSOBuildError(f"device code verification failed: HTTP {status}")
    if "consent" not in final_url.lower():
        raise SSOBuildError("device code verification did not lead to consent page")

    # 5. Approve consent
    log("auto-approve consent")
    if cancel and cancel():
        raise SSOBuildError("cancelled during approve")
    status, final_url, _ = _request(
        cookies, "POST", APPROVE_URL,
        {"user_code": user_code, "action": "allow", "principal_type": "User", "principal_id": ""},
        proxy=resolved_proxy,
    )
    if status < 200 or status >= 400:
        raise SSOBuildError(f"consent approval failed: HTTP {status}")
    if "done" not in final_url.lower():
        raise SSOBuildError("consent approval did not lead to done page")

    # 6. Poll token
    log("poll for OAuth token")
    token = _poll_token(
        cookies, device_code,
        interval=interval, expires_in=expires_in,
        log=log, cancel=cancel, proxy=resolved_proxy,
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
