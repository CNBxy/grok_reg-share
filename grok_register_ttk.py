#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import datetime
import time
import os
import sys
import queue
import secrets
import hmac
import struct
import random
import re
import string
import json
from email import policy
from email.header import decode_header, make_header
from email.parser import Parser

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "bearer",
    "cloudflare_path_domains": "/domains",
    "cloudflare_path_accounts": "/accounts",
    "cloudflare_path_token": "/token",
    "cloudflare_path_messages": "/messages",
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_import_enabled": True,
    "grok2api_import_base": "http://127.0.0.1:8000",
    "grok2api_import_management_key": "",
    "grok2api_import_retries": 3,
    "grok2api_import_retry_delay": 2,
    "grok2api_auto_add_local": False,
    "grok2api_local_token_file": "",
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "show_tutorial_on_start": True,
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
    "openai_cpa_webhook_secret": "",
    "openai_cpa_cloudmail_fallback": True,
    "cpa_mint_browser_retries": 2,
    "nav_email_button_timeout": 12,
    "email_form_timeout": 20,
    "screenshot_on_error": False,
    "screenshot_interval": False,
    "cpa_remote_push_enabled": False,
    "cpa_remote_push_url": "",
    "cpa_remote_push_token": "",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
# CloudMail 公开 token 单例（多线程共享，避免并发覆盖）
_cloudmail_public_token = None
_cloudmail_public_token_lock = threading.Lock()
_openai_cpa_code_pool = {}
_openai_cpa_code_pool_lock = threading.Lock()
_OPENAI_CPA_POOL_TTL_SECONDS = 300
_OPENAI_CPA_POOL_MAX_ENTRIES = 1000



# ── 邮箱追踪 ──

_EMAILS_USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails_used.txt")
_EMAILS_ERROR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails_error.txt")
_email_track_lock = threading.Lock()


