#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东南大学校园网自动登录（SEU-WLAN 校园网 / SEU-ISP 运营商宽带，Drcom 认证门户）

作用：开机 / 登录 Windows 后自动完成校园网认证，免去每次手动打开浏览器、输入一卡通号和密码、
      选择服务类型的步骤。账号和密码两个网络是同一个，写法按当前连的无线网络自动切换：
        SEU-WLAN → ",0,一卡通号"（无感知认证前缀，不带服务类型后缀）
        SEU-ISP  → "一卡通号@cmcc"（中国电信 @dx、中国联通 @unicom）

原理：
  1. 直连探测认证门户（w.seu.edu.cn / 10.80.128.2 / 10.9.10.100，明确不走系统代理）
  2. 确认是 Drcom 门户后，调用 eportal 的 PORTAL 接口 ?c=Portal&a=login 提交账号密码
     （老接口 /drcom/login 也会返回 result=1 但不放行，只作为兜底尝试）
  3. 解析 dr1003({...})，并**真去请求一次外网**判断是否真的通：门户说成功但外网不通就继续重试，
     连续两次「门户说在线但外网不通」会先注销再重新认证
  4. 网络没就绪时按间隔重试，直到成功、被明确拒绝或超过最大等待时间

配置：同目录 config.json（支持 // 注释）   日志：同目录 login.log
手动运行：seu-autologin.exe --once       只试一次
          seu-autologin.exe --init       首次配置向导
          seu-autologin.exe --pause      结束后停住等按键（给双击用）
"""

from __future__ import annotations

import base64
from contextlib import closing
import gzip
from html.parser import HTMLParser
import http.client
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
import zlib
from urllib import error as urlerror  # noqa: F401  (保留备用: 捕获 HTTPError/URLError)
from urllib import parse as urlparse
from urllib import request as urlrequest

def _app_dir() -> str:
    """程序所在目录。

    打包成 exe（PyInstaller onefile）时 __file__ 指向临时解包目录，
    而 config.json / login.log 应该跟着 exe 本身走。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 用 pythonw（GUI 子系统、没有控制台）去启动 netsh / powershell 这类控制台程序时，
# Windows 会给子进程新开一个控制台窗口；Win11 默认宿主是 Windows Terminal，
# 表现就是"终端一闪而过"。加这个标志让子进程完全无窗口。
NO_WINDOW_FLAG = 0x08000000 if os.name == "nt" else 0

DEFAULT_CONFIG = {
    "account": "",
    "password": "",
    "isp_suffix": "@cmcc",
    # 连上哪个无线网络就用哪一份配置；没匹配到的 SSID 用上面的默认值
    "profiles": [
        {"ssid": "SEU-ISP", "isp_suffix": "@cmcc", "note": "运营商宽带（中国移动）"},
        # 校园网：账号要带 ",0," 前缀、不带运营商后缀（实测）
        {"ssid": "SEU-WLAN", "account_prefix": ",0,", "isp_suffix": "", "note": "校园网（校园用户）"},
    ],
    # 登录协议：SEU 对本机所在网段(10.210.0.0/16)用的是 PORTAL 协议(eportal)，
    # 本地认证 /drcom/login 虽然也会返回 result=1，但不会在 AC 上真正放行。
    "login_mode": "auto",
    "campus_wifi_only": True,
    "portal_login_base": "https://w.seu.edu.cn:801/eportal/",
    "portal_login_method": 1,
    "js_version": "1.0",
    # 东大不同网段的门户地址不一样，探测列表按顺序试；确认不了就跳过
    "portal_urls": ["http://w.seu.edu.cn/", "http://10.80.128.2/", "http://10.9.10.100/"],
    "portal_host_header": "w.seu.edu.cn",
    "login_path": "/drcom/login",
    # 认证结果不能只信门户的 result，必须再校验一次真实外网
    "connectivity_check_url": "http://www.msftconnecttest.com/connecttest.txt",
    "connectivity_expect": "Microsoft Connect Test",
    "connectivity_grace_seconds": 3,
    "connectivity_attempts": 3,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "request_timeout_seconds": 4,
    "login_timeout_seconds": 12,
    "retry_interval_seconds": 10,
    "fast_retry_seconds": 2,
    "fast_retry_window_seconds": 120,
    "max_wait_seconds": 300,
    "log_file": "login.log",
    "log_max_kb": 512,
    "lock_file": "login.lock",
    "lock_stale_seconds": 60,
    # 探测不到门户、且本机完全没有可用网络时，主动催一次 WLAN 连接
    "wifi_nudge": True,
    "wifi_ssid": "SEU-ISP",
    "wifi_interface": "WLAN",
    "wifi_nudge_after_seconds": 8,
    "wifi_nudge_interval_seconds": 20,
}

# 门户页面特征：Drcom 门户的 HTML 里会出现 Dr.COM / Dr.COMWebLoginID / DrcomServer 等字样
PORTAL_MARKERS = ("dr.com", "drcom", "webloginid")

# 门户返回里出现这些字样，说明是明确的认证失败（账号/密码/运营商不对），再重试也没有意义
AUTH_FAIL_KEYS = (
    "passerror",
    "pass error",
    "password",
    "usererror",
    "user error",
    "usernotexist",
    "notexist",
    "error",
    "authentication fail",
    "auth fail",
    "denied",
    "认证失败",
    "密码错误",
    "账号或密码",
    "账号不存在",
    "用户不存在",
    "被拒绝",
)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            raw = handle.read()
        cfg.update(json.loads(strip_json_comments(raw)))
    except FileNotFoundError:
        pass
    except Exception as exc:  # 配置坏了也要能记日志说明原因
        cfg["_config_error"] = "%s: %s" % (exc.__class__.__name__, exc)
    return cfg


