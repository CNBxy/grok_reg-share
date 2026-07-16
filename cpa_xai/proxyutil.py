"""Resolve outbound proxy for CPA mint HTTP + browser.

Priority (highest first):
  1. explicit argument
  2. thread-local runtime pin (set_runtime_proxy)
  3. environment https_proxy / HTTPS_PROXY / http_proxy / HTTP_PROXY

Thread-local pin avoids cross-talk when multiple mint workers run with
different proxies in the same process.

Supported proxy schemes:
  - http://   (standard HTTP proxy)
  - https://  (HTTPS proxy, less common)
  - socks5://  (SOCKS5, with or without user:pass@)
  - socks5h:// (SOCKS5 with remote DNS resolution)
  - socks4://  (SOCKS4)
  - bare host:port (treated as http://)

SOCKS support for urllib.request requires PySocks (``pip install pysocks``).
When PySocks is installed, urllib.request.ProxyHandler accepts socks5://
and socks5h:// URLs directly.  curl_cffi (used by grok_register_ttk.py)
supports SOCKS natively via libcurl -- no extra dependency needed.
"""

from __future__ import annotations

import os
import threading
from urllib.parse import urlparse

_thread = threading.local()

SOCKS_SCHEMES = ("socks5", "socks5h", "socks4")


def set_runtime_proxy(proxy: str | None) -> None:
    """Pin proxy for the *current thread*. Empty clears pin."""
    p = (proxy or "").strip()
    _thread.proxy = p or None


def get_runtime_proxy() -> str | None:
    return getattr(_thread, "proxy", None)


def resolve_proxy(explicit: str | None = None) -> str:
    for cand in (
        (explicit or "").strip(),
        (get_runtime_proxy() or "").strip(),
        (os.environ.get("https_proxy") or "").strip(),
        (os.environ.get("HTTPS_PROXY") or "").strip(),
        (os.environ.get("http_proxy") or "").strip(),
        (os.environ.get("HTTP_PROXY") or "").strip(),
    ):
        if cand:
            return cand
    return ""


def is_socks(proxy: str) -> bool:
    """Return True if the proxy URL uses a SOCKS scheme."""
    p = (proxy or "").strip()
    if not p or "://" not in p:
        return False
    scheme = urlparse(p).scheme.lower()
    return scheme in SOCKS_SCHEMES


def _default_port(scheme: str) -> int:
    """Default port for a proxy scheme."""
    s = scheme.lower()
    if s in ("socks5", "socks5h", "socks4"):
        return 1080
    if s == "https":
        return 443
    return 80  # http, unknown


def proxy_for_chromium(proxy: str) -> str:
    """Chromium --proxy-server cannot embed user:pass; host:port only.

    Supports all schemes Chromium recognises: http, https, socks5, socks4.
    Returns scheme://host:port (userinfo stripped).
    """
    p = (proxy or "").strip()
    if not p:
        return ""
    u = urlparse(p if "://" in p else f"http://{p}")
    host = u.hostname or ""
    if not host:
        return ""
    scheme = (u.scheme or "http").lower()
    port = u.port or _default_port(scheme)
    return f"{scheme}://{host}:{port}"


def proxy_log_label(proxy: str) -> str:
    """Redact userinfo for logs."""
    p = (proxy or "").strip()
    if not p:
        return ""
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
        host = u.hostname or "?"
        port = u.port or ""
        auth = "user:***@" if u.username else ""
        return f"{u.scheme or 'http'}://{auth}{host}{(':' + str(port)) if port else ''}"
    except Exception:
        return "(proxy)"