def mark_used(email: str, password: str = ""):
    """记录成功注册的邮箱，防止重复使用。"""
    with _email_track_lock:
        with open(_EMAILS_USED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----ok\n")


def mark_error(email: str, password: str = "", reason: str = ""):
    """记录失败邮箱及原因，避免重试烂邮箱。"""
    with _email_track_lock:
        with open(_EMAILS_ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----{reason}\n")


def is_email_used(email: str) -> bool:
    """检查邮箱是否已被使用或标记为失败。"""
    email_lower = email.strip().lower()
    for fpath in (_EMAILS_USED_FILE, _EMAILS_ERROR_FILE):
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("----")
                        if parts and parts[0].strip().lower() == email_lower:
                            return True
    return False


# ── 页面状态快照 ──

# ── CLI / batch performance knobs (register_cli may mutate) ──
PERF_FLAGS = {
    "fast": False,           # scale down human_sleep
    "sleep_scale": 1.0,      # multiply all human_sleep means
    "skip_debug_io": False,  # skip dump_state / take_screenshot
    "cookie_snapshot": True, # save_cookies_snapshot
    "async_side_effects": True,  # grok2api / cookie snapshot in background
    "browser_reuse": True,   # clear_session instead of quit between accounts
    "browser_recycle_every": 25,  # full quit+recreate after N successful reuses
}

_side_effect_pool = None


def _get_side_effect_pool():
    global _side_effect_pool
    if _side_effect_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _side_effect_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sidefx")
    return _side_effect_pool


def configure_perf(**kwargs):
    """Update PERF_FLAGS from CLI. Unknown keys ignored."""
    for k, v in kwargs.items():
        if k in PERF_FLAGS:
            PERF_FLAGS[k] = v


_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


def dump_state(page, tag: str = ""):
    """打印当前页面状态：URL、可见按钮文本、输入框类型。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        info = page.run_js("""() => {
            const btns = [...document.querySelectorAll('button')]
                .map(b => b.innerText.trim())
                .filter(t => t)
                .slice(0, 20);
            const inputs = [...document.querySelectorAll('input,textarea')]
                .map(i => (i.type || 'text') + '/' + (i.placeholder || i.name || ''))
                .slice(0, 15);
            return {url: location.href, btns: btns, inputs: inputs};
        }""")
        if not info:
            print(f"  [state:{tag}] page context not ready (None)")
            return
        print(f"  [state:{tag}] url: {info.get('url', '?')}")
        print(f"  [state:{tag}] btns: {info.get('btns', [])}")
        print(f"  [state:{tag}] inputs: {info.get('inputs', [])}")
    except Exception as e:
        print(f"  [state:{tag}] dump_state err: {e}")


def take_screenshot(page, tag: str = ""):
    """捕获当前页面截图并保存到 screenshots/ 目录。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H%M%S")
        path = os.path.join(_SCREENSHOT_DIR, f"{ts}_{tag}.png")
        page.get_screenshot(path=path)
        print(f"  [screenshot] saved: {path}")
    except Exception as e:
        print(f"  [screenshot] err: {e}")


def take_error_screenshot(page, tag: str = ""):
    """页面交互失败时截图 + 保存 HTML 到 screenshots/ 目录。

    不受 PERF_FLAGS.skip_debug_io 控制，仅由 config.screenshot_on_error 开关。
    """
    if not config.get("screenshot_on_error"):
        return
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 截图
        png_path = os.path.join(_SCREENSHOT_DIR, f"error_{ts}_{tag}.png")
        page.get_screenshot(path=png_path)
        print(f"  [error-screenshot] saved: {png_path}")
        # 保存页面 HTML
        html_path = os.path.join(_SCREENSHOT_DIR, f"error_{ts}_{tag}.html")
        html = page.html if page else ""
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  [error-html] saved: {html_path}")
    except Exception as e:
        print(f"  [error-screenshot] err: {e}")


# ── 流程定时截图 ──

_screenshot_thread = None
_screenshot_stop = threading.Event()
# 注册线程的 browser 引用（跨线程截图用，不经过 TabPool 的 thread_local）
_screenshot_browser = None


def start_interval_screenshot(worker_id: int | str = "", browser=None):
    """启动后台线程，每 0.1s 截图一张到 screenshots/ 目录。

    受 config.screenshot_interval 开关控制。关闭时调用无效果。
    每个账号注册开始时调用 start，结束时调用 stop。

    browser: 注册线程的 Chromium browser 对象。截图线程通过它直接获取最新 tab，
            避免跨线程访问 TabPool 的 thread_local（会误创建新浏览器）。
    """
    global _screenshot_thread, _screenshot_stop, _screenshot_browser
    if not config.get("screenshot_interval"):
        return
    stop_interval_screenshot()
    _screenshot_stop.clear()
    # 优先用传入的 browser，否则尝试从当前线程 TabPool 获取
    if browser is None:
        browser = TabPool.get_browser()
    _screenshot_browser = browser
    wid = str(worker_id)

    def _loop():
        import itertools
        counter = itertools.count()
        ss_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        print(f"[screenshot] 流程截图已启动 (worker={wid}, browser={'有' if _screenshot_browser else '无'})", flush=True)
        err_count = 0
        while not _screenshot_stop.is_set():
            try:
                browser_ref = _screenshot_browser
                if browser_ref is None:
                    _screenshot_stop.wait(0.1)
                    continue
                # 通过 browser 直接获取最新 tab（跨线程安全，只读截图）
                tab_ids = browser_ref.tab_ids
                if not tab_ids:
                    _screenshot_stop.wait(0.1)
                    continue
                tab = browser_ref.get_tab(tab_ids[-1])
                if tab is None:
                    _screenshot_stop.wait(0.1)
                    continue
                seq = next(counter)
                ts = datetime.datetime.now().strftime("%H%M%S")
                path = os.path.join(ss_dir, f"flow_{wid}_{ts}_{seq:05d}.png")
                tab.get_screenshot(path=path)
                err_count = 0  # 成功后重置错误计数
            except Exception as e:
                err_count += 1
                if err_count <= 3:
                    print(f"[screenshot] 截图异常: {e}", flush=True)
            _screenshot_stop.wait(0.1)

    _screenshot_thread = threading.Thread(target=_loop, daemon=True, name=f"sshot-{wid}")
    _screenshot_thread.start()


def stop_interval_screenshot():
    """停止流程定时截图线程。"""
    global _screenshot_thread, _screenshot_stop, _screenshot_browser
    if _screenshot_thread is None:
        return
    _screenshot_stop.set()
    _screenshot_thread.join(timeout=2)
    _screenshot_thread = None
    _screenshot_browser = None


# ── 超时守卫 ──

REGISTER_TIMEOUT = 180  # 单次注册总超时（秒）


class TimeoutError(Exception):
    pass


def check_timeout(start_time: float):
    """检查是否超过总超时时间。"""
    elapsed = time.time() - start_time
    if elapsed > REGISTER_TIMEOUT:
        raise TimeoutError(f"注册超时 ({REGISTER_TIMEOUT}s, 已用 {elapsed:.0f}s)")


# ── 全量 cookie 保存 ──

_COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies", "grok")


def save_cookies_snapshot(page, tag: str = "", email: str = ""):
    """保存当前浏览器上下文的全量 cookie 快照。"""
    if not PERF_FLAGS.get("cookie_snapshot", True):
        return
    try:
        browser = _get_browser()
        if not browser:
            return
        os.makedirs(_COOKIE_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cookies = browser.cookies()
        data = {
            "ts": ts,
            "tag": tag,
            "email": email,
            "url": page.url if page else "",
            "cookies": cookies,
        }
        path = os.path.join(_COOKIE_DIR, f"full_{ts}_{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  [cookies] saved: {path} ({len(cookies)} cookies)")
    except Exception as e:
        print(f"  [cookies] save err: {e}")


# ── .env 加载 ──


def load_env():
    """从 .env 文件加载环境变量（零依赖）。
    只在 os.environ 中尚未设置该 KEY 时填入（真实环境变量优先）。
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


class RegistrationCancelled(Exception):
    pass


def _strip_config_comments(obj):
    """去除 config 中以 // 或 # 开头的注释键。"""
    if not isinstance(obj, dict):
        return obj
    return {
        k: v
        for k, v in obj.items()
        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
    }


def merge_config_with_example():
    """每次启动时自动合并 config.example.json 的新增字段到 config.json。

    - 保留用户已在 config.json 中自定义的值
    - 补充 config.example.json 中新增的字段（用户 config.json 中不存在的键）
    - 不删除用户 config.json 中已有的任何键
    - 写回时保持 config.example.json 的字段顺序和注释结构
    """
    if not os.path.exists(CONFIG_EXAMPLE_FILE):
        return
    if not os.path.exists(CONFIG_FILE):
        return

    try:
        with open(CONFIG_EXAMPLE_FILE, "r", encoding="utf-8") as f:
            example_raw = json.load(f)
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_raw = json.load(f)
    except Exception:
        return

    if not isinstance(example_raw, dict) or not isinstance(user_raw, dict):
        return

    example = _strip_config_comments(example_raw)
    user = _strip_config_comments(user_raw)

    # 检查是否有新增字段需要合并
    new_keys = [k for k in example if k not in user]
    if not new_keys:
        return

    # 以 example 为骨架（保持字段顺序+注释），合并用户已有值
    merged = {}
    for k, v in example_raw.items():
        if k.startswith("//") or k.startswith("#"):
            # 保留注释行
            merged[k] = v
        elif k in user:
            merged[k] = user[k]
        else:
            # example 中新增的字段，使用 example 的默认值
            merged[k] = v

    # 追加用户 config.json 中存在但 example 中没有的键
    for k, v in user_raw.items():
        if k not in merged:
            merged[k] = v

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=4, ensure_ascii=False)
        print(f"[config] 已自动合并 {len(new_keys)} 个新增配置项: {', '.join(new_keys)}")
    except Exception as e:
        print(f"[config] 合并配置失败: {e}")


def load_config():
    load_env()
    # 每次加载前自动合并 config.example.json 中的新增字段
    merge_config_with_example()
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Allow "// comment" keys in config.example.json / config.json templates.
            if isinstance(loaded, dict):
                loaded = {
                    k: v
                    for k, v in loaded.items()
                    if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
                }
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    proxy = config.get("proxy", "")
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "bearer") or "bearer").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email v1.8.x: POST /api/new_address -> {address,jwt}"""
    global _cf_domain_index
    url = f"{api_base}/api/new_address"
    payload = {}
    try:
        # 在多个域名之间轮换，降低单域偶发不收件导致的失败率
        domains = [x.strip() for x in re.split(r"[,，\s]+", str(config.get("defaultDomains", "") or "")) if x.strip()]
        if domains:
            payload["domain"] = domains[_cf_domain_index % len(domains)]
            _cf_domain_index += 1
    except Exception:
        pass
    resp = http_post(url, json=payload, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare /api/new_address 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare /api/new_address 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return r"D:\注册机\3255d5ee6e702db9220a897df64635a1ec9df644\vendor\grok2api\data\token.json"


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def _grok2api_push_import(body, provider_path, mgmt_key, base_url, log_callback, retries=3, retry_delay=2):
    """Push credentials to grok2api import/body endpoint via management key auth."""
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"
    url = f"{base_url}/api/admin/v1/accounts/{provider_path}/import/body"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = http_post(url, headers=headers, data=body, timeout=30, proxies={})
            if resp.status_code == 401:
                if log_callback:
                    log_callback(f"[grok2api] 认证失败，检查 management_key 配置")
                return False
            resp.raise_for_status()
            if log_callback:
                log_callback(f"[+] grok2api {provider_path} 导入成功: {url}")
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_delay)
    if log_callback:
        log_callback(f"[!] grok2api {provider_path} 导入失败({retries}次): {last_exc}")
    return False


def _grok2api_find_account_id(email, base_url, mgmt_key, log_callback=None, retries=3, retry_delay=2):
    """Query grok2api accounts list to find a web account ID by email."""
    if not email:
        return None
    headers = {}
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"
    url = f"{base_url}/api/admin/v1/accounts"
    params = {"search": email, "provider": "grok_web", "pageSize": 5}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = http_get(url, headers=headers, params=params, timeout=15, proxies={})
            if resp.status_code == 401:
                if log_callback:
                    log_callback("[grok2api] 查询账号认证失败")
                return None
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("items", [])
            email_lower = email.lower()
            for item in items:
                if (item.get("email") or "").lower() == email_lower:
                    account_id = item.get("id")
                    if account_id:
                        if log_callback:
                            log_callback(f"[grok2api] 找到账号 ID: {account_id} ({email})")
                        return int(account_id)
            if log_callback:
                log_callback(f"[grok2api] 未找到匹配 email 的 web 账号: {email}")
            return None
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_delay)
    if log_callback:
        log_callback(f"[grok2api] 查询账号失败({retries}次): {last_exc}")
    return None


def _grok2api_enable_nsfw(account_id, base_url, mgmt_key, log_callback=None, retries=3, retry_delay=2):
    """Call grok2api to accept terms + set birthday + enable NSFW via batch scripts endpoint."""
    import json as _json
    headers = {"Content-Type": "application/json"}
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"
    url = f"{base_url}/api/admin/v1/accounts/web/run-scripts"
    payload = {
        "ids": [str(account_id)],
        "actions": {
            "acceptTerms": True,
            "setBirthDate": True,
            "enableNSFW": True,
        },
    }
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = http_post(url, headers=headers, data=_json.dumps(payload), timeout=120, proxies={})
            if resp.status_code == 401:
                if log_callback:
                    log_callback("[grok2api] 账号工具认证失败")
                return False
            resp.raise_for_status()
            if log_callback:
                log_callback(f"[grok2api] 账号工具执行成功 (account_id={account_id})")
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_delay)
    if log_callback:
        log_callback(f"[grok2api] 账号工具执行失败({retries}次, account_id={account_id}): {last_exc}")
    return False


def grok2api_enable_nsfw(email, base_url, mgmt_key, log_callback=None):
    """Find web account by email and enable NSFW (sync, must return result)."""
    if not config.get("enable_nsfw", True):
        return
    if not email:
        if log_callback:
            log_callback("[grok2api] enable_nsfw 跳过: 无 email")
        return
    retries = int(config.get("grok2api_import_retries", 3))
    retry_delay = float(config.get("grok2api_import_retry_delay", 2))
    account_id = _grok2api_find_account_id(email, base_url, mgmt_key, log_callback, retries, retry_delay)
    if not account_id:
        return
    _grok2api_enable_nsfw(account_id, base_url, mgmt_key, log_callback, retries, retry_delay)


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    """Push SSO to grok2api Grok Web & Grok Console via import API (从画面中导入账号 流程)."""
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_import_base", "") or "").strip().rstrip("/")
    mgmt_key = str(config.get("grok2api_import_management_key", "") or "").strip()
    if not base:
        if log_callback:
            log_callback("[Debug] grok2api_import_base 未配置，跳过远程导入")
        return False

    web_ok = _grok2api_push_import(
        body=token,
        provider_path="web",
        mgmt_key=mgmt_key,
        base_url=base,
        log_callback=log_callback,
        retries=int(config.get("grok2api_import_retries", 3)),
        retry_delay=float(config.get("grok2api_import_retry_delay", 2)),
    )
    _grok2api_push_import(
        body=token,
        provider_path="console",
        mgmt_key=mgmt_key,
        base_url=base,
        log_callback=log_callback,
        retries=int(config.get("grok2api_import_retries", 3)),
        retry_delay=float(config.get("grok2api_import_retry_delay", 2)),
    )
    if web_ok and email:
        grok2api_enable_nsfw(email, base, mgmt_key, log_callback)
    return True


def _add_token_to_grok2api_pools_sync(raw_token, email="", log_callback=None):
    # SSO 账本只写 accounts_cli.txt；不再本地备份 tokens/grok/
    if config.get("grok2api_auto_add_local", False):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_import_enabled", True):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 导入失败: {exc}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None, sync=False):
    """Push SSO into grok2api pools. sync=True forces synchronous execution (import + NSFW)."""
    if not sync and PERF_FLAGS.get("async_side_effects", True):
        def _job():
            try:
                _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] grok2api side-effect 异常: {exc}")
        try:
            _get_side_effect_pool().submit(_job)
            if log_callback:
                log_callback("[*] grok2api 导入已异步提交")
            return
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 异步提交失败，同步写入: {exc}")
    _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)


CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-images",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
]


def create_browser_options():
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    # Apply config.json "proxy" to Chromium. Without this, only HTTP helpers
    # used get_proxies(); the browser itself fell through to system/env proxy.
    proxy = (config.get("proxy") or "").strip()
    if proxy:
        try:
            from urllib.parse import urlparse

            u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            host = u.hostname or ""
            if host:
                # SOCKS5/SOCKS5h default 1080, HTTPS 443, HTTP/unknown 80
                scheme = (u.scheme or "http").lower()
                if u.port:
                    port = u.port
                elif scheme in ("socks5", "socks5h", "socks4"):
                    port = 1080
                elif scheme == "https":
                    port = 443
                else:
                    port = 80
                # Chromium --proxy-server cannot embed user:pass
                options.set_argument(f"--proxy-server={scheme}://{host}:{port}")
        except Exception as e:
            print(f"  [proxy] set browser proxy failed: {e}")
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def human_sleep(mean_seconds, cancel_callback=None):
    """高斯分布人类化延迟，sigma=mean*0.3，clamp [mean*0.5, mean*2.0]。

    PERF_FLAGS sleep_scale / fast 可压缩批量注册等待。
    """
    scale = float(PERF_FLAGS.get("sleep_scale", 1.0) or 1.0)
    if PERF_FLAGS.get("fast"):
        scale = min(scale, 0.15)
    mean_seconds = max(0.0, float(mean_seconds) * scale)
    if mean_seconds <= 0.01:
        raise_if_cancelled(cancel_callback)
        return
    try:
        delay = random.gauss(mean_seconds, mean_seconds * 0.3)
    except Exception:
        delay = mean_seconds
    delay = max(mean_seconds * 0.5, min(mean_seconds * 2.0, delay))
    sleep_with_cancel(delay, cancel_callback)



def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取token失败: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


# ──────────────────────── CloudMail (maillab/cloud-mail) ────────────────────────
# API 前缀: /api/（所有接口均挂载在 /api/ 下）
# 认证格式: Authorization: <token>（不带 Bearer 前缀）
# 公开 token 通过 /api/public/genToken 获取（需管理员账号）

def get_cloudmail_url():
    return str(os.environ.get("CLOUDMAIL_URL") or config.get("cloudmail_url", "") or "").rstrip("/")


def get_cloudmail_password():
    return os.environ.get("CLOUDMAIL_PASSWORD") or config.get("cloudmail_password", "")


def get_cloudmail_admin_email():
    return str(os.environ.get("CLOUDMAIL_ADMIN_EMAIL") or config.get("cloudmail_admin_email", "") or "").strip()


def cloudmail_login(url, email, password):
    """POST /api/login -> JWT string"""
    resp = http_post(
        f"{url}/api/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            jwt = token_data.get("token")
            if jwt:
                return jwt
    raise Exception(f"CloudMail 登录失败: {str(data)[:200]}")


def cloudmail_register(url, email, password, turnstile_token=""):
    """POST /api/register -> 注册用户+账号"""
    payload = {"email": email, "password": password}
    if turnstile_token:
        payload["token"] = turnstile_token
    resp = http_post(
        f"{url}/api/register",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") != 200:
        raise Exception(f"CloudMail 注册失败: {data.get('message', str(data))}")
    return data


def cloudmail_gen_public_token(url, admin_email, admin_password):
    """POST /api/public/genToken -> 公开 API token (UUID)"""
    resp = http_post(
        f"{url}/api/public/genToken",
        json={"email": admin_email, "password": admin_password},
        headers={"Content-Type": "application/json"},
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            return token_data.get("token")
    raise Exception(f"CloudMail 获取公开 token 失败: {str(data)[:200]}")


def cloudmail_public_email_list(url, public_token, to_email="", size=20):
    """POST /api/public/emailList -> 公开邮件查询（需公开 token，Authorization: <token>）"""
    payload = {"size": size}
    if to_email:
        payload["toEmail"] = to_email
    resp = http_post(
        f"{url}/api/public/emailList",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": public_token,
        },
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("code") == 200:
            return data.get("data", [])
        raise Exception(f"CloudMail 邮件查询失败: {data.get('message', str(data))}")
    return []


def _cloudmail_get_shared_token(force_refresh=False):
    """获取或刷新共享的公开 token（线程安全单例）"""
    global _cloudmail_public_token
    with _cloudmail_public_token_lock:
        if _cloudmail_public_token and not force_refresh:
            return _cloudmail_public_token
        url = get_cloudmail_url()
        admin_email = get_cloudmail_admin_email()
        admin_password = get_cloudmail_password()
        if not url or not admin_email or not admin_password:
            raise Exception("CloudMail 配置不完整")
        token = cloudmail_gen_public_token(url, admin_email, admin_password)
        if not token:
            raise Exception("CloudMail 公开 token 为空")
        _cloudmail_public_token = token
        return token


def _cloudmail_extract_code_from_messages(messages, seen_attempts, log_callback=None):
    if log_callback:
        log_callback(f"[Debug] CloudMail 本轮邮件数量: {len(messages)}")
    for msg in messages:
        msg_id = msg.get("emailId") or msg.get("id") or msg.get("messageId")
        if not msg_id:
            continue
        attempt = int(seen_attempts.get(msg_id, 0))
        if attempt >= 5:
            continue
        seen_attempts[msg_id] = attempt + 1
        parts = []
        for field in ("content", "text", "textContent", "text_content", "body", "snippet", "intro"):
            value = msg.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(value)
        html_val = msg.get("html") or msg.get("htmlContent") or msg.get("html_content")
        if isinstance(html_val, str):
            parts.append(re.sub(r"<[^>]+>", " ", html_val))
        elif isinstance(html_val, list):
            for html_part in html_val:
                if isinstance(html_part, str):
                    parts.append(re.sub(r"<[^>]+>", " ", html_part))
        subject = str(msg.get("subject", "") or "")
        combined = "\n".join(parts)
        if log_callback:
            log_callback(f"[Debug] CloudMail 收到邮件: {subject}")
        code = extract_verification_code(combined, subject)
        if code:
            return code
        if log_callback:
            log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
    return None


def _cloudmail_poll_code_once(email, public_token, seen_attempts, log_callback=None):
    messages = cloudmail_public_email_list(
        get_cloudmail_url(),
        public_token,
        to_email=email,
        size=20,
    )
    return _cloudmail_extract_code_from_messages(messages, seen_attempts, log_callback)


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=300,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    # 使用配置的 mail_poll_interval，默认 0.3s
    if poll_interval is None:
        poll_interval = max(0.1, float(config.get("mail_poll_interval", 0.3) or 0.3))
    url = get_cloudmail_url()
    if not url:
        raise Exception("CloudMail URL 未配置")
    # 获取共享公开 token（所有线程共用同一个，避免并发覆盖）
    try:
        public_token = _cloudmail_get_shared_token()
    except Exception as exc:
        raise Exception(f"CloudMail 获取公开 token 失败: {exc}")
    if log_callback:
        log_callback("[Debug] CloudMail 公开 token 获取成功")
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 60
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        # 统一使用 poll_interval（0.3s 短轮询，无需前加速）
        current_interval = poll_interval
        # 用完整邮箱地址查询（公开 API 的 toEmail 需要完整地址）
        try:
            code = _cloudmail_poll_code_once(email, public_token, seen_attempts, log_callback)
        except Exception as exc:
            err_msg = str(exc)
            if log_callback:
                log_callback(f"[Debug] CloudMail 邮件查询失败: {err_msg}")
            # token 失效时，刷新共享 token（加锁，多线程只刷新一次）
            if "token" in err_msg.lower() or "401" in err_msg:
                try:
                    public_token = _cloudmail_get_shared_token(force_refresh=True)
                    if log_callback:
                        log_callback("[Debug] CloudMail 公开 token 已刷新")
                except Exception:
                    pass
            sleep_with_cancel(current_interval, cancel_callback)
            continue
        if code:
            if log_callback:
                log_callback(f"[*] CloudMail 从邮件中提取到验证码: {code}")
            return code
        sleep_with_cancel(current_interval, cancel_callback)
    raise Exception(f"CloudMail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── OpenAI-CPA 内存池邮箱 ────────────────────────

def get_openai_cpa_webhook_secret():
    return str(
        os.environ.get("EMAIL_WEBHOOK_SECRET")
        or config.get("openai_cpa_webhook_secret", "")
        or ""
    ).strip()


def openai_cpa_cloudmail_fallback_enabled():
    raw = os.environ.get("OPENAI_CPA_CLOUDMAIL_FALLBACK")
    if raw is None:
        raw = config.get("openai_cpa_cloudmail_fallback", True)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def openai_cpa_cloudmail_fallback_available():
    return openai_cpa_cloudmail_fallback_enabled() and all(
        (get_cloudmail_url(), get_cloudmail_admin_email(), get_cloudmail_password())
    )


def _prune_openai_cpa_code_pool_locked(now=None):
    now = time.time() if now is None else float(now)
    stale_keys = [
        email
        for email, entry in _openai_cpa_code_pool.items()
        if now - float(entry.get("received_at", 0) or 0) > _OPENAI_CPA_POOL_TTL_SECONDS
    ]
    for email in stale_keys:
        _openai_cpa_code_pool.pop(email, None)

    overflow = len(_openai_cpa_code_pool) - _OPENAI_CPA_POOL_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(
            _openai_cpa_code_pool.items(),
            key=lambda item: float(item[1].get("received_at", 0) or 0),
        )[:overflow]
        for email, _ in oldest:
            _openai_cpa_code_pool.pop(email, None)


def openai_cpa_memory_pool_stats():
    with _openai_cpa_code_pool_lock:
        _prune_openai_cpa_code_pool_locked()
        return {"pending": len(_openai_cpa_code_pool), "ttl_seconds": _OPENAI_CPA_POOL_TTL_SECONDS}


def _decode_webhook_raw_email(raw_content):
    subject = ""
    body_parts = []
    try:
        message = Parser(policy=policy.default).parsestr(raw_content)
        raw_subject = str(message.get("Subject", "") or "")
        if raw_subject:
            subject = str(make_header(decode_header(raw_subject)))

        parts = message.walk() if message.is_multipart() else [message]
        for part in parts:
            content_type = str(part.get_content_type() or "").lower()
            disposition = str(part.get_content_disposition() or "").lower()
            if content_type not in {"text/plain", "text/html"} or disposition == "attachment":
                continue
            try:
                text = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                else:
                    text = str(payload or "")
            if content_type == "text/html":
                text = re.sub(r"<[^>]+>", " ", str(text))
            if str(text).strip():
                body_parts.append(str(text))
    except Exception:
        pass
    return subject, "\n".join(body_parts) or raw_content


def accept_openai_cpa_webhook(payload, provided_secret):
    """校验 Worker 推送并将验证码短暂写入内存池。"""
    expected_secret = get_openai_cpa_webhook_secret()
    if not expected_secret:
        return 503, {"ok": False, "error": "OpenAI-CPA Webhook 密钥未配置"}
    if not hmac.compare_digest(str(provided_secret or ""), expected_secret):
        return 401, {"ok": False, "error": "Webhook 密钥无效"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "请求体必须是 JSON 对象"}

    to_addr = str(payload.get("to_addr") or "").strip().lower()
    raw_content = payload.get("raw_content")
    message_id = str(payload.get("message_id") or "")[:512]
    if len(to_addr) > 320 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}", to_addr):
        return 400, {"ok": False, "error": "收件邮箱格式无效"}
    if not isinstance(raw_content, str) or not raw_content:
        return 400, {"ok": False, "error": "raw_content 不能为空"}
    if len(raw_content.encode("utf-8", errors="ignore")) > 2 * 1024 * 1024:
        return 413, {"ok": False, "error": "邮件内容超过 2MB 限制"}

    subject, decoded_text = _decode_webhook_raw_email(raw_content)
    code = extract_verification_code(decoded_text, subject)
    if not code:
        return 202, {"ok": True, "stored": False, "reason": "邮件中未识别到验证码"}

    with _openai_cpa_code_pool_lock:
        _prune_openai_cpa_code_pool_locked()
        _openai_cpa_code_pool[to_addr] = {
            "code": code,
            "message_id": message_id,
            "received_at": time.time(),
        }
    return 200, {"ok": True, "stored": True}


def openai_cpa_get_oai_code(
    dev_token,
    email,
    timeout=300,
    poll_interval=0.3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    target_email = str(email or "").strip().lower()
    memory_enabled = bool(get_openai_cpa_webhook_secret())
    fallback_enabled = openai_cpa_cloudmail_fallback_enabled()
    fallback_available = openai_cpa_cloudmail_fallback_available()
    if not memory_enabled and not fallback_available:
        if fallback_enabled:
            raise Exception("OpenAI-CPA 本地回退已开启，但 CloudMail URL、管理员邮箱或密码不完整")
        raise Exception("OpenAI-CPA 未配置 Webhook 通信密钥，也未开启可用的 CloudMail 本地回退")

    deadline = time.time() + timeout
    next_resend_at = time.time() + 60
    interval = max(0.1, min(float(poll_interval or 0.3), 1.0))
    public_token = None
    seen_attempts = {}
    if fallback_available:
        try:
            public_token = _cloudmail_get_shared_token()
        except Exception as exc:
            if not memory_enabled:
                raise Exception(f"OpenAI-CPA CloudMail 本地回退初始化失败: {exc}")
            if log_callback:
                log_callback(f"[Debug] CloudMail 本地回退暂不可用，将继续等待内存池: {exc}")
    if log_callback:
        sources = []
        if memory_enabled:
            sources.append("Webhook 内存池")
        if public_token:
            sources.append("CloudMail API 回退")
        log_callback(f"[Debug] OpenAI-CPA 等待验证码 ({' + '.join(sources)}): {target_email}")

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if memory_enabled:
            with _openai_cpa_code_pool_lock:
                _prune_openai_cpa_code_pool_locked()
                entry = _openai_cpa_code_pool.pop(target_email, None)
            if entry and entry.get("code"):
                code = str(entry["code"]).strip()
                if log_callback:
                    log_callback(f"[*] OpenAI-CPA 内存池提取到验证码: {code}")
                return code

        if public_token:
            try:
                code = _cloudmail_poll_code_once(
                    target_email,
                    public_token,
                    seen_attempts,
                    log_callback,
                )
                if code:
                    if log_callback:
                        log_callback(f"[*] OpenAI-CPA 通过 CloudMail 本地回退提取到验证码: {code}")
                    return code
            except Exception as exc:
                err_msg = str(exc)
                if log_callback:
                    log_callback(f"[Debug] OpenAI-CPA CloudMail 回退查询失败: {err_msg}")
                if "token" in err_msg.lower() or "401" in err_msg:
                    try:
                        public_token = _cloudmail_get_shared_token(force_refresh=True)
                    except Exception:
                        pass

        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        sleep_with_cancel(interval, cancel_callback)

    raise Exception(f"OpenAI-CPA 在 {timeout}s 内未从内存池或 CloudMail 回退收到验证码")


# ──────────────────────── Generator.email ────────────────────────

GENERATOR_EMAIL_BASE_URL = "https://generator.email"


def generator_email_create_email():
    """通过 generator.email 创建临时邮箱，返回 (email, surl)"""
    from curl_cffi import requests as cf_requests
    from curl_cffi import CurlHttpVersion

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    timeout = 30
    proxy = config.get("proxy") or None

    resp = cf_requests.get(
        GENERATOR_EMAIL_BASE_URL,
        headers=headers,
        proxies={"https": proxy, "http": proxy} if proxy else None,
        timeout=timeout,
        impersonate="chrome110",
        http_version=CurlHttpVersion.V1_1,
    )
    if resp.status_code != 200:
        raise Exception(f"GeneratorEmail 获取页面失败 (HTTP {resp.status_code})")

    html = resp.text or ""
    # 解析邮箱地址
    match = re.search(r'id="email_ch_text"[^>]*>([^<]+)</span>', html, re.I)
    if not match:
        match = re.search(r'id="email_ch_text"[^>]*>([^<]+)<', html, re.I)
    if match:
        email = match.group(1).strip()
    else:
        user_match = re.search(r'id="userName"[^>]*value="([^"]+)"', html, re.I)
        domain_match = re.search(r'id="domainName2"[^>]*value="([^"]+)"', html, re.I)
        if user_match and domain_match:
            email = f"{user_match.group(1).strip()}@{domain_match.group(1).strip()}"
        else:
            raise Exception("GeneratorEmail 无法从页面解析邮箱地址")

    # 构造 surl (session URL)
    username, domain = email.split("@", 1)
    safe_user = re.sub(r"[^a-zA-Z_0-9.-]", "", username).lower()
    surl = f"{domain.lower()}/{safe_user}"
    return email, surl


def generator_email_get_inbox_links(surl):
    """获取 generator.email 收件箱中的邮件链接列表"""
    from curl_cffi import requests as cf_requests
    from curl_cffi import CurlHttpVersion

    if not surl:
        return []

    mailbox_url = f"{GENERATOR_EMAIL_BASE_URL}/{surl.lstrip('/')}"
    cookies = {"surl": surl}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    proxy = config.get("proxy") or None

    resp = cf_requests.get(
        mailbox_url,
        headers=headers,
        cookies=cookies,
        proxies={"https": proxy, "http": proxy} if proxy else None,
        timeout=30,
        impersonate="chrome110",
        http_version=CurlHttpVersion.V1_1,
    )
    if resp.status_code != 200:
        return []

    html = resp.text or ""
    pattern = r'<a href="([^"]+)"[^>]*>([\s\S]*?)</a>'
    links = re.findall(pattern, html)
    results = []
    for href, _inner_html in links:
        m_id = href.split("/")[-1]
        results.append({"href": href, "id": m_id})
    return results


def generator_email_get_code_from_detail(href, surl):
    """从 generator.email 邮件详情页提取验证码"""
    from curl_cffi import requests as cf_requests
    from curl_cffi import CurlHttpVersion
    from html import unescape

    if not href:
        return None

    detail_url = (
        f"{GENERATOR_EMAIL_BASE_URL}/{href.lstrip('/')}"
        if not href.startswith("http")
        else href
    )
    cookies = {"surl": surl}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    proxy = config.get("proxy") or None

    resp = cf_requests.get(
        detail_url,
        headers=headers,
        cookies=cookies,
        proxies={"https": proxy, "http": proxy} if proxy else None,
        timeout=30,
        impersonate="chrome110",
        http_version=CurlHttpVersion.V1_1,
    )
    if resp.status_code != 200:
        return None

    raw_html = resp.text or ""
    clean_text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw_html)
    clean_text = re.sub(r"<[^>]+>", " ", clean_text)
    clean_text = unescape(clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    return extract_verification_code(clean_text)


def generator_email_get_email_and_token():
    """generator.email 创建邮箱入口"""
    email, surl = generator_email_create_email()
    if not email or not surl:
        raise Exception("GeneratorEmail 创建邮箱失败")
    print(f"[*] 已创建 GeneratorEmail 邮箱: {email}")
    return email, surl


def generator_email_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """generator.email 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            mail_links = generator_email_get_inbox_links(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] GeneratorEmail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for item in mail_links:
            m_id = item.get("id")
            m_href = item.get("href")
            if not m_id or m_id in seen_ids:
                continue
            seen_ids.add(m_id)
            try:
                code = generator_email_get_code_from_detail(m_href, dev_token)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] GeneratorEmail 获取邮件详情失败: {exc}")
                continue
            if code:
                if log_callback:
                    log_callback(f"[*] GeneratorEmail 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"GeneratorEmail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Inboxes.com ────────────────────────

INBOXES_API_BASE = "https://inboxes.com/api/v2"


def inboxes_com_create_email():
    """创建 Inboxes.com 邮箱，返回 (email, user_id)"""
    from curl_cffi import requests as cf_requests

    session = cf_requests.Session(impersonate="chrome120")
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://inboxes.com",
        "referer": "https://inboxes.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    proxy = config.get("proxy") or None
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    resp = session.post(f"{INBOXES_API_BASE}/inbox", headers=headers, json={}, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        email = data.get("inbox")
        user_id = session.cookies.get_dict().get("user_id")
        if email and user_id:
            return email, user_id
    raise Exception(f"Inboxes.com 创建邮箱失败 (HTTP {resp.status_code})")


def inboxes_com_get_inbox(email_address, user_id):
    """获取 Inboxes.com 收件箱"""
    from curl_cffi import requests as cf_requests

    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    cookies = {"user_id": user_id}
    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{INBOXES_API_BASE}/inbox/{email_address}",
        headers=headers, cookies=cookies, proxies=proxies, timeout=10,
        impersonate="chrome120",
    )
    if resp.status_code == 200:
        return resp.json().get("msgs", [])
    return []


def inboxes_com_get_message_body(uid, user_id):
    """获取 Inboxes.com 邮件正文"""
    from curl_cffi import requests as cf_requests

    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    cookies = {"user_id": user_id}
    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"https://inboxes.com/read/{uid}",
        headers=headers, cookies=cookies, proxies=proxies, timeout=10,
        impersonate="chrome120",
    )
    if resp.status_code == 200:
        return resp.text
    return ""