def strip_json_comments(text: str) -> str:
    """允许在 config.json 里写 // 和 /* */ 注释（JSONC 风格），字符串里的 // 不受影响。"""
    out = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def log(cfg: dict, message: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    path = os.path.join(BASE_DIR, cfg.get("log_file") or "login.log")
    try:
        limit = max(64, int(cfg.get("log_max_kb", 512))) * 1024
        if os.path.exists(path) and os.path.getsize(path) > limit:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                tail = handle.read()[-limit // 2 :]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(tail)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    # 被 pythonw.exe 拉起时 stdout 为 None，这里只是方便手动运行看结果
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass


def decode_body(raw: bytes) -> str:
    for encoding in ("gbk", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace")


def make_opener():
    # 传入空 dict 的 ProxyHandler = 明确禁用系统代理（含 Clash 等本地代理设置），直连校园网门户
    return urlrequest.build_opener(urlrequest.ProxyHandler({}))


def local_ip(target="10.80.128.2"):
    """返回本机访问门户时实际使用的源地址（不会真的发包，只是让系统选路）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.connect((target, 80))
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        sock.close()


def acquire_lock(cfg: dict):
    """同一时刻只让一个实例真正发起认证。

    开机任务和登录任务可能在同一秒被唤醒，重复提交会把日志写乱、也没必要。
    返回值：锁文件路径（已持有）/ None（已有实例在跑，本次应退出）/ ""（无法判断，不做限制）。
    """
    path = os.path.join(BASE_DIR, cfg.get("lock_file") or "login.lock")
    stale = float(cfg.get("lock_stale_seconds", 120))
    for _ in range(3):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write("pid=%d\nstart=%.0f\n" % (os.getpid(), time.time()))
            return path
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue
            if age > stale:  # 上一个实例异常退出留下的死锁，直接接管
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            return None
        except OSError:
            return ""
    return None


def touch_lock(path: str) -> None:
    if path:
        try:
            os.utime(path, None)
        except OSError:
            pass


def release_lock(path: str) -> None:
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as stream:
            mine = stream.read().startswith("pid=%d" % os.getpid())
    except OSError:
        return
    if not mine:
        return
    # 注意：Windows 上必须先关掉文件再删，边打开边删会失败
    try:
        os.remove(path)
    except OSError:
        pass


def http_get(opener, url: str, timeout: float, cfg: dict, host_header: str | None = None):
    headers = {
        "User-Agent": cfg.get("user_agent", "Mozilla/5.0"),
        "Accept": "*/*",
        "Connection": "close",
    }
    if host_header:
        headers["Host"] = host_header
    req = urlrequest.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read()


def host_header_for(url: str, cfg: dict):
    """只有用 IP 直连门户时才需要手动给 Host 头，域名请求一律不加。"""
    host = urlparse.urlsplit(url).hostname or ""
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        return cfg.get("portal_host_header") or None
    return None


def find_portal(opener, cfg: dict, timeout: float):
    """依次探测门户地址，返回 (可用基地址, 说明)。找不到时基地址为 None。"""
    notes = []
    for url in cfg.get("portal_urls") or DEFAULT_CONFIG["portal_urls"]:
        try:
            status, raw = http_get(opener, url, timeout, cfg, host_header_for(url, cfg))
        except Exception as exc:
            notes.append("%s -> %s" % (url, exc.__class__.__name__))
            continue
        text = decode_body(raw)
        if any(marker in text.lower() for marker in PORTAL_MARKERS):
            return url, "HTTP %s，已识别为 Drcom 认证门户" % status
        notes.append(
            "%s -> HTTP %s，返回内容不是 Drcom 门户（开头：%s）"
            % (url, status, re.sub(r"\s+", " ", text.strip())[:80])
        )
    return None, "；".join(notes) if notes else "无可用地址"


def build_login_url(base: str, cfg: dict, creds: dict) -> str:
    params = {
        "callback": "dr1003",
        "DDDDD": creds.get("prefix", "") + creds["account"] + creds["suffix"],
        "upass": creds["password"],
        "0MKKey": "123456",
        "R1": "0",
        "R2": "",
        "R3": "0",
        "R6": "0",
        "para": "00",
        "v6ip": "",
        "v": str(int(time.time() * 1000)),
    }
    path = cfg.get("login_path") or DEFAULT_CONFIG["login_path"]
    return urlparse.urljoin(base, path) + "?" + urlparse.urlencode(params)


def build_portal_login_url(cfg: dict, ip: str, creds: dict, base: str = "") -> str:
    """eportal 的 PORTAL 协议登录接口（?c=Portal&a=login）。"""
    base = base or cfg.get("portal_login_base") or DEFAULT_CONFIG["portal_login_base"]
    params = {
        "c": "Portal",
        "a": "login",
        "callback": "dr1003",
        "login_method": str(cfg.get("portal_login_method", 1)),
        "user_account": creds.get("prefix", "") + creds["account"] + creds["suffix"],
        "user_password": creds["password"],
        "wlan_user_ip": ip or "",
        "wlan_user_ipv6": "",
        "wlan_user_mac": "",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": str(cfg.get("js_version", "1.0")),
    }
    return base + "?" + urlparse.urlencode(params)


def build_portal_logout_url(cfg: dict, ip: str) -> str:
    """eportal 的注销接口（?c=Portal&a=logout），用于会话卡死时重来一次。"""
    base = cfg.get("portal_login_base") or DEFAULT_CONFIG["portal_login_base"]
    params = {
        "c": "Portal",
        "a": "logout",
        "callback": "dr1004",
        "user_account": "drcom",
        "user_password": "123",
        "ac_logout": "1",
        "register_mode": "1",
        "wlan_user_ip": ip or "",
        "wlan_user_ipv6": "",
        "wlan_user_mac": "",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": str(cfg.get("js_version", "1.0")),
    }
    return base + "?" + urlparse.urlencode(params)


def current_ssid(cfg: dict) -> str:
    """当前连着的无线网络名。只和配置里出现过的 SSID 比对，不解析本地化的 netsh 输出。"""
    ssids = [str(item.get("ssid")) for item in (cfg.get("profiles") or []) if item.get("ssid")]
    if cfg.get("wifi_ssid"):
        ssids.append(str(cfg.get("wifi_ssid")))
    ssids = [name for name in ssids if name]
    if not ssids:
        return ""
    # 1) netsh：SYSTEM / 管理员上下文可用，最快
    found = match_ssid(run_text(["netsh", "wlan", "show", "interfaces"]), ssids)
    if found:
        return found
    # 2) 网络列表管理器：普通用户上下文也能用（netsh 的 WlanQueryInterface 需要提权）
    found = match_ssid(run_text([
        "powershell", "-NoProfile", "-Command", "(Get-NetConnectionProfile).Name",
    ]), ssids)
    return found or ""


def run_text(command: list) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="gbk",
                                errors="replace", timeout=20, creationflags=NO_WINDOW_FLAG)
        return result.stdout or ""
    except Exception:
        return ""


def match_ssid(output: str, ssids: list) -> str:
    for name in ssids:
        if name and re.search(re.escape(name), output or "", re.IGNORECASE):
            return name
    return ""


def credentials_for(cfg: dict, ssid: str) -> dict:
    """按当前 SSID 取账号/密码/运营商后缀；profile 里的值优先，缺省回退到顶层配置。"""
    profile = {}
    for item in cfg.get("profiles") or []:
        if ssid and str(item.get("ssid") or "").lower() == ssid.lower():
            profile = item
            break
    suffix = profile.get("isp_suffix")
    prefix = profile.get("account_prefix")
    return {
        "account": str(profile.get("account") or cfg.get("account") or ""),
        "password": str(profile.get("password") or cfg.get("password") or ""),
        "suffix": str(cfg.get("isp_suffix") or "") if suffix is None else str(suffix),
        # 校园网（SEU-WLAN）实测要用 ",0," 前缀且不带后缀
        "prefix": str(cfg.get("account_prefix") or "") if prefix is None else str(prefix),
    }


def login_candidates(cfg: dict, ip: str, portal_base: str, creds: dict, ssid: str = ""):
    """返回要依次尝试的登录接口。auto = 先 PORTAL 协议，再退回本地认证。"""
    mode = str(cfg.get("login_mode") or "auto").lower()
    portal = (build_portal_login_url(cfg, ip, creds),
              "PORTAL 协议（eportal，本机 IP %s，网络 %s，账号 %s%s%s）"
              % (ip or "?", ssid or "未识别", creds.get("prefix", ""), creds["account"], creds["suffix"]))
    local = (build_login_url(portal_base, cfg, creds), "本地认证（/drcom/login）")
    if mode == "portal":
        return [portal]
    if mode == "drcom":
        return [local]
    candidates = [portal, local]
    # 门户不挂在 w.seu.edu.cn 时（东大其他网段），按探测到的地址推一个 eportal 入口再试一次
    derived = portal_base_from(portal_base)
    configured = (cfg.get("portal_login_base") or DEFAULT_CONFIG["portal_login_base"]).rstrip("/")
    if derived and not same_host(derived, configured):
        candidates.append((build_portal_login_url(cfg, ip, creds, derived),
                           "PORTAL 协议（按探测地址推导 %s）" % derived))
    return candidates


def portal_base_from(discovered: str) -> str:
    """从探测到的门户地址推导 eportal 登录入口，例如 http://10.9.10.100/ → http://10.9.10.100/eportal/"""
    parts = urlparse.urlsplit(discovered or "")
    host = parts.netloc
    if not host:
        return ""
    return "%s://%s/eportal/" % (parts.scheme or "http", host)


def same_host(url_a: str, url_b: str) -> bool:
    """只比较主机名：端口/协议不同（如 :801 https）不算不同门户。"""
    try:
        return (urlparse.urlsplit(url_a).hostname or "").lower() == \
               (urlparse.urlsplit(url_b).hostname or "").lower()
    except Exception:
        return False


def classify(body_text: str):
    """把门户返回分类为 ok / online / authfail / unknown。

    门户有两种返回：PORTAL 协议 dr1003({"result":"1","msg":"认证成功"})，
    本地认证 dr1003({"result":1,...})，已在线时是 {"result":"0","ret_code":2}。
    """
    match = re.search(r"\{.*\}", body_text, re.S)
    if not match:
        return "unknown", body_text.strip()[:120], None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return "unknown", match.group(0)[:120], None

    result = str(data.get("result"))
    msg = decode_portal_text(str(data.get("msg") or ""))
    msga = decode_portal_text(str(data.get("msga") or ""))
    ret_code = data.get("ret_code")
    text = (msg + " " + msga).lower()

    if result in ("1", "ok"):
        return "ok", msg or msga or "result=%s" % result, data
    if ret_code == 2 or "online" in text or "已在线" in msg:
        return "online", msg or msga or ("ret_code=%s" % ret_code), data
    for key in AUTH_FAIL_KEYS:
        if key in text:
            return "authfail", msg or msga, data
    return "unknown", msg or msga or ("ret_code=%s" % ret_code), data


def decode_portal_text(value: str) -> str:
    """门户有时把提示用 base64 返回，例如 QXV0aGVudGljYXRpb24gZmFpbA== → Authentication fail。"""
    text = (value or "").strip()
    if len(text) >= 8 and len(text) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text):
        try:
            decoded = base64.b64decode(text).decode("utf-8").strip()
            if decoded and decoded.isprintable() and any(char.isalnum() for char in decoded):
                return decoded
        except Exception:
            pass
    return text


def connectivity_ok(cfg: dict, timeout: float):
    """真实外网校验：未认证时这个请求会被门户劫持成登录页，内容就对不上了。"""
    url = cfg.get("connectivity_check_url") or DEFAULT_CONFIG["connectivity_check_url"]
    expect = cfg.get("connectivity_expect") or DEFAULT_CONFIG["connectivity_expect"]
    try:
        status, raw = http_get(make_opener(), url, timeout, cfg)
    except Exception as exc:
        return False, "%s: %s" % (exc.__class__.__name__, exc)
    text = decode_body(raw)
    if str(status) == "200" and expect in text:
        return True, "HTTP %s，内容匹配" % status
    if any(marker in text.lower() for marker in PORTAL_MARKERS):
        return False, "HTTP %s，被门户劫持回认证页" % status
    return False, "HTTP %s，内容不符（%s）" % (status, re.sub(r"\s+", " ", text.strip())[:60])


def wait_for_connectivity(cfg: dict, timeout: float) -> bool:
    total = max(1, int(cfg.get("connectivity_attempts", 3)))
    grace = max(0.0, float(cfg.get("connectivity_grace_seconds", 3)))
    for index in range(1, total + 1):
        good, info = connectivity_ok(cfg, timeout)
        if good:
            log(cfg, "    外网校验通过（%s）" % info)
            return True
        log(cfg, "    第 %d/%d 次外网校验未通过：%s" % (index, total, info))
        if index < total:
            time.sleep(grace)
    return False


def nudge_wifi(cfg: dict, waited: float, last_nudge: list, ssids: list) -> None:
    """本机完全没有可用网络时，主动让 Windows 去连指定 SSID。

    只在“一个可用网络都没有”时才动手，所以不会把已经从别处上网的机器抢过来。
    """
    if not cfg.get("wifi_nudge", True) or not ssids:
        return
    if waited < float(cfg.get("wifi_nudge_after_seconds", 8)):
        return
    gap = float(cfg.get("wifi_nudge_interval_seconds", 20))
    if last_nudge and (time.time() - last_nudge[0]) < gap:
        return
    if local_ip("223.5.5.5"):  # 已经有网络（哪怕还没认证），不抢
        return
    if last_nudge:
        last_nudge[0] = time.time()
    else:
        last_nudge.append(time.time())
    interface = str(cfg.get("wifi_interface") or "")
    for ssid in ssids:
        command = ["netsh", "wlan", "connect", "name=%s" % ssid]
        if interface:
            command.append("interface=%s" % interface)
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="gbk",
                                    errors="replace", timeout=20, creationflags=NO_WINDOW_FLAG)
            output = " ".join((result.stdout or "").split())[:100]
        except Exception as exc:
            output = "%s: %s" % (exc.__class__.__name__, exc)
        log(cfg, "    主动连接无线网络 %s：%s" % (ssid, output or "（无输出）"))


CARRIERS = {
    "1": ("@cmcc", "中国移动"),
    "2": ("@dx", "中国电信"),
    "3": ("@unicom", "中国联通"),
}

WIZARD_PROFILE_WLAN = """    {
      "ssid": "SEU-WLAN",         // 校园网：平时连的多数是这个
      "account_prefix": ",0,",    // 校园网的账号要带这个前缀、且不带后缀（实测）
      "isp_suffix": "",
      "account": "",              // 留空 = 用上面的 account
      "password": "",             // 留空 = 用上面的 password
      "note": "校园网"
    }"""

WIZARD_PROFILE_ISP = """    {
      "ssid": "SEU-ISP",          // 运营商宽带；没有的话这条可以删掉
      "isp_suffix": "%(suffix)s", %(comment)s
      "account": "",
      "password": "",             // 留空 = 用上面的 password
      "note": "%(note)s"
    }"""

WIZARD_CONFIG = """{
  // 本文件由首次运行向导生成，可以直接改；支持 // 注释
  "account": "%(account)s",     // 一卡通
  "password": "%(password)s",   // 密码（校园网 / 宽带同一个）
  "profiles": [
%(profiles)s
  ],
  "portal_urls": ["http://w.seu.edu.cn/", "http://10.80.128.2/", "http://10.9.10.100/"],
  "wifi_nudge": true,
  "wifi_ssid": "SEU-WLAN",
  "wifi_interface": "WLAN",
  "log_file": "login.log"
}
"""


def has_credentials(cfg: dict) -> bool:
    if not str(cfg.get("account") or "").strip():
        return False
    if str(cfg.get("password") or "").strip():
        return True
    return any(str(item.get("password") or "").strip() for item in (cfg.get("profiles") or []))


def first_run_wizard(force: bool = False) -> bool:
    """没有可用配置时，在控制台问几个问题并生成 config.json。"""
    if not force and not (sys.stdin and sys.stdout and sys.stdout.isatty()):
        return False  # 无窗口 exe / 计划任务里不提问
    print()
    print("=" * 64)
    print(" 第一次运行，回答三个问题就能用了（直接回车 = 用方括号里的默认值）")
    print("=" * 64)
    try:
        account = ""
        while not account:
            account = input(" 1/3 一卡通：").strip()
        password = input(" 2/3 密码（校园网和宽带是同一个）：").strip()
        print(" 3/3 有没有运营商宽带（SEU-ISP）？")
        print("      1 = 中国移动    2 = 中国电信    3 = 中国联通")
        print("      直接回车 = 没有，只用校园网")
        choice = input("      [回车 = 没有]：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n 已取消。")
        return False

    if choice in CARRIERS:
        suffix, carrier = CARRIERS[choice]
        isp_comment = "// 移动 @cmcc / 电信 @dx / 联通 @unicom"
        isp_note = "运营商宽带（%s）" % carrier
    else:
        suffix, carrier = CARRIERS["1"]
        isp_comment = "// 没有运营商宽带的话，这条可以不管或删掉"
        isp_note = "没有运营商宽带（可删）"
    profiles = WIZARD_PROFILE_WLAN + ",\n" + WIZARD_PROFILE_ISP % {
        "suffix": suffix, "carrier": carrier, "comment": isp_comment, "note": isp_note,
    }
    text = WIZARD_CONFIG % {
        "account": account,
        "password": password,
        "profiles": profiles,
    }
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        print(" 写配置文件失败：%s" % exc)
        return False
    print("\n 已保存配置：%s" % CONFIG_PATH)
    print(" 提示：两个网络用的是同一个一卡通号和同一个密码，要改直接编辑 config.json。")
    print(" 接下来自动试一次认证，结果会打印在下面。\n")
    return True


CAMPUS_SSIDS = {"SEU-WLAN", "SEU-ISP"}
CAMPUS_PORTAL_HOST = "w.seu.edu.cn"
CAMPUS_PORTAL_IP = "10.80.128.2"
CAMPUS_LOGIN_BASE = f"https://{CAMPUS_PORTAL_HOST}:802/eportal/"
CAMPUS_CACHE_PATH = os.path.join(BASE_DIR, "campus_portal_cache.json")
CAMPUS_BODY_LIMIT = 512 * 1024


class CampusRoute(NamedTuple):
    protocol: str
    port: int


CAMPUS_DEFAULT_ROUTES = {"SEU-WLAN": CampusRoute("classic", 802), "SEU-ISP": CampusRoute("portal", 804)}


def load_campus_cache() -> dict[str, CampusRoute]:
    try:
        with open(CAMPUS_CACHE_PATH, encoding="utf-8") as handle:
            data = json.loads(handle.read(8192))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    routes = {}
    for ssid, value in data.items():
        if ssid not in CAMPUS_SSIDS or not isinstance(value, dict):
            continue
        protocol, port = value.get("protocol"), value.get("port")
        if type(port) is int and 1 <= port <= 65535 and (protocol == "portal" or (protocol == "classic" and port == 802)):
            routes[ssid] = CampusRoute(protocol, port)
    return routes


def cache_campus_route(cfg: dict, cache: dict, ssid: str, route: CampusRoute) -> None:
    if cache.get(ssid) == route:
        return
    updated = {**cache, ssid: route}
    payload = {key: value._asdict() for key, value in updated.items()}
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=BASE_DIR, delete=False) as handle:
            pending = handle.name
            json.dump(payload, handle, indent=2)
        os.replace(pending, CAMPUS_CACHE_PATH)
        pending = None
        cache[ssid] = route
    except OSError:
        log(cfg, f"{ssid} 认证入口缓存写入失败，下次运行将重新检测")
    finally:
        if pending:
            try:
                os.remove(pending)
            except OSError:
                pass


