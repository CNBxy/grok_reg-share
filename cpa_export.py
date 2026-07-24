"""Register-machine hook: mint CPA xai auth after successful registration.

OIDC package lives at ./cpa_xai (bundled with this project).
Optional override: config `api_reverse_tools` / env `API_REVERSE_TOOLS`
points at a directory that *contains* the `cpa_xai` package.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

_REG_DIR = Path(__file__).resolve().parent
_DEFAULT_OUT = _REG_DIR / "cpa_auths"
_DEFAULT_CPA = Path("")  # empty = do not assume a machine-local CPA path


def _ensure_cpa_xai_on_path(tools_dir: str | Path | None = None) -> Path:
    """Put the parent of `cpa_xai` on sys.path. Default: this project root."""
    if tools_dir:
        tools = Path(tools_dir).expanduser().resolve()
    else:
        env = (os.environ.get("API_REVERSE_TOOLS") or "").strip()
        tools = Path(env).expanduser().resolve() if env else _REG_DIR
    # If user pointed at .../cpa_xai itself, use its parent
    if tools.name == "cpa_xai" and (tools / "__init__.py").is_file():
        tools = tools.parent
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return tools


def export_cookies_from_page(page: Any) -> list[dict]:
    """Best-effort export of cookies from a DrissionPage tab/browser."""
    if page is None:
        return []
    cookies = None
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if cookies:
                break
        except TypeError:
            continue
        except Exception:
            continue
    if not cookies:
        try:
            browser = getattr(page, "browser", None)
            if browser is not None:
                cookies = browser.cookies()
        except Exception:
            cookies = None
    if isinstance(cookies, list):
        return [c for c in cookies if isinstance(c, dict)]
    return []


def export_cpa_xai_for_account(
    email: str,
    password: str = "",
    *,
    page: Any | None = None,
    cookies: Any | None = None,
    sso: str | None = None,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Mint OIDC + write xai-<email>.json using pure-HTTP SSO->Build flow (no browser)."""
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not cfg.get("cpa_export_enabled", True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    tools_dir = cfg.get("api_reverse_tools") or cfg.get("cpa_xai_parent") or None
    _ensure_cpa_xai_on_path(tools_dir)

    try:
        from cpa_xai import mint_and_export  # type: ignore
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] import cpa_xai failed: {e}")
        return {"ok": False, "error": f"import: {e}"}

    out_dir = Path(cfg.get("cpa_auth_dir") or _DEFAULT_OUT).expanduser()
    if not out_dir.is_absolute():
        out_dir = (_REG_DIR / out_dir).resolve()

    hotload_raw = (cfg.get("cpa_hotload_dir") or "").strip()
    cpa_dir = Path(hotload_raw).expanduser() if hotload_raw else None
    if cpa_dir and not cpa_dir.is_absolute():
        cpa_dir = (_REG_DIR / cpa_dir).resolve()

    # Priority: cpa_proxy > proxy > env.
    proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
    if not proxy:
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()
    probe = bool(cfg.get("cpa_probe_after_write", True))
    probe_chat = bool(cfg.get("cpa_probe_chat", False))
    base_url = cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"

    sso_val = (sso or "").strip()
    if not sso_val:
        log("[cpa] no SSO token provided, cannot mint")
        return {"ok": False, "error": "missing SSO token"}

    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"[cpa] SSO->Build OIDC for {email} -> {out_dir} proxy={proxy or '(none)'}")

    def _log(msg: str) -> None:
        log(f"[cpa] {msg}")

    result = mint_and_export(
        email=email,
        sso_token=sso_val,
        auth_dir=out_dir,
        proxy=proxy or None,
        base_url=base_url,
        probe=probe,
        probe_chat=probe_chat,
        log=_log,
    )

    if result.get("ok") and result.get("path") and cfg.get("cpa_copy_to_hotload", False) and cpa_dir:
        try:
            cpa_dir.mkdir(parents=True, exist_ok=True)
            src = Path(result["path"])
            dst = cpa_dir / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            result["cpa_path"] = str(dst)
            log(f"[cpa] hotload copy -> {dst}")
        except Exception as e:  # noqa: BLE001
            log(f"[cpa] hotload copy failed: {e}")
            result["cpa_copy_error"] = str(e)

    # 成功后推送远程 CPA 仓管
    if result.get("ok") and result.get("path"):
        push_cpa_to_remote(result["path"], cfg, log)          # CLIProxyAPI（旧，cpa_remote_push_*）
        push_cpa_to_grok2api_build(result["path"], cfg, log)  # grok2api Build 导入（新，grok2api_import_*）

    # failure log under register dir
    if not result.get("ok"):
        fail_path = out_dir / "cpa_auth_failed.txt"
        with open(fail_path, "a", encoding="utf-8") as f:
            f.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        if cfg.get("cpa_mint_required", False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error')}")

    return result


def push_cpa_to_grok2api_build(auth_file_path: str, cfg: dict, log: Callable[[str], None] | None = None) -> bool:
    """将 CPA xai-*.json 推送到 grok2api Grok Build（从画面中导入账号 流程）。

    配置项：
        grok2api_import_enabled       : 是否开启 grok2api 导入推送
        grok2api_import_base          : grok2api 根 URL，如 http://127.0.0.1:8000
        grok2api_import_management_key: Management Key（Bearer 认证）
        grok2api_import_retries       : 重试次数（默认3）
        grok2api_import_retry_delay   : 重试间隔秒数（默认2）

    推送方式：
        POST {base}/api/admin/v1/accounts/import/body
        Authorization: Bearer <management_key>
        Content-Type: application/json
        Body = Grok Build 导入格式 JSON
    """
    log = log or (lambda m: print(m, flush=True))

    if not cfg.get("grok2api_import_enabled", False):
        return False

    base_url = str(cfg.get("grok2api_import_base", "") or "").strip().rstrip("/")
    if not base_url:
        log("[cpa-push] grok2api_import_base 未配置，跳过")
        return False

    mgmt_key = str(cfg.get("grok2api_import_management_key", "") or "").strip()

    try:
        auth_path = Path(auth_file_path)
        if not auth_path.exists():
            log(f"[cpa-push] 文件不存在: {auth_file_path}")
            return False
        with open(auth_path, "r", encoding="utf-8") as f:
            cpa_data = json.load(f)
    except Exception as e:
        log(f"[cpa-push] 读取文件失败: {e}")
        return False

    # 将 CPA xai 格式转换为 Grok Build 导入格式
    access_token = (cpa_data.get("access_token") or "").strip()
    refresh_token = (cpa_data.get("refresh_token") or "").strip()
    if not access_token:
        log("[cpa-push] CPA 文件中无 access_token，跳过")
        return False

    email = (cpa_data.get("email") or "").strip()
    user_id = (cpa_data.get("sub") or "").strip()
    id_token = (cpa_data.get("id_token") or "").strip()
    # expired 格式为 YYYY-MM-DDTHH:MM:SSZ，转为 RFC3339
    expired_raw = (cpa_data.get("expired") or "").strip()
    expires_at = ""
    if expired_raw:
        try:
            from datetime import datetime
            dt = datetime.strptime(expired_raw.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            expires_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            expires_at = expired_raw

    name = email or f"Grok Build {user_id[:8] if user_id else ''}"

    # Build 导入 JSON 格式
    build_entry = {
        "provider": "grok_build",
        "name": name,
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "email": email,
        "user_id": user_id,
    }
    if id_token:
        build_entry["id_token"] = id_token
    if expires_at:
        build_entry["expires_at"] = expires_at

    build_payload = json.dumps({"accounts": [build_entry]}, ensure_ascii=False)

    target_url = f"{base_url}/api/admin/v1/accounts/import/body"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"

    import urllib.request
    import urllib.error

    data = build_payload.encode("utf-8")
    req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")

    retries = int(cfg.get("grok2api_import_retries", 3))
    retry_delay = float(cfg.get("grok2api_import_retry_delay", 2))

    for attempt in range(1, retries + 1):
        try:
            proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
            if proxy:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                opener = urllib.request.build_opener(proxy_handler)
            else:
                opener = urllib.request.build_opener()
            resp = opener.open(req, timeout=15)
            resp_body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
            if 200 <= status < 300:
                log(f"[cpa-push] 已推送 Build 凭证 -> {target_url} (HTTP {status})")
                return True
            else:
                log(f"[cpa-push] 推送失败 HTTP {status}: {resp_body[:200]}")
                if attempt < retries:
                    time.sleep(retry_delay)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            log(f"[cpa-push] 推送失败 HTTP {e.code}: {err_body}")
            if attempt < retries:
                time.sleep(retry_delay)
        except Exception as e:
            log(f"[cpa-push] 推送异常(尝试 {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(retry_delay)
    return False


def call_grok2api_sso_to_build(
    sso: str,
    email: str = "",
    name: str = "",
    *,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """注册成功后调用 grok2api SSO→Build 转换接口完成 Device OAuth。

    配置项：
        grok2api_import_enabled          : 总开关
        grok2api_device_oauth_enabled    : 是否启用 SSO→Build 转换
        grok2api_import_base             : grok2api 根 URL
        grok2api_import_management_key   : Management Key（Bearer 认证）
        grok2api_import_retries          : 重试次数（默认3）
        grok2api_import_retry_delay      : 重试间隔秒数（默认2）

    调用方式：
        POST {base}/api/admin/v1/accounts/device/sso-to-build
        Authorization: Bearer <management_key>
        Body: {"sso": "...", "email": "...", "name": "..."}
    """
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not cfg.get("grok2api_import_enabled", False):
        log("[sso2build] grok2api 导入未开启，跳过")
        return {"ok": False, "skipped": True, "reason": "grok2api_import_disabled"}

    if not cfg.get("grok2api_device_oauth_enabled", False):
        log("[sso2build] SSO→Build 转换未开启，跳过")
        return {"ok": False, "skipped": True, "reason": "device_oauth_disabled"}

    base_url = str(cfg.get("grok2api_import_base", "") or "").strip().rstrip("/")
    if not base_url:
        log("[sso2build] grok2api_import_base 未配置，跳过")
        return {"ok": False, "skipped": True, "reason": "no_base_url"}

    mgmt_key = str(cfg.get("grok2api_import_management_key", "") or "").strip()

    sso_val = (sso or "").strip()
    if not sso_val:
        log("[sso2build] SSO token 为空，跳过")
        return {"ok": False, "error": "empty_sso"}

    target_url = f"{base_url}/api/admin/v1/accounts/device/sso-to-build"
    payload = {"sso": sso_val}
    if email:
        payload["email"] = email
    if name:
        payload["name"] = name

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"

    import urllib.request as urllib_req
    import urllib.error as urllib_err

    req = urllib_req.Request(target_url, data=data, headers=headers, method="POST")

    retries = int(cfg.get("grok2api_import_retries", 3))
    retry_delay = float(cfg.get("grok2api_import_retry_delay", 2))

    for attempt in range(1, retries + 1):
        try:
            proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
            if proxy:
                proxy_handler = urllib_req.ProxyHandler({"http": proxy, "https": proxy})
                opener = urllib_req.build_opener(proxy_handler)
            else:
                opener = urllib_req.build_opener()
            resp = opener.open(req, timeout=120)
            resp_body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
            if 200 <= status < 300:
                result = json.loads(resp_body)
                account_info = result.get("data", {}).get("account", {})
                log(f"[sso2build] 转换成功: account_id={account_info.get('id')} email={account_info.get('email')}")
                return {"ok": True, "data": result.get("data", {})}
            else:
                log(f"[sso2build] 失败 HTTP {status}: {resp_body[:300]}")
                if attempt < retries:
                    time.sleep(retry_delay)
        except urllib_err.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            log(f"[sso2build] 失败 HTTP {e.code}: {err_body}")
            if attempt < retries:
                time.sleep(retry_delay)
        except Exception as e:
            log(f"[sso2build] 异常(尝试 {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(retry_delay)
    return {"ok": False, "error": "max_retries"}


def push_cpa_to_remote(auth_file_path: str, cfg: dict, log: Callable[[str], None] | None = None) -> bool:
    """将 CPA xai-*.json 推送到远程 CLIProxyAPI 仓管（原始逻辑）。

    配置项：
        cpa_remote_push_enabled  : 是否开启远程推送
        cpa_remote_push_url      : 远程仓管 API 根 URL，如 http://localhost:8317
        cpa_remote_push_token    : Bearer 认证 token（Management Key，可空）

    推送方式（CLIProxyAPI 原始 JSON 上传）：
        POST {url}/v0/management/auth-files?name=xai-<email>.json
        Content-Type: application/json
        Authorization: Bearer <token>
        Body = xai-*.json 文件原始内容
    """
    log = log or (lambda m: print(m, flush=True))

    if not cfg.get("cpa_remote_push_enabled", False):
        return False

    base_url = str(cfg.get("cpa_remote_push_url", "") or "").strip().rstrip("/")
    if not base_url:
        log("[cpa-push] 远程推送已开启但 URL 未配置，跳过")
        return False

    token = str(cfg.get("cpa_remote_push_token", "") or "").strip()

    try:
        auth_path = Path(auth_file_path)
        if not auth_path.exists():
            log(f"[cpa-push] 文件不存在: {auth_file_path}")
            return False
        with open(auth_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
        # 验证是合法 JSON
        json.loads(raw_content)
    except Exception as e:
        log(f"[cpa-push] 读取文件失败: {e}")
        return False

    # CLIProxyAPI: POST /v0/management/auth-files?name=<filename>
    filename = auth_path.name
    target_url = f"{base_url}/v0/management/auth-files?name={filename}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import urllib.request
    import urllib.error

    data = raw_content.encode("utf-8")
    req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")

    # 代理设置：复用 cpa_proxy / proxy
    proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
    else:
        opener = urllib.request.build_opener()

    try:
        resp = opener.open(req, timeout=15)
        body = resp.read().decode("utf-8", errors="replace")
        status = resp.getcode()
        if 200 <= status < 300:
            log(f"[cpa-push] 已推送 {filename} -> {target_url} (HTTP {status})")
            return True
        else:
            log(f"[cpa-push] 推送失败 HTTP {status}: {body[:200]}")
            return False
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log(f"[cpa-push] 推送失败 HTTP {e.code}: {body}")
        return False
    except Exception as e:
        log(f"[cpa-push] 推送异常: {e}")
        return False