def inboxes_com_get_email_and_token():
    """Inboxes.com 创建邮箱入口"""
    email, user_id = inboxes_com_create_email()
    if not email or not user_id:
        raise Exception("Inboxes.com 创建邮箱失败")
    print(f"[*] 已创建 Inboxes.com 邮箱: {email}")
    return email, user_id


def inboxes_com_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """Inboxes.com 轮询验证码"""
    from html import unescape
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            msgs = inboxes_com_get_inbox(email, dev_token)
            if log_callback:
                log_callback(f"[Debug] Inboxes.com 检查收件箱 {email}，返回 {len(msgs)} 封邮件")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Inboxes.com 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for m in msgs:
            m_id = str(m.get("uid", ""))
            if not m_id or m_id in seen_ids:
                continue
            seen_ids.add(m_id)
            subject = str(m.get("s", ""))
            sender = str(m.get("f", "")).lower()
            if log_callback:
                log_callback(f"[Debug] Inboxes.com 发现邮件: uid={m_id}, from={sender}, subject={subject}")
            if "openai" not in sender and "openai" not in subject.lower() and "chatgpt" not in subject.lower():
                continue
            try:
                raw_body = inboxes_com_get_message_body(m_id, user_id=dev_token)
                clean_body = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw_body)
                clean_body = re.sub(r"<[^>]+>", " ", clean_body)
                clean_body = unescape(clean_body)
                clean_body = re.sub(r"\s+", " ", clean_body).strip()
                combined_text = subject + " \n " + clean_body
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Inboxes.com 获取邮件详情失败: {exc}")
                continue
            code = extract_verification_code(combined_text, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Inboxes.com 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Inboxes.com 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Tempmail.lol ────────────────────────

TEMPMAIL_LOL_API_BASE = "https://api.tempmail.lol/v2"


def tempmail_lol_create_email():
    """创建 Tempmail.lol 邮箱，返回 (email, token)"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.post(
        f"{TEMPMAIL_LOL_API_BASE}/inbox/create",
        headers={"Accept": "application/json"},
        json=None,
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code in (200, 201):
        data = resp.json()
        email = str(data.get("address", "")).strip()
        token = str(data.get("token", "")).strip()
        if email and token:
            return email, token
    raise Exception(f"Tempmail.lol 创建邮箱失败 (HTTP {resp.status_code})")


def tempmail_lol_get_inbox(token):
    """获取 Tempmail.lol 收件箱"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{TEMPMAIL_LOL_API_BASE}/inbox",
        params={"token": token},
        headers={"Accept": "application/json"},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict):
            return data.get("emails", [])
    return []


