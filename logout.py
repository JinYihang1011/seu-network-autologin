# -*- coding: utf-8 -*-
"""把本机从 SEU-ISP 踢下线（调用 eportal 注销接口），用于测试自动登录。

用法：python logout.py
注销后再运行 seu_isp_login.py --once，就能看到真实的“认证成功”过程。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seu_isp_login as S  # noqa: E402

cfg = S.load_config()
ip = S.local_ip()
url = S.build_portal_logout_url(cfg, ip)
body = S.decode_body(S.http_get(S.make_opener(), url, 8, cfg, S.host_header_for(url, cfg))[1])
print("本机 IP:", ip)
print("注销返回:", body.strip()[:200])