def campus_source(cfg: dict) -> tuple[str, str, str] | None:
    """返回校园 SSID、无线网卡 IPv4 和 MAC。None 表示未确认校园网。"""
    netsh = run_text(["netsh", "wlan", "show", "interfaces"])
    names = {value.strip().upper() for value in re.findall(r"^[ \t]*SSID[ \t]*:[ \t]*([^\r\n]+)", netsh, re.M)}
    interface = "'" + str(cfg.get("wifi_interface") or "WLAN").replace("'", "''") + "'"
    installed_pwsh = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "PowerShell", "7", "pwsh.exe")
    pwsh = installed_pwsh if os.path.isfile(installed_pwsh) else (shutil.which("pwsh") or "powershell")
    command = [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    if not names:
        profiles = run_text(command + [f"(Get-NetConnectionProfile -InterfaceAlias {interface}).Name"])
        names = {name.strip().upper() for name in profiles.splitlines() if name.strip()}
    if len(names) != 1 or not names <= CAMPUS_SSIDS:
        return None

    # 默认路由可能返回 TUN 的虚拟地址，认证时要使用物理 WLAN 网卡的地址。
    output = run_text(command + [
        "[System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | "
        f"Where-Object {{ $_.Name -eq {interface} -and $_.OperationalStatus -eq 'Up' "
        "-and $_.NetworkInterfaceType -eq 'Wireless80211' } | "
        "ForEach-Object { $mac = $_.GetPhysicalAddress().ToString(); "
        "$_.GetIPProperties().UnicastAddresses | "
        "Where-Object { $_.Address.AddressFamily -eq 'InterNetwork' } | "
        "ForEach-Object { '{0}|{1}' -f $_.Address.IPAddressToString, $mac } }"
    ])
    addresses = re.findall(r"^[ \t]*(10(?:\.\d{1,3}){3})\|([0-9A-Fa-f]{12})?[ \t]*\r?$", output, re.M)
    address, mac = addresses[0] if len(addresses) == 1 else ("", "")
    return next(iter(names)), address, mac.upper()


class CampusPortalHTTPError(OSError):
    def __init__(self, status: int):
        super().__init__(f"Campus portal returned HTTP {status}")
        self.status = status


def campus_portal_get(port: int, path: str, source: str, timeout: float) -> str:
    """限定为已核对的校内网关，TLS 仍校验学校域名。"""
    context = ssl.create_default_context()
    with socket.create_connection((CAMPUS_PORTAL_IP, port), timeout, source_address=(source, 0)) as channel:
        with context.wrap_socket(channel, server_hostname=CAMPUS_PORTAL_HOST) as tls:
            with closing(http.client.HTTPSConnection(CAMPUS_PORTAL_HOST, port, timeout=timeout)) as connection:
                connection.sock = tls
                connection.request("GET", path, headers={"Connection": "close", "Cache-Control": "no-cache"})
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise CampusPortalHTTPError(response.status)
                    body = response.read(CAMPUS_BODY_LIMIT + 1)
                    if len(body) > CAMPUS_BODY_LIMIT:
                        raise ValueError("Campus portal response is too large")
                    if response.getheader("Content-Encoding", "").lower() == "gzip":
                        try:
                            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                                body = compressed.read(CAMPUS_BODY_LIMIT + 1)
                        except (OSError, EOFError, zlib.error) as exc:
                            raise ValueError("Invalid compressed portal response") from exc
                        if len(body) > CAMPUS_BODY_LIMIT:
                            raise ValueError("Decompressed portal response is too large")
                    return decode_body(body)


class CampusScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        src = dict(attrs).get("src")
        if not src:
            return
        try:
            url = urlparse.urlsplit(urlparse.urljoin(f"https://{CAMPUS_PORTAL_HOST}/", src))
            if (url.scheme == "https" and url.hostname == CAMPUS_PORTAL_HOST and url.port in (None, 443)
                    and url.username is None and re.fullmatch(r"/[A-Za-z0-9_./-]+\.js", url.path)):
                if url.path not in self.paths:
                    self.paths.append(url.path)
        except ValueError:
            pass


class UnsupportedCampusPortal(ValueError):
    pass


def discover_campus_route(ssid: str, source: str, timeout: float) -> CampusRoute:
    """仅解析已校验证书的校园网资源，不执行页面 JavaScript，也不发送凭据。"""
    home = campus_portal_get(443, "/", source, timeout)
    scripts = CampusScripts()
    scripts.feed(home)
    declaration = r"\benHTTPSPort\s*=\s*['\"]?([1-9]\d{0,4})['\"]?(?=\s*(?:[;,\r\n]|$))"
    ports = set(re.findall(declaration, home))
    # 旧版门户也可能通过内联脚本加载 a41.js。
    for path in dict.fromkeys([*scripts.paths[:4], "/a41.js"]):
        if ports:
            break
        try:
            text = campus_portal_get(443, path, source, timeout)
        except CampusPortalHTTPError as exc:
            if exc.status == 404:
                continue
            raise
        ports.update(re.findall(declaration, text))
    if len(ports) != 1 or int(next(iter(ports))) > 65535:
        raise ValueError("No unique HTTPS port declared by campus portal")
    port = int(next(iter(ports)))
    query = urlparse.urlencode({
        "callback": "dr1003",
        "wlan_user_ip": base64.b64encode(source.encode("ascii")).decode("ascii"),
        "wlan_user_ssid": ssid,
    })
    body = campus_portal_get(port, f"/eportal/portal/page/loadConfig?{query}", source, timeout)
    _, _, data = classify(body)
    public = data.get("data") if isinstance(data, dict) and str(data.get("code")) == "1" else None
    if not isinstance(public, dict):
        raise ValueError("Unrecognized campus page configuration")
    if str(public.get("login_method")) != "1" or str(public.get("en_md5", 0)).lower() in {"1", "true"}:
        raise UnsupportedCampusPortal("Campus portal requires an unsupported login method")
    return CampusRoute("portal", port)


def campus_online(source: str, timeout: float) -> bool:
    body = campus_portal_get(443, "/drcom/chkstatus?callback=dr1002", source, timeout)
    _, _, data = classify(body)
    if not isinstance(data, dict) or str(data.get("result")) not in {"0", "1"}:
        raise ValueError("Unrecognized campus portal status")
    if str(data["result"]) == "0":
        return False
    if not any(data.get(key) == source for key in ("v4ip", "v46ip", "lip")):
        raise ValueError("Campus portal reported a different source address")
    return True


def portal_online(port: int, source: str, mac: str, timeout: float) -> bool:
    query = urlparse.urlencode({
        "callback": "dr1003",
        "wlan_user_ip": base64.b64encode(source.encode("ascii")).decode("ascii"),
        "wlan_user_mac": mac,
        "login_method": "1",
    })
    body = campus_portal_get(port, f"/eportal/portal/online_list?{query}", source, timeout)
    _, _, data = classify(body)
    if not isinstance(data, dict) or str(data.get("result")) not in {"0", "1"}:
        raise ValueError("Unrecognized ePortal status")
    if str(data["result"]) == "0":
        return False
    entries = data.get("list")
    if not isinstance(entries, list):
        raise ValueError("Missing ePortal online records")
    return any(isinstance(item, dict) and item.get("online_ip") == source for item in entries)


def campus_route_online(route: CampusRoute, source: str, mac: str, timeout: float) -> bool:
    if route.protocol == "classic":
        return campus_online(source, timeout)
    return portal_online(route.port, source, mac, timeout)


def campus_login_request(cfg: dict, route: CampusRoute, source: str, mac: str, creds: dict) -> str:
    legacy = urlparse.urlsplit(build_portal_login_url(cfg, source, creds, CAMPUS_LOGIN_BASE))
    if route.protocol == "classic":
        return f"{legacy.path}?{legacy.query}"

    # ISP 门户的 HTTPS 前端把认证方式 1 的请求发给 /eportal/portal/login。
    params = dict(urlparse.parse_qsl(legacy.query, keep_blank_values=True))
    params.pop("c")
    params.pop("a")
    params.update(login_method="1", wlan_user_mac=mac, jsVersion="4.X", operate="portal_login")
    return f"/eportal/portal/login?{urlparse.urlencode(params)}"


def portal_error_reason(exc: Exception) -> str:
    return f"HTTP {exc.status}" if isinstance(exc, CampusPortalHTTPError) else type(exc).__name__


def should_rediscover(phase: str, exc: Exception) -> bool:
    if isinstance(exc, CampusPortalHTTPError):
        return exc.status in {301, 302, 303, 307, 308, 404, 405, 410, 421}
    return isinstance(exc, (ValueError, ssl.SSLError, ConnectionRefusedError, http.client.HTTPException)) or (
        phase == "查询在线状态" and isinstance(exc, OSError)
    )


def login_campus_wifi(cfg: dict, once: bool, lock: str = "") -> int:
    timeout = float(cfg.get("request_timeout_seconds", 4))
    login_timeout = float(cfg.get("login_timeout_seconds", 12))
    interval = max(2.0, float(cfg.get("retry_interval_seconds", 10)))
    deadline = time.monotonic() + (0.0 if once else float(cfg.get("max_wait_seconds", 300)))
    cache = load_campus_cache()
    routes = dict(cache)
    refreshed = set()
    portal_errors = (OSError, ValueError, http.client.HTTPException)
    while True:
        touch_lock(lock)
        connection = campus_source(cfg)
        if connection is None:
            log(cfg, "当前未确认连接 SEU-WLAN 或 SEU-ISP，本次跳过")
            return 0
        ssid, source, mac = connection
        if source:
            if ssid not in routes:
                routes[ssid] = CAMPUS_DEFAULT_ROUTES[ssid]
                if ssid == "SEU-ISP":
                    try:
                        routes[ssid] = discover_campus_route(ssid, source, timeout)
                    except UnsupportedCampusPortal:
                        log(cfg, f"{ssid} 门户需要新的认证方式，请使用网页认证")
                        return 1
                    except portal_errors as exc:
                        log(cfg, f"{ssid} 入口发现暂不可用（{portal_error_reason(exc)}），使用默认入口检查")
                    else:
                        log(cfg, f"{ssid} 已从学校门户发现 HTTPS {routes[ssid].port} 认证入口")
            route = routes[ssid]
            touch_lock(lock)
            phase = "查询在线状态"
            try:
                already_online = campus_route_online(route, source, mac, timeout)
                unknown_response = False
                if not already_online:
                    if campus_source(cfg) != connection:
                        log(cfg, f"{ssid} 连接已变化，本次认证跳过")
                        return 0
                    creds = credentials_for(cfg, ssid)
                    if ssid == "SEU-ISP":
                        suffix = creds["suffix"].strip().casefold()
                        if suffix == "@lt":
                            suffix = CARRIERS["3"][0]
                        if suffix not in {carrier_suffix for carrier_suffix, _ in CARRIERS.values()}:
                            log(cfg, "SEU-ISP 缺少有效的运营商配置")
                            return 3
                        creds["suffix"] = suffix
                    touch_lock(lock)
                    phase = "提交认证"
                    target = campus_login_request(cfg, route, source, mac, creds)
                    body = campus_portal_get(route.port, target, source, login_timeout)
                    state, _, _ = classify(body)
                    if state == "authfail":
                        log(cfg, f"{ssid} 门户拒绝认证")
                        return 2
                    unknown_response = state == "unknown"
                touch_lock(lock)
                phase = "复核在线状态"
                if campus_source(cfg) != connection:
                    log(cfg, f"{ssid} 连接已变化，本次复核跳过")
                    return 0
                online = already_online or campus_route_online(route, source, mac, timeout)
                if online:
                    reachable, _ = connectivity_ok(cfg, timeout)
                    if reachable and campus_source(cfg) == connection:
                        cache_campus_route(cfg, cache, ssid, route)
                        if already_online:
                            log(cfg, f"{ssid} 门户确认该网卡已在线，外网探测通过，无需重复认证")
                        else:
                            log(cfg, f"提交 {ssid} 认证后，门户确认该网卡在线，外网探测通过")
                        return 0
                elif unknown_response:
                    phase = "提交认证"
                    raise ValueError("Unrecognized login response")
            except portal_errors as exc:
                # 异常消息或完整响应可能含登录 URL 或身份信息。
                log(cfg, f"{ssid} {phase}未通过（{portal_error_reason(exc)}）")
                if ssid not in refreshed and should_rediscover(phase, exc):
                    refreshed.add(ssid)
                    touch_lock(lock)
                    try:
                        candidate = discover_campus_route(ssid, source, timeout)
                    except UnsupportedCampusPortal:
                        log(cfg, f"{ssid} 门户需要新的认证方式，请使用网页认证")
                        return 1
                    except portal_errors as discovery_error:
                        log(cfg, f"{ssid} 重新发现认证入口失败（{portal_error_reason(discovery_error)}）")
                    else:
                        if candidate != route:
                            routes[ssid] = candidate
                            log(cfg, f"{ssid} 重新发现 HTTPS {candidate.port} 入口，先检查在线状态再尝试认证")
                            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log(cfg, f"{ssid} 未能确认校园网认证和外网同时可用")
            return 1
        time.sleep(min(interval, remaining))


def main(argv) -> int:
    cfg = load_config()
    if cfg.get("_config_error"):
        log(cfg, "读取 config.json 出错：%s" % cfg["_config_error"])
    if "--init" in argv:
        first_run_wizard(force=True)
        cfg = load_config()
    elif not has_credentials(cfg):
        if first_run_wizard():
            cfg = load_config()
    if not cfg.get("account") or not cfg.get("password"):
        log(cfg, "配置不完整：account 或 password 为空。双击 1-首次配置并测试.cmd，"
                 "或运行 %s --init，按提示填一次即可" % os.path.basename(sys.executable or "seu-autologin.exe"))
        return 3

    lock = acquire_lock(cfg)
    if lock is None:
        log(cfg, "已有另一个认证实例在运行，本次跳过")
        return 0

    once = "--once" in argv
    use_campus_wifi = (
        cfg.get("campus_wifi_only", True)
        and str(cfg.get("login_mode") or "auto").lower() == "auto"
        and (cfg.get("portal_login_base") or DEFAULT_CONFIG["portal_login_base"])
        == DEFAULT_CONFIG["portal_login_base"]
        and (cfg.get("portal_urls") or DEFAULT_CONFIG["portal_urls"])
        == DEFAULT_CONFIG["portal_urls"]
    )
    if use_campus_wifi:
        try:
            return login_campus_wifi(cfg, once, lock)
        finally:
            release_lock(lock)

    timeout = float(cfg.get("request_timeout_seconds", 6))
    login_timeout = float(cfg.get("login_timeout_seconds", 12))
    interval = max(1.0, float(cfg.get("retry_interval_seconds", 10)))
    fast_interval = max(0.5, float(cfg.get("fast_retry_seconds", 2)))
    fast_window = max(0.0, float(cfg.get("fast_retry_window_seconds", 120)))
    started = time.time()
    deadline = started + (0.0 if once else float(cfg.get("max_wait_seconds", 300)))
    opener = make_opener()

    log(
        cfg,
        "===== 开始认证：默认账号 %s%s，无线网络自动识别 ====="
        % (cfg.get("account"), cfg.get("isp_suffix") or ""),
    )

    wifi_ssids = [str(item.get("ssid")) for item in (cfg.get("profiles") or []) if item.get("ssid")]
    if cfg.get("wifi_ssid"):
        wifi_ssids.append(str(cfg.get("wifi_ssid")))
    wifi_ssids = [name for name in wifi_ssids if name]

    try:
        attempt = 0
        quiet_until = 0.0
        stuck = 0
        logout_done = False
        last_nudge = []
        while True:
            attempt += 1
            now = time.time()
            touch_lock(lock)
            # 刚开机 / 刚登录的头几分钟网络可能还在连，用更短的间隔抢时间，之后回到正常间隔
            wait = fast_interval if (now - started) < fast_window else interval
            base, info = find_portal(opener, cfg, timeout)

            if base is None:
                nudge_wifi(cfg, now - started, last_nudge, wifi_ssids)
                # 探不到门户、但外网能用 → 多半不在校园网（家里 / 手机热点），不用白等 5 分钟
                if attempt >= 3 and attempt % 3 == 0:
                    reachable, _info = connectivity_ok(cfg, timeout)
                    if reachable:
                        log(cfg, "第 %d 次：探测不到校园网认证门户，但外网是通的（当前不在校园网环境），本次结束"
                            % attempt)
                        return 0
                # 网络还没起来时失败是常态，只在前几次和每分钟打一条日志，避免刷屏
                if attempt <= 3 or now >= quiet_until:
                    log(cfg, "第 %d 次：未发现校园网认证门户（%s），%g 秒后重试" % (attempt, info, wait))
                    quiet_until = now + 60
            else:
                source_ip = local_ip()
                ssid = current_ssid(cfg)
                creds = credentials_for(cfg, ssid)
                candidates = login_candidates(cfg, source_ip, base, creds, ssid)
                for index, (login_url, mode_desc) in enumerate(candidates, 1):
                    state, detail, snippet = "unknown", "", ""
                    try:
                        _status, raw = http_get(opener, login_url, login_timeout, cfg,
                                                host_header_for(login_url, cfg))
                        body = decode_body(raw)
                        state, detail, _data = classify(body)
                        snippet = re.sub(r"\s+", " ", body.strip())[:160]
                    except Exception as exc:
                        state, detail = "unknown", "%s: %s" % (exc.__class__.__name__, exc)

                    log(
                        cfg,
                        "第 %d 次-%d：提交认证（%s）→ 门户返回 %s%s"
                        % (attempt, index, mode_desc, detail, ("｜" + snippet) if snippet else ""),
                    )

                    if state == "authfail":
                        log(cfg, "第 %d 次：认证被拒绝——请检查账号、密码或运营商后缀，已停止重试" % attempt)
                        return 2

                    # 门户说什么都只是参考，最终以外网是否真的通了为准
                    if wait_for_connectivity(cfg, timeout):
                        log(cfg, "第 %d 次：认证完成，外网已连通" % attempt)
                        return 0

                    if state == "online":
                        stuck += 1
                        if stuck >= 2 and not logout_done:
                            logout_done = True
                            log(cfg, "    连续 2 次「门户说在线但外网不通」，先注销再重新认证")
                            try:
                                logout_url = build_portal_logout_url(cfg, source_ip)
                                http_get(opener, logout_url, login_timeout, cfg,
                                         host_header_for(logout_url, cfg))
                            except Exception as exc:
                                log(cfg, "    注销请求失败：%s" % exc.__class__.__name__)

                    if index < len(candidates):
                        log(cfg, "    该协议没打通外网，换下一个协议")

                log(cfg, "第 %d 次：外网仍不通，%g 秒后重试整个流程" % (attempt, wait))

            if time.time() >= deadline:
                log(cfg, "等待超时，退出（本次未完成认证，下次触发时会再试）")
                return 1
            time.sleep(wait)
    finally:
        release_lock(lock)


def wait_for_enter() -> None:
    """双击 .cmd 运行时，让窗口停住等用户看完结果（计划任务里不会触发）。"""
    try:
        if sys.stdin and sys.stdout and sys.stdout.isatty():
            input("\n按回车键关闭窗口...")
    except Exception:
        pass


def scheduled_task_installed() -> bool:
    """检查自动任务是否已安装（用于提示，查不到就当没装）。"""
    return "SEU-ISP-AutoLogin" in run_text(["schtasks", "/query", "/tn", "SEU-ISP-AutoLogin"])


if __name__ == "__main__":
    args = sys.argv[1:]
    code = main(args)
    if code == 0 and "--once" in args and not scheduled_task_installed():
        print("\n提示：上面只是手动跑了一次。想要自动认证（开机 / 登录 / 切换网络 / 睡眠唤醒），")
        print("      请双击「2-安装开机自启.cmd」装一次；不装的话每次都得手动运行本程序。")
    if "--pause" in args:
        wait_for_enter()
    sys.exit(code)