def tempmail_lol_get_email_and_token():
    """Tempmail.lol 创建邮箱入口"""
    email, token = tempmail_lol_create_email()
    if not email or not token:
        raise Exception("Tempmail.lol 创建邮箱失败")
    print(f"[*] 已创建 Tempmail.lol 邮箱: {email}")
    return email, token


def tempmail_lol_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """Tempmail.lol 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            email_list = tempmail_lol_get_inbox(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Tempmail.lol 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in email_list:
            msg_date = str(msg.get("date", 0))
            if not msg_date or msg_date in seen_ids:
                continue
            seen_ids.add(msg_date)
            sender = str(msg.get("from", "")).lower()
            subject = str(msg.get("subject", ""))
            body = str(msg.get("body", ""))
            html = re.sub(r"<[^>]+>", " ", str(msg.get("html") or ""))
            content = "\n".join([sender, subject, body, html])
            if "openai" not in sender and "openai" not in content.lower():
                continue
            safe_content = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", content)
            code = extract_verification_code(safe_content, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Tempmail.lol 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Tempmail.lol 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── TempMail.org ────────────────────────

TEMPMAIL_ORG_BASE = "https://web2.temp-mail.org"


def tempmail_org_create_email():
    """创建 TempMail.org 邮箱，返回 (email, token)"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = cf_requests.Session(impersonate="chrome110")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://web2.temp-mail.org",
        "Referer": "https://web2.temp-mail.org",
    })
    if proxies:
        session.proxies.update(proxies)

    resp = session.post(f"{TEMPMAIL_ORG_BASE}/mailbox", timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("mailbox"), data.get("token")
    raise Exception(f"TempMail.org 创建邮箱失败 (HTTP {resp.status_code})")


def tempmail_org_get_inbox(token):
    """获取 TempMail.org 收件箱"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{TEMPMAIL_ORG_BASE}/messages",
        headers={"Authorization": f"Bearer {token}"},
        proxies=proxies,
        timeout=30,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("messages", [])
    return []


def tempmail_org_get_email_and_token():
    """TempMail.org 创建邮箱入口"""
    email, token = tempmail_org_create_email()
    if not email or not token:
        raise Exception("TempMail.org 创建邮箱失败")
    print(f"[*] 已创建 TempMail.org 邮箱: {email}")
    return email, token


def tempmail_org_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """TempMail.org 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            email_list = tempmail_org_get_inbox(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] TempMail.org 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in email_list:
            msg_id = str(msg.get("_id", msg.get("id", "")))
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            subject = str(msg.get("subject", ""))
            body_preview = str(msg.get("bodyPreview", ""))
            content = "\n".join([subject, body_preview])
            code = extract_verification_code(content, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] TempMail.org 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"TempMail.org 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── FreeMail ────────────────────────

def freemail_get_api_url():
    return str(config.get("freemail_api_url", "") or "").rstrip("/")


def freemail_get_api_token():
    return str(config.get("freemail_api_token", "") or "")


def freemail_create_email():
    """通过 FreeMail API 创建邮箱，返回 (email, "")"""
    api_url = freemail_get_api_url()
    api_token = freemail_get_api_token()
    if not api_url:
        raise Exception("FreeMail API URL 未配置 (freemail_api_url)")
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    # FreeMail 使用 defaultDomains 生成随机邮箱
    raw = str(config.get("defaultDomains", "") or "")
    domains = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
    if not domains:
        raise Exception("FreeMail 需要在 defaultDomains 中配置可用域名")
    global _cf_domain_index
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    username = generate_username(10)
    email = f"{username}@{domain}"

    resp = cf_requests.post(
        f"{api_url}/api/create",
        json={"email": email},
        headers=headers,
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    return email, ""


def freemail_get_inbox(email_address):
    """获取 FreeMail 收件箱"""
    api_url = freemail_get_api_url()
    api_token = freemail_get_api_token()
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    resp = cf_requests.get(
        f"{api_url}/api/emails",
        params={"mailbox": email_address, "limit": 20},
        headers=headers,
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        raw_data = resp.json()
        emails_list = (
            raw_data.get("data") or raw_data.get("emails")
            or raw_data.get("messages") or raw_data.get("results") or []
            if isinstance(raw_data, dict) else raw_data
        )
        if isinstance(emails_list, list):
            return emails_list
    return []


def freemail_get_email_detail(mail_id):
    """获取 FreeMail 邮件详情"""
    api_url = freemail_get_api_url()
    api_token = freemail_get_api_token()
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    resp = cf_requests.get(
        f"{api_url}/api/email/{mail_id}",
        headers=headers,
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        return resp.json()
    return None


def freemail_get_email_and_token():
    """FreeMail 创建邮箱入口"""
    email, token = freemail_create_email()
    if not email:
        raise Exception("FreeMail 创建邮箱失败")
    print(f"[*] 已创建 FreeMail 邮箱: {email}")
    return email, token


def freemail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """FreeMail 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            emails_list = freemail_get_inbox(email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] FreeMail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for mail in emails_list:
            mail_id = str(mail.get("id") or mail.get("timestamp") or mail.get("subject") or "")
            if not mail_id or mail_id in seen_ids:
                continue
            seen_ids.add(mail_id)
            subject_text = str(mail.get("subject") or mail.get("title") or "")
            code = extract_verification_code(subject_text)
            if not code:
                code = str(mail.get("code") or mail.get("verification_code") or "")
            if not code:
                try:
                    detail = freemail_get_email_detail(mail_id)
                    if detail:
                        detail_content = "\n".join(filter(None, [
                            str(detail.get("subject") or ""),
                            str(detail.get("content") or ""),
                            re.sub(r"<[^>]+>", " ", str(detail.get("html_content") or "")),
                        ]))
                        code = extract_verification_code(detail_content)
                except Exception:
                    pass
            if code:
                if log_callback:
                    log_callback(f"[*] FreeMail 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"FreeMail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Catchmail.io ────────────────────────

CATCHMAIL_API_BASE = "https://api.catchmail.io"
CATCHMAIL_DOMAIN = "catchmail.io"


def catchmail_create_email():
    """创建 Catchmail.io 邮箱，返回 (email, token)。无需认证，直接选取地址。"""
    username = generate_username(10)
    email = f"{username}@{CATCHMAIL_DOMAIN}"
    return email, ""


def catchmail_get_inbox(email_address):
    """获取 Catchmail.io 收件箱"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{CATCHMAIL_API_BASE}/api/v1/mailbox",
        params={"address": email_address},
        headers={"Accept": "application/json"},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        try:
            data = resp.json()
            return data.get("messages", [])
        except Exception:
            return []
    return []


def catchmail_get_message_detail(message_id, mailbox):
    """获取 Catchmail.io 单封邮件详情"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{CATCHMAIL_API_BASE}/api/v1/message/{message_id}",
        params={"mailbox": mailbox},
        headers={"Accept": "application/json"},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return None
    return None


def catchmail_get_email_and_token():
    """Catchmail.io 创建邮箱入口"""
    email, token = catchmail_create_email()
    if not email:
        raise Exception("Catchmail.io 创建邮箱失败")
    print(f"[*] 已创建 Catchmail.io 邮箱: {email}")
    return email, token


def catchmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """Catchmail.io 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = catchmail_get_inbox(email)
            if log_callback:
                log_callback(f"[Debug] Catchmail.io 检查收件箱 {email}，返回 {len(messages)} 封邮件")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Catchmail.io 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = str(msg.get("id", ""))
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            sender = str(msg.get("from", "")).lower()
            subject = str(msg.get("subject", ""))
            combined = f"{sender}\n{subject}"
            if log_callback:
                log_callback(f"[Debug] Catchmail.io 发现邮件: id={msg_id}, from={sender}, subject={subject}")
            if "openai" not in combined.lower():
                continue
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Catchmail.io 从邮件中提取到验证码: {code}")
                return code
            try:
                detail = catchmail_get_message_detail(msg_id, email)
                if detail:
                    body_text = str(detail.get("body", {}).get("text", ""))
                    body_html = re.sub(r"<[^>]+>", " ", str(detail.get("body", {}).get("html", "")))
                    detail_content = "\n".join([subject, body_text, body_html])
                    code = extract_verification_code(detail_content, subject)
                    if code:
                        if log_callback:
                            log_callback(f"[*] Catchmail.io 从邮件详情中提取到验证码: {code}")
                        return code
            except Exception:
                pass
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Catchmail.io 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Guerrilla Mail ────────────────────────

GUERRILLA_MAIL_API_BASE = "http://api.guerrillamail.com/ajax.php"


def guerrilla_create_email():
    """创建 Guerrilla Mail 邮箱，返回 (email, session_token)"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    params = {"f": "get_email_address", "ip": "127.0.0.1", "agent": "Mozilla/5.0"}
    resp = cf_requests.get(
        GUERRILLA_MAIL_API_BASE,
        params=params,
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        data = resp.json()
        email = str(data.get("email_addr", "")).strip()
        session_token = str(data.get("sid_token", "")).strip()
        if email and session_token:
            return email, session_token
    raise Exception(f"Guerrilla Mail 创建邮箱失败 (HTTP {resp.status_code})")


def guerrilla_set_email_user(session_token, username):
    """设置 Guerrilla Mail 用户名"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        GUERRILLA_MAIL_API_BASE,
        params={"f": "set_email_user", "username": username, "sid_token": session_token},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        data = resp.json()
        return str(data.get("email_addr", "")).strip()
    return ""


def guerrilla_check_email(session_token):
    """获取 Guerrilla Mail 收件箱"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        GUERRILLA_MAIL_API_BASE,
        params={"f": "check_email", "seq": "0", "sid_token": session_token},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        try:
            data = resp.json()
            return data.get("list", [])
        except Exception:
            return []
    return []


def guerrilla_fetch_email(session_token, email_id):
    """获取 Guerrilla Mail 单封邮件详情"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        GUERRILLA_MAIL_API_BASE,
        params={"f": "fetch_email", "email_id": email_id, "sid_token": session_token},
        proxies=proxies,
        timeout=15,
        impersonate="chrome100",
    )
    if resp.status_code == 200:
        return resp.json()
    return None


def guerrilla_get_email_and_token():
    """Guerrilla Mail 创建邮箱入口"""
    email, session_token = guerrilla_create_email()
    if not email or not session_token:
        raise Exception("Guerrilla Mail 创建邮箱失败")
    print(f"[*] 已创建 Guerrilla Mail 邮箱: {email}")
    return email, session_token


def guerrilla_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """Guerrilla Mail 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            email_list = guerrilla_check_email(dev_token)
            if log_callback:
                log_callback(f"[Debug] Guerrilla Mail 检查收件箱，返回 {len(email_list)} 封邮件")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Guerrilla Mail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in email_list:
            mail_id = str(msg.get("mail_id", ""))
            if not mail_id or mail_id in seen_ids:
                continue
            seen_ids.add(mail_id)
            sender = str(msg.get("mail_from", "")).lower()
            subject = str(msg.get("mail_subject", ""))
            combined = f"{sender}\n{subject}"
            if log_callback:
                log_callback(f"[Debug] Guerrilla Mail 发现邮件: from={sender}, subject={subject}")
            if "openai" not in combined.lower():
                continue
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Guerrilla Mail 从邮件中提取到验证码: {code}")
                return code
            try:
                detail = guerrilla_fetch_email(dev_token, mail_id)
                if detail:
                    body_text = str(detail.get("mail_body", ""))
                    body_html = re.sub(r"<[^>]+>", " ", body_text)
                    detail_content = "\n".join([subject, body_html])
                    code = extract_verification_code(detail_content, subject)
                    if code:
                        if log_callback:
                            log_callback(f"[*] Guerrilla Mail 从邮件详情中提取到验证码: {code}")
                        return code
            except Exception:
                pass
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Guerrilla Mail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Mail.tm ────────────────────────

MAIL_TM_API_BASE = "https://api.mail.tm"


def mailtm_create_email():
    """创建 Mail.tm 邮箱，返回 (email, token)"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = cf_requests.Session(impersonate="chrome110")
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if proxies:
        session.proxies.update(proxies)

    # 1. 获取可用域名
    resp = session.get(f"{MAIL_TM_API_BASE}/domains", timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Mail.tm 获取域名失败 (HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        domains = resp.json().get("hydra:member", [])
    except Exception as exc:
        raise Exception(f"Mail.tm 域名响应解析失败: {exc}, body={resp.text[:200]}")
    if not domains:
        raise Exception("Mail.tm 无可用域名")
    domain = domains[0].get("domain")
    if not domain:
        raise Exception("Mail.tm 域名数据格式错误")

    # 2. 创建账号
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    resp = session.post(
        f"{MAIL_TM_API_BASE}/accounts",
        json={"address": address, "password": password},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise Exception(f"Mail.tm 创建账号失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    # 3. 获取 JWT token
    resp = session.post(
        f"{MAIL_TM_API_BASE}/token",
        json={"address": address, "password": password},
        timeout=15,
    )
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception as exc:
            raise Exception(f"Mail.tm token 响应解析失败: {exc}, body={resp.text[:200]}")
        token = data.get("token", "")
        if token:
            return address, token
        raise Exception(f"Mail.tm token 为空, response={resp.text[:200]}")
    raise Exception(f"Mail.tm 获取 token 失败 (HTTP {resp.status_code}): {resp.text[:200]}")


def mailtm_get_inbox(token):
    """获取 Mail.tm 收件箱"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{MAIL_TM_API_BASE}/messages",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return []
        return data.get("hydra:member", [])
    return []


def mailtm_get_message_detail(message_id, token):
    """获取 Mail.tm 单封邮件详情"""
    from curl_cffi import requests as cf_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cf_requests.get(
        f"{MAIL_TM_API_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        proxies=proxies,
        timeout=15,
        impersonate="chrome110",
    )
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return None
    return None


def mailtm_get_email_and_token():
    """Mail.tm 创建邮箱入口"""
    email, token = mailtm_create_email()
    if not email or not token:
        raise Exception("Mail.tm 创建邮箱失败")
    print(f"[*] 已创建 Mail.tm 邮箱: {email}")
    return email, token


def mailtm_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    """Mail.tm 轮询验证码"""
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = mailtm_get_inbox(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Mail.tm 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = str(msg.get("id", ""))
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            sender = str(msg.get("from", {}).get("address", "")).lower()
            subject = str(msg.get("subject", ""))
            combined = f"{sender}\n{subject}"
            if "openai" not in combined.lower():
                continue
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Mail.tm 从邮件中提取到验证码: {code}")
                return code
            try:
                detail = mailtm_get_message_detail(msg_id, dev_token)
                if detail:
                    intro = str(detail.get("intro", ""))
                    text = str(detail.get("text", ""))
                    detail_content = "\n".join([subject, intro, text])
                    code = extract_verification_code(detail_content, subject)
                    if code:
                        if log_callback:
                            log_callback(f"[*] Mail.tm 从邮件详情中提取到验证码: {code}")
                        return code
            except Exception:
                pass
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Mail.tm 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── 公共邮箱工具 ────────────────────────

def get_email_provider():
    return config.get("email_provider", "duckmail")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider in {"cloudmail", "openai_cpa"}:
        # Catch-all 模式：直接生成随机邮箱，无需调用邮箱创建接口
        # Cloudflare Email Routing 会自动将所有该域名的邮件路由到 Worker
        if provider == "openai_cpa" and not (
            get_openai_cpa_webhook_secret() or openai_cpa_cloudmail_fallback_available()
        ):
            raise Exception("OpenAI-CPA 需要配置 Webhook 通信密钥，或开启并完整配置 CloudMail 本地回退")
        # 支持英文逗号、中文逗号、空格分隔
        raw = str(config.get("defaultDomains", "") or "")
        domains = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        if not domains:
            raise Exception(f"{provider} 需要在 defaultDomains 中配置可用域名")
        global _cf_domain_index
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
        username = generate_username(10)
        address = f"{username}@{domain}"
        return address, f"{provider}_catch_all"
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    if provider == "generator_email":
        return generator_email_get_email_and_token()
    if provider == "inboxes":
        return inboxes_com_get_email_and_token()
    if provider == "tempmail":
        return tempmail_lol_get_email_and_token()
    if provider == "tempmail_org":
        return tempmail_org_get_email_and_token()
    if provider == "freemail":
        return freemail_get_email_and_token()
    if provider == "catchmail":
        return catchmail_get_email_and_token()
    if provider == "guerrilla":
        return guerrilla_get_email_and_token()
    if provider == "mailtm":
        return mailtm_get_email_and_token()
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "openai_cpa":
        return openai_cpa_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "generator_email":
        return generator_email_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "inboxes":
        return inboxes_com_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "tempmail":
        return tempmail_lol_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "tempmail_org":
        return tempmail_org_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "freemail":
        return freemail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "catchmail":
        return catchmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "guerrilla":
        return guerrilla_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    if provider == "mailtm":
        return mailtm_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"([A-Z0-9]{3}-[A-Z0-9]{3})", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"(?<![\d#])(\d{6})(?!\d)", f"{subject}\n{text}")
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {res.text[:200]}"
            )
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 寮傚父: {e}")
        return False


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 寮傚父: {e}")
        return False


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 寮傚父: {e}")
        return False


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": f"sso={token}; sso-rw={token}; cf_clearance={cf_clearance}",
                }
            )
            if not set_tos_accepted(session, log_callback):
                return False, "set_tos_accepted 失败!"
            if not set_birth_date(session, log_callback):
                return False, "set_birth_date 失败!"
            if not update_nsfw_settings(session, log_callback):
                return False, "update_nsfw_settings 失败!"
            return True, "鎴愬姛寮€鍚疦SFW"
    except Exception as e:
        return False, f"寮傚父: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_thread_ctx = threading.local()

from tab_pool import TabPool


def _get_browser():
    return TabPool.get_browser()


def _set_browser(value):
    pass  # TabPool 管理 browser，外部 setter 为 no-op


def _get_page():
    if TabPool.get_browser() is None:
        return None
    return TabPool.get_tab()


def _set_page(value):
    pass  # TabPool 管理 tab，外部 setter 为 no-op


def start_browser(log_callback=None):
    last_exc = None
    for attempt in range(1, 5):
        try:
            TabPool.init(create_browser_options, log_callback=log_callback)
            page = TabPool.get_tab()
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return TabPool.get_browser(), page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            # 每线程独立浏览器，shutdown 只影响当前线程
            try:
                TabPool.release_tab()
            except Exception:
                pass
            human_sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    """Quit current-thread Chromium (full process exit + del_data)."""
    TabPool.release_tab()


def prepare_browser_for_next_account(log_callback=None, force_recycle: bool = False):
    """Between accounts: clear session (reuse) or full recycle.

    Returns (browser, page).
    """
    reuse = bool(PERF_FLAGS.get("browser_reuse", True)) and not force_recycle
    every = int(PERF_FLAGS.get("browser_recycle_every", 25) or 25)
    served = TabPool.served_count()
    if reuse and TabPool.get_browser() is not None and (every <= 0 or served < every):
        if TabPool.clear_session(log_callback=log_callback):
            TabPool.mark_served()
            return TabPool.get_browser(), _get_page()
    # full recycle
    if log_callback:
        log_callback(f"[*] 浏览器完整回收（reuse={reuse}, served={served}, every={every}）")
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def shutdown_browser():
    """Quit all tracked Chromium instances."""
    TabPool.shutdown()


def restart_browser(log_callback=None):
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def refresh_active_page():
    if TabPool.get_browser() is None:
        restart_browser()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            page = browser.get_tab(tabs[-1])
        else:
            page = browser.new_tab()
        page.refresh()
        TabPool.sync_tab()
    except Exception:
        restart_browser()
    return _get_page()


def click_email_signup_button(timeout=None, log_callback=None, cancel_callback=None):
    page = _get_page()
    if timeout is None:
        timeout = int(config.get("nav_email_button_timeout", 12))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        lower.includes('email')
    );
});
if (!target) {
    return false;
}
target.click();
return true;
        """)

        if clicked:
            if log_callback:
                log_callback("[*] 已点击「使用邮箱注册」按钮")
            human_sleep(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        human_sleep(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    take_error_screenshot(page, "no_email_signup_btn")
    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            TabPool.release_tab()
            page = _get_page()
            page.get(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            page = _get_page()
            page.get(SIGNUP_URL)
    page.wait.doc_loaded()
    dump_state(page, "signup-loaded")
    take_screenshot(page, "signup")
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )
    dump_state(page, "after-email-signup-click")


def has_profile_form(log_callback=None):
    page = refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=None, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    check_timeout(time.time())
    if timeout is None:
        timeout = int(config.get("email_form_timeout", 20))
    email, dev_token = get_email_and_token()
    if not email:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'not-ready';
input.focus(); input.click();
// 清空并设置值
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
// 完整事件序列，确保 React 受控组件同步
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
// 验证：值已写入即可（不依赖 checkValidity，部分站点自定义校验会导致误判）
const current = (input.value || '').trim();
if (current === email) return 'filled';
// 兜底：尝试逐字符输入
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
if ((input.value || '').trim() === email) return 'filled';
return input.value;
            """,
            email,
        )
        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if filled != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue
        human_sleep(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !input.checkValidity() || !(input.value || '').trim()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        lower.includes('sign up') ||
        lower.includes('signup') ||
        lower.includes('continue') ||
        lower.includes('next')
    );
});
if (!submitButton || submitButton.disabled) return false;
submitButton.click();
return true;
            """
        )
        if clicked:
            if log_callback:
                log_callback(f"[*] 已填写邮箱并点击注册: {email}")
            dump_state(page, "email-submitted")
            take_screenshot(page, "email-submitted")
            return email, dev_token
        human_sleep(0.5, cancel_callback)
    take_error_screenshot(page, "no_email_input")
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    dump_state(page, "wait-code")
    take_screenshot(page, "wait-code")
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            human_sleep(1.5, cancel_callback)
            return code

        human_sleep(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    page = _get_page()
    if page is None:
        take_error_screenshot(page, "no_page_turnstile")
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        human_sleep(1, cancel_callback)

    take_error_screenshot(page, "turnstile_failed")
    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    dump_state(page, "profile-form")
    take_screenshot(page, "profile-form")
    given_name, family_name, password = build_profile()
    # 预热 Turnstile：等 2 秒让 iframe 初始化，插件会自动点击 checkbox
    if log_callback:
        log_callback("[*] 预热 Turnstile...")
    human_sleep(2, cancel_callback)
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount') || t.includes('completesignup');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                human_sleep(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                human_sleep(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount') || t.includes('completesignup');
});
if (!submitBtn) return 'no-submit-button';
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            human_sleep(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if submit_state == "no-submit-button" and log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")

        human_sleep(0.5, cancel_callback)

    take_error_screenshot(page, "profile_failed")
    raise Exception("最终注册页资料填写失败")


# ── NSFW 自动开启（从 AaronL725 移植） ──


def generate_random_birthdate():
    """生成随机生日（20-40 岁）。"""
    import datetime as dt
    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    """设置生日。"""
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_birth_date status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_birth_date HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    """同意 TOS。"""
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_tos_accepted HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    """编码 NSFW 设置 gRPC 请求体。"""
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    """更新 NSFW 设置。"""
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"update_nsfw_settings HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw(sso_cookie, log_callback=None):
    """使用 sso cookie 自动开启 NSFW（生日 + TOS + NSFW 设置）。"""
    from curl_cffi import requests as cf_requests
    session = cf_requests.Session()
    session.cookies.set("sso", sso_cookie, domain="grok.com")
    session.cookies.set("sso", sso_cookie, domain="accounts.x.ai")

    results = {}

    # 1. 设置生日
    ok, msg = set_birth_date(session, log_callback=log_callback)
    results["set_birth_date"] = {"ok": ok, "msg": msg}

    # 2. 同意 TOS
    ok, msg = set_tos_accepted(session, log_callback=log_callback)
    results["set_tos_accepted"] = {"ok": ok, "msg": msg}

    # 3. 开启 NSFW
    ok, msg = update_nsfw_settings(session, log_callback=log_callback)
    results["update_nsfw_settings"] = {"ok": ok, "msg": msg}

    if log_callback:
        all_ok = all(r["ok"] for r in results.values())
        log_callback(f"[*] NSFW 设置: {'全部成功' if all_ok else results}")

    return results


# ── wait_for_sso_cookie ──


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            page = _get_page()
            if page is None:
                human_sleep(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    return t.includes('完成注册');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount') || t.includes('completesignup');
});
if (!submitBtn) return 'final-page-no-submit';
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and retried in ("final-page-no-submit", "final-page-clicked-submit"):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        human_sleep(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


# ── 登录（非注册）获取 sso ──

LOGIN_URL = "https://accounts.x.ai/login?redirect=grok-com"


def open_login_page(log_callback=None, cancel_callback=None):
    """打开 xAI 登录页，点击「使用邮箱登录」。"""
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(LOGIN_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        restart_browser()
        page = _get_page()
        page.get(LOGIN_URL)
    page.wait.doc_loaded()
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    # 点击「使用邮箱登录」
    clicked = page.run_js("""
const btn = document.querySelector('button[data-testid="continue-with-email"]');
if (btn) { btn.click(); return 'clicked'; }
return 'not-found';
""")
    if clicked != 'clicked':
        take_error_screenshot(page, "no_email_login_btn")
        raise Exception("未找到「使用邮箱登录」按钮")
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback("[*] 已点击「使用邮箱登录」")


def fill_login_and_submit(email, password, timeout=120, log_callback=None, cancel_callback=None):
    """两步登录：1.填邮箱点下一步 2.填密码处理Turnstile点登录。"""
    page = _get_page()
    deadline = time.time() + timeout
    last_cf_retry = 0.0

    # ── 步骤1：填邮箱，点「下一步」 ──
    email_submitted = False
    while time.time() < deadline and not email_submitted:
        raise_if_cancelled(cancel_callback)
        state = page.run_js("""
const emailInput = document.querySelector('input[data-testid="email"]');
if (!emailInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = emailInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(emailInput, arguments[0]); else emailInput.value = arguments[0];
emailInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
emailInput.dispatchEvent(new Event('change', {bubbles:true}));
emailInput.blur();
if (String(emailInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-btn';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""", email)
        if state == 'submitted':
            email_submitted = True
            if log_callback:
                log_callback(f"[*] 已填写邮箱并提交: {email}")
        elif state == 'not-ready':
            human_sleep(0.5, cancel_callback)
        elif state == 'btn-disabled':
            human_sleep(0.5, cancel_callback)
        else:
            human_sleep(0.5, cancel_callback)
    if not email_submitted:
        raise Exception("邮箱提交超时")

    # 等密码框出现
    human_sleep(2, cancel_callback)

    # ── 步骤2：填密码，处理 Turnstile，点「登录」 ──
    pw_filled = False
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not pw_filled:
            filled = page.run_js("""
const pwInput = document.querySelector('input[data-testid="password"]');
if (!pwInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = pwInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(pwInput, arguments[0]); else pwInput.value = arguments[0];
pwInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
pwInput.dispatchEvent(new Event('change', {bubbles:true}));
pwInput.blur();
if (String(pwInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
return 'ready';
""", password)
            if isinstance(filled, str) and filled.startswith('wait-cf'):
                pw_filled = True
                if log_callback:
                    token_len = filled.split(':',1)[1] if ':' in filled else '0'
                    log_callback(f"[*] 已填密码，等待 Turnstile... token长度={token_len}")
                now = time.time()
                if now - last_cf_retry >= 8:
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                            if log_callback:
                                log_callback("[*] Turnstile 已通过，回填完成")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                    last_cf_retry = now
                human_sleep(1, cancel_callback)
                continue
            elif filled == 'ready':
                pw_filled = True
                if log_callback:
                    log_callback("[*] 密码已填写，准备提交")
            elif filled == 'not-ready':
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == 'fill-failed':
                human_sleep(0.5, cancel_callback)
                continue

        # 提交
        state = page.run_js("""
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-submit';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""")
        if isinstance(state, str) and state.startswith('wait-cf'):
            if log_callback:
                token_len = state.split(':',1)[1] if ':' in state else '0'
                log_callback(f"[*] 等待 Turnstile 通过后再提交... token长度={token_len}")
            now = time.time()
            if now - last_cf_retry >= 8:
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                        if log_callback:
                            log_callback("[*] Turnstile 二次复用完成")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                last_cf_retry = now
            human_sleep(1, cancel_callback)
            continue
        elif state == 'submitted':
            if log_callback:
                log_callback("[*] 已点击登录，等待 sso cookie...")
            return
        elif state == 'btn-disabled':
            human_sleep(1, cancel_callback)
            continue
        human_sleep(1, cancel_callback)
    raise Exception("登录提交超时")

def login_and_get_sso(email, password, log_callback=None, cancel_callback=None):
    """完整登录流程：打开页 → 填邮箱密码 → Turnstile → 等 sso cookie。"""
    open_login_page(log_callback=log_callback, cancel_callback=cancel_callback)
    fill_login_and_submit(email, password, log_callback=log_callback, cancel_callback=cancel_callback)
    sso = wait_for_sso_cookie(timeout=120, log_callback=log_callback, cancel_callback=cancel_callback)
    return sso


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("980x860")
        self.root.minsize(900, 760)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.stats_lock = threading.Lock()
        self._tutorial_window = None
        self.setup_ui()
        self.root.after(200, self._maybe_show_tutorial_on_start)

    def setup_ui(self):
        load_config()
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_frame = ttk.LabelFrame(main_frame, text="配置", padding=10)
        config_frame.pack(fill=tk.X, pady=5)
        ttk.Label(config_frame, text="邮箱服务商:").grid(row=0, column=0, sticky=tk.W)
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = ttk.Combobox(config_frame, textvariable=self.email_provider_var, values=["duckmail", "yyds", "cloudflare", "cloudmail"], width=12, state="readonly")
        self.email_provider_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="注册数量:").grid(row=0, column=2, sticky=tk.W, padx=10)
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = ttk.Spinbox(config_frame, from_=1, to=100, width=8, textvariable=self.count_var)
        self.count_spinbox.grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="并发线程:").grid(row=1, column=2, sticky=tk.W, padx=10)
        self.thread_var = tk.StringVar(value=str(config.get("register_threads", 1)))
        self.thread_spinbox = ttk.Spinbox(config_frame, from_=1, to=10, width=8, textvariable=self.thread_var)
        self.thread_spinbox.grid(row=1, column=3, sticky=tk.W, padx=5)
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = ttk.Checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        self.nsfw_check.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(config_frame, text="代理（可选）:").grid(row=2, column=0, sticky=tk.W)
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = ttk.Entry(config_frame, textvariable=self.proxy_var, width=30)
        self.proxy_entry.grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="DuckMail API Key:").grid(row=3, column=0, sticky=tk.W)
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=30)
        self.api_key_entry.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Base:").grid(row=4, column=0, sticky=tk.W)
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_base_var, width=30)
        self.cloudflare_api_base_entry.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Key:").grid(row=5, column=0, sticky=tk.W)
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_key_var, width=30)
        self.cloudflare_api_key_entry.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare 鉴权模式:").grid(row=6, column=0, sticky=tk.W)
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "bearer"))
        self.cloudflare_auth_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cloudflare_auth_mode_var,
            values=["query-key", "bearer", "x-api-key", "none"],
            width=12,
            state="readonly",
        )
        self.cloudflare_auth_mode_combo.grid(row=6, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CF 路径(domains/accounts/token/messages):").grid(row=7, column=0, sticky=tk.W)
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/domains"),
                    config.get("cloudflare_path_accounts", "/accounts"),
                    config.get("cloudflare_path_token", "/token"),
                    config.get("cloudflare_path_messages", "/messages"),
                ]
            )
        )
        self.cloudflare_paths_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_paths_var, width=30)
        self.cloudflare_paths_entry.grid(row=7, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail URL:").grid(row=8, column=0, sticky=tk.W)
        self.cloudmail_url_var = tk.StringVar(value=str(config.get("cloudmail_url", "")))
        self.cloudmail_url_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_url_var, width=30)
        self.cloudmail_url_entry.grid(row=8, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员邮箱:").grid(row=9, column=0, sticky=tk.W)
        self.cloudmail_admin_email_var = tk.StringVar(value=str(config.get("cloudmail_admin_email", "")))
        self.cloudmail_admin_email_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_admin_email_var, width=30)
        self.cloudmail_admin_email_entry.grid(row=9, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员密码:").grid(row=10, column=0, sticky=tk.W)
        self.cloudmail_password_var = tk.StringVar(value=str(config.get("cloudmail_password", "")))
        self.cloudmail_password_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_password_var, width=30, show="*")
        self.cloudmail_password_entry.grid(row=10, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 导入启用:").grid(row=11, column=0, sticky=tk.W)
        self.grok2api_import_enabled_var = tk.BooleanVar(value=bool(config.get("grok2api_import_enabled", True)))
        self.grok2api_import_enabled_check = ttk.Checkbutton(config_frame, variable=self.grok2api_import_enabled_var)
        self.grok2api_import_enabled_check.grid(row=11, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 根地址:").grid(row=12, column=0, sticky=tk.W)
        self.grok2api_import_base_var = tk.StringVar(value=str(config.get("grok2api_import_base", "http://127.0.0.1:8000")))
        self.grok2api_import_base_entry = ttk.Entry(config_frame, textvariable=self.grok2api_import_base_var, width=30)
        self.grok2api_import_base_entry.grid(row=12, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api Management Key:").grid(row=13, column=0, sticky=tk.W)
        self.grok2api_import_mgmt_key_var = tk.StringVar(value=str(config.get("grok2api_import_management_key", "")))
        self.grok2api_import_mgmt_key_entry = ttk.Entry(config_frame, textvariable=self.grok2api_import_mgmt_key_var, width=30)
        self.grok2api_import_mgmt_key_entry.grid(row=13, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 导入重试次数:").grid(row=14, column=0, sticky=tk.W)
        self.grok2api_import_retries_var = tk.StringVar(value=str(config.get("grok2api_import_retries", 3)))
        self.grok2api_import_retries_entry = ttk.Entry(config_frame, textvariable=self.grok2api_import_retries_var, width=8)
        self.grok2api_import_retries_entry.grid(row=14, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="导入重试间隔(秒):").grid(row=15, column=0, sticky=tk.W)
        self.grok2api_import_retry_delay_var = tk.StringVar(value=str(config.get("grok2api_import_retry_delay", 2)))
        self.grok2api_import_retry_delay_entry = ttk.Entry(config_frame, textvariable=self.grok2api_import_retry_delay_var, width=8)
        self.grok2api_import_retry_delay_entry.grid(row=15, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="默认域名(defaultDomains):").grid(row=17, column=0, sticky=tk.W)
        self.default_domains_var = tk.StringVar(value=str(config.get("defaultDomains", "")))
        self.default_domains_entry = ttk.Entry(config_frame, textvariable=self.default_domains_var, width=30)
        self.default_domains_entry.grid(row=17, column=1, columnspan=3, sticky=tk.W, padx=5)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        self.help_btn = ttk.Button(btn_frame, text="教程", command=self.show_tutorial)
        self.help_btn.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=60)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        # 仅当用户当前就在底部时自动跟随，避免手动上滑后被强制拉回底部
        yview = self.log_text.yview()
        at_bottom = bool(yview) and yview[1] >= 0.999
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        if at_bottom:
            self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def _maybe_show_tutorial_on_start(self):
        if bool(config.get("show_tutorial_on_start", True)):
            self.show_tutorial()

    def _tutorial_text(self):
        return """欢迎使用 Grok 注册机。建议按下面顺序填写（从最关键到可选）：

【第一步：先确定邮箱后端信息从哪里来】
如果你使用 cloudflare 模式（你当前主要是这套），先去你的临时邮箱服务配置接口查信息：
- 常见接口: /open_api/settings、/api/settings、/health_check
- 重点字段:
  - api_base（对应本工具的 Cloudflare API Base）
  - domains / defaultDomains（可用域名）
  - needAuth（是否需要鉴权）
  - admin_password 或 api_key（需要鉴权时使用）
  - provider.type（应为 cloudflare_temp_email）

【第二步：先填最小可运行配置】
1) 邮箱服务商
- duckmail: 需要 DuckMail API Key
- yyds: 需要 YYDS API Key 或 JWT
- cloudflare: 需要 Cloudflare API Base（cloudflare_temp_email 临时邮箱）
- cloudmail: 需要 CloudMail URL + 密码 + defaultDomains（maillab/cloud-mail 完整邮箱）

2) Cloudflare API Base（cloudflare 模式必填）
- 示例: https://xxxx.pages.dev
- 填写规则: 与 settings 接口中的 api_base 保持一致

3) 默认域名(defaultDomains)
- 填写你要优先使用的域名
- 支持单域名或逗号分隔多域名轮换
- 示例: a.com,b.com

4) CF 路径(domains/accounts/token/messages)
- 必须与后端真实路由一致
- 常见新路径:
  - /api/domains,/api/new_address,/api/token,/api/mails
- 常见旧路径:
  - /domains,/accounts,/token,/messages

5) Cloudflare API Key / 鉴权模式
- needAuth=false: 通常鉴权模式选 none，key 可留空
- needAuth=true: 按后端要求填 key，并选择 bearer/x-api-key/query-key

6) CloudMail 模式配置（maillab/cloud-mail 部署）
- CloudMail URL: 你的 Worker 地址，如 https://mail.xxx.workers.dev
- CloudMail 管理员邮箱: 管理员账号，如 admin@yourdomain.com
- CloudMail 管理员密码: 管理员密码（用于获取公开 API token 查询邮件）
- defaultDomains: 必须填写可用域名，如 yourdomain.com
- 前提: CloudMail 管理面板需关闭注册验证码（Turnstile），或确保注册接口可用
- 邮件获取: 通过 /api/public/emailList 公开接口查询，自动刷新 token

【第三步：并发与稳定性】
6) 注册数量
- 本次要注册的总账号数

7) 并发线程
- 建议先 3-6 稳定后再升到 10

8) 代理（可选）
- 不填=直连
- 示例: http://127.0.0.1:7890
- 代理不稳会影响验证码和注册稳定性

9) 注册后开启 NSFW
- 勾选后成功账号会自动调用接口开启对应设置

【第四步：grok2api 入池（可选）】
10) grok2api 本地自动入池
- 开启后把成功 sso 自动写入本地池
- 本地 token.json 填 grok2api 的 token.json 路径

11) grok2api 池名
- ssoBasic 或 ssoSuper

12) grok2api 远端自动入池
- 开启后调用远端管理接口自动加 token
- 远端 Base 示例: https://xxx/admin/api
- app_key 按远端服务配置填写

【最后：快速自检】
1) 先设置: 注册数量=1，并发线程=1
2) 点开始后看日志是否出现：
- 已创建邮箱: xxx@你的域名
- Cloudflare/CloudMail 本轮邮件数量: ...
- 从邮件中提取到验证码: ...
3) 若第一步就失败：
- cloudflare 模式: 检查 API Base / CF 路径 / 鉴权模式
- cloudmail 模式: 检查 URL / 密码 / defaultDomains / 注册接口是否可用

提示:
- 点“开始注册”会自动保存当前配置到 config.json。
- 如果关闭了启动教程，可随时点主界面的“教程”按钮重新打开。"""

    def show_tutorial(self):
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._tutorial_window = win
        win.title("使用教程")
        win.geometry("760x620")
        win.minsize(680, 520)
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=26)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", self._tutorial_text())
        txt.config(state=tk.DISABLED)

        footer = ttk.Frame(frame)
        footer.pack(fill=tk.X, pady=(8, 0))

        dont_show_var = tk.BooleanVar(value=not bool(config.get("show_tutorial_on_start", True)))
        chk = ttk.Checkbutton(
            footer,
            text="以后不再自动显示本教程",
            variable=dont_show_var,
        )
        chk.pack(side=tk.LEFT)

        def on_close():
            config["show_tutorial_on_start"] = not bool(dont_show_var.get())
            save_config()
            try:
                win.destroy()
            except Exception:
                pass

        close_btn = ttk.Button(footer, text="关闭", command=on_close)
        close_btn.pack(side=tk.RIGHT, padx=5)
        win.protocol("WM_DELETE_WINDOW", on_close)

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "duckmail"
        config["proxy"] = self.proxy_var.get().strip()
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "bearer"
        config["cloudmail_url"] = self.cloudmail_url_var.get().strip()
        config["cloudmail_admin_email"] = self.cloudmail_admin_email_var.get().strip()
        config["cloudmail_password"] = self.cloudmail_password_var.get().strip()
        config["grok2api_import_enabled"] = bool(self.grok2api_import_enabled_var.get())
        config["grok2api_import_base"] = self.grok2api_import_base_var.get().strip()
        config["grok2api_import_management_key"] = self.grok2api_import_mgmt_key_var.get().strip()
        config["grok2api_import_retries"] = int(self.grok2api_import_retries_var.get().strip() or "3")
        config["grok2api_import_retry_delay"] = int(self.grok2api_import_retry_delay_var.get().strip() or "2")
        config["defaultDomains"] = self.default_domains_var.get().strip()
        try:
            config["register_threads"] = max(1, min(10, int(self.thread_var.get())))
        except Exception:
            config["register_threads"] = 1
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        if config["email_provider"] == "cloudmail":
            if not config.get("cloudmail_url"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail URL")
                return
            if not config.get("cloudmail_admin_email"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员邮箱")
                return
            if not config.get("cloudmail_password"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员密码")
                return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        worker_count = max(1, min(config.get("register_threads", 1), count))
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count, worker_count),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 3
        start_interval_screenshot(idx, browser=TabPool.get_browser())
        try:
            for mail_try in range(1, max_mail_retry + 1):
                logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
                logf("[*] 2. 创建邮箱并提交")
                email, dev_token = fill_email_and_submit(log_callback=logf, cancel_callback=self.should_stop)
                logf(f"[*] 邮箱: {email}")
                try:
                    with open(os.path.join(os.path.dirname(__file__), "mail_credentials.txt"), "a", encoding="utf-8") as f:
                        f.write(f"{email}\t{dev_token}\n")
                except Exception:
                    pass
                logf("[*] 3. 拉取验证码")
                try:
                    code = fill_code_and_submit(email, dev_token, log_callback=logf, cancel_callback=self.should_stop)
                    mail_ok = True
                    break
                except Exception as mail_exc:
                    msg = str(mail_exc)
                    if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                        logf(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                        restart_browser(log_callback=logf)
                        sleep_with_cancel(1, self.should_stop)
                        continue
                    raise
            if not mail_ok:
                raise Exception("验证码阶段失败，已达到最大重试次数")
            logf(f"[*] 验证码: {code}")
            logf("[*] 4. 填写资料")
            profile = fill_profile_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
            logf("[*] 5. 等待 sso cookie")
            sso = wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
            with self.stats_lock:
                self.results.append({"email": email, "sso": sso, "profile": profile})
                self.success_count += 1
                line = f"{email}----{profile.get('password','')}----{sso}\n"
                try:
                    with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    logf(f"[Debug] 保存账号文件失败: {file_exc}")
            add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
            logf(f"[+] 注册成功: {email}")
        finally:
            stop_interval_screenshot()

    def _worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"
        logf = lambda m: self.log(f"{prefix} {m}")
        try:
            start_browser(log_callback=logf)
            logf("[*] 浏览器已启动")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            stop_browser()

    def run_registration(self, count, worker_count):
        task_queue = queue.Queue()
        for i in range(1, count + 1):
            task_queue.put(i)
        workers = []
        try:
            start_interval = float(config.get("thread_start_interval", 0.8))
        except Exception:
            start_interval = 0.8
        if start_interval < 0:
            start_interval = 0.0
        for wid in range(1, worker_count + 1):
            t = threading.Thread(target=self._worker_loop, args=(wid, count, task_queue), daemon=True)
            workers.append(t)
            t.start()
            if wid < worker_count and start_interval > 0:
                sleep_with_cancel(start_interval, self.should_stop)
        for t in workers:
            t.join()
        self._set_running_ui(False)
        self.log("[*] 任务结束")

def main():
    root = tk.Tk()
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

