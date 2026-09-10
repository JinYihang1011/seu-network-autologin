#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEU-ISP 校园网自动登录脚本（东南大学运营商宽带，Drcom 认证门户）

作用：开机 / 登录 Windows 后自动完成 SEU-ISP 的网页认证，
      免去每次开机手动打开浏览器、输入账号密码、选择运营商“中国移动”的步骤。

原理：
  1. 直连探测认证门户 http://w.seu.edu.cn/ 或 http://10.80.128.2/（明确不走系统代理）
  2. 确认返回的是 Drcom 门户页面后，调用 /drcom/login 接口提交账号 + 密码 + 运营商后缀
  3. 解析返回的 dr1003({...}) JSON，判定：登录成功 / 已在线 / 账号密码错误 / 结果未知
  4. 网络没就绪时按间隔重试，直到成功、被明确拒绝或超过最大等待时间

配置：同目录 config.json        日志：同目录 login.log
手动运行：python seu_isp_login.py            正常重试模式
          python seu_isp_login.py --once     只试一次，方便排查
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib import error as urlerror  # noqa: F401  (保留备用: 捕获 HTTPError/URLError)
from urllib import parse as urlparse
from urllib import request as urlrequest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "account": "",
    "password": "",
    "isp_suffix": "@cmcc",
    # 连上哪个无线网络就用哪一份配置；没匹配到的 SSID 用上面的默认值
    "profiles": [
        {"ssid": "SEU-ISP", "isp_suffix": "@cmcc", "note": "运营商宽带（中国移动）"},
        {"ssid": "SEU-WLAN", "isp_suffix": "@xyw", "note": "校园网（校园用户）"},
    ],
    # 登录协议：SEU 对本机所在网段(10.210.0.0/16)用的是 PORTAL 协议(eportal)，
    # 本地认证 /drcom/login 虽然也会返回 result=1，但不会在 AC 上真正放行。
    "login_mode": "auto",
    "portal_login_base": "https://w.seu.edu.cn:801/eportal/",
    "portal_login_method": 1,
    "js_version": "1.0",
    "portal_urls": ["http://w.seu.edu.cn/", "http://10.80.128.2/"],
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
            cfg.update(json.load(handle))
    except FileNotFoundError:
        pass
    except Exception as exc:  # 配置坏了也要能记日志说明原因
        cfg["_config_error"] = "%s: %s" % (exc.__class__.__name__, exc)
    return cfg


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
        "DDDDD": creds["account"] + creds["suffix"],
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


def build_portal_login_url(cfg: dict, ip: str, creds: dict) -> str:
    """eportal 的 PORTAL 协议登录接口（?c=Portal&a=login）。"""
    base = cfg.get("portal_login_base") or DEFAULT_CONFIG["portal_login_base"]
    params = {
        "c": "Portal",
        "a": "login",
        "callback": "dr1003",
        "login_method": str(cfg.get("portal_login_method", 1)),
        "user_account": creds["account"] + creds["suffix"],
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
                                errors="replace", timeout=20)
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
    return {
        "account": str(profile.get("account") or cfg.get("account") or ""),
        "password": str(profile.get("password") or cfg.get("password") or ""),
        "suffix": str(cfg.get("isp_suffix") or "") if suffix is None else str(suffix),
    }


def login_candidates(cfg: dict, ip: str, portal_base: str, creds: dict, ssid: str = ""):
    """返回要依次尝试的登录接口。auto = 先 PORTAL 协议，再退回本地认证。"""
    mode = str(cfg.get("login_mode") or "auto").lower()
    portal = (build_portal_login_url(cfg, ip, creds),
              "PORTAL 协议（eportal，本机 IP %s，网络 %s，账号 %s%s）"
              % (ip or "?", ssid or "未识别", creds["account"], creds["suffix"]))
    local = (build_login_url(portal_base, cfg, creds), "本地认证（/drcom/login）")
    if mode == "portal":
        return [portal]
    if mode == "drcom":
        return [local]
    return [portal, local]


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
    msg = str(data.get("msg") or "")
    msga = str(data.get("msga") or "")
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
                                    errors="replace", timeout=20)
            output = " ".join((result.stdout or "").split())[:100]
        except Exception as exc:
            output = "%s: %s" % (exc.__class__.__name__, exc)
        log(cfg, "    主动连接无线网络 %s：%s" % (ssid, output or "（无输出）"))


def main(argv) -> int:
    cfg = load_config()
    if cfg.get("_config_error"):
        log(cfg, "读取 config.json 出错：%s" % cfg["_config_error"])
    if not cfg.get("account") or not cfg.get("password"):
        log(cfg, "配置不完整：account 或 password 为空，请检查 config.json")
        return 3

    lock = acquire_lock(cfg)
    if lock is None:
        log(cfg, "已有另一个认证实例在运行，本次跳过")
        return 0

    once = "--once" in argv
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
