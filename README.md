# SEU-ISP 自动登录

东南大学校园宽带（SEU-ISP）**开机自动认证**脚本：不用再打开浏览器、输入账号密码、选择运营商。

Python 3 标准库实现，无第三方依赖，已在 Windows 11 上实测通过。

## 特性

- **锁屏阶段就认证**：系统启动时由 SYSTEM 计划任务触发，不用等登录 Windows
- **登录后兜底 + 断线自愈**：用户登录时再触发一次，之后每 15 分钟检查一次
- **不只信门户返回**：每次认证后真的去请求一次外网，通了才算成功
- **会话卡死自愈**：连续两次「门户说在线但外网不通」会自动注销后重新认证
- **单实例锁**：两个任务同时被唤醒时只跑一个，不会重复提交
- **直连门户**：明确绕过系统代理

## 原理

东南大学的认证门户是 Drcom 系统（`w.seu.edu.cn`）。把网页上的登录动作拆成 HTTP 请求：

| 动作 | 请求 |
| --- | --- |
| 登录 | `https://w.seu.edu.cn:801/eportal/?c=Portal&a=login` |
| 注销 | `https://w.seu.edu.cn:801/eportal/?c=Portal&a=logout` |

登录参数：

| 参数 | 说明 |
| --- | --- |
| `login_method` | `1`（PORTAL 协议） |
| `user_account` | `<学号>@cmcc`，其中 `cmcc`=中国移动、`dx`=中国电信、`lt`=中国联通、`xyw`=校园网 |
| `user_password` | 密码 |
| `wlan_user_ip` | 本机 IP，脚本自动获取 |
| `jsVersion` | `1.0` |

成功返回 `dr1003({"result":"1","msg":"认证成功"})`；已经在线时是 `{"result":"0","ret_code":2}`。

> **踩坑记录**：老接口 `http://w.seu.edu.cn/drcom/login`（本地认证）**也会返回 `result=1`**，
> 但它不会在 AC 上真正放行——表现为「日志说登录成功，实际还是没网」。
> 本脚本默认走 PORTAL 协议，并以外网是否真的通作为最终判据。

## 为什么还要做外网校验

未认证时，任何 HTTP 请求都会被门户劫持成认证页（返回 200 + 一段门户 HTML）。
所以脚本在认证后请求 `http://www.msftconnecttest.com/connecttest.txt`，
只有拿到 `Microsoft Connect Test` 才算成功：

- 门户说成功、外网也通 → 收工
- 门户说成功、外网不通 → 继续重试；连续两次则先注销再重登

## 安装

```powershell
git clone https://github.com/<你的用户名>/seu-isp-autologin.git
cd seu-isp-autologin
copy config.example.json config.json
notepad config.json                 # 填 account / password / isp_suffix
python seu_isp_login.py --once      # 先手动跑一次，确认能认证成功
```

确认没问题后，双击 `install_tasks.cmd`（会弹 UAC，选「是」），它会注册两个计划任务：

| 任务名 | 触发条件 | 运行身份 |
| --- | --- | --- |
| `SEU-ISP-AutoLogin-Boot` | 系统启动时（锁屏阶段） | SYSTEM / 最高权限 |
| `SEU-ISP-AutoLogin` | 用户登录后立即 + 每 15 分钟 | 当前用户 / 普通权限 |

> Windows 的「快速启动」开启时，「关机再开机」其实是从休眠恢复，不会触发开机触发器，
> 这时由登录任务接上；真正的「重启」才走 Boot 任务。两个任务合起来覆盖各种情况。

## 验证（不用重启）

```powershell
python logout.py                 # 调用门户注销接口，本机立刻断网
python seu_isp_login.py --once   # 跑一次认证
```

成功时 `login.log` 长这样：

```
第 1 次-1：提交认证（PORTAL 协议（eportal，本机 IP 10.210.12.124））→ 门户返回 认证成功｜dr1003({"result":"1","msg":"认证成功"})
    外网校验通过（HTTP 200，内容匹配）
第 1 次：认证完成，外网已连通
```

## 配置项

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `account` | — | 学号 |
| `password` | — | 密码 |
| `isp_suffix` | `@cmcc` | 运营商后缀：移动 `@cmcc`、电信 `@dx`、联通 `@lt`、校园网 `@xyw` |
| `login_mode` | `auto` | `auto` = 先 PORTAL 协议，失败再回退本地认证；也可强制 `portal` / `drcom` |
| `portal_login_base` | `https://w.seu.edu.cn:801/eportal/` | 登录 / 注销接口地址 |
| `max_wait_seconds` | `300` | 单次运行最多等多久（开机时网络可能起得慢） |
| `fast_retry_seconds` / `fast_retry_window_seconds` | `2` / `120` | 前 2 分钟每 2 秒探测一次门户 |
| `retry_interval_seconds` | `10` | 2 分钟之后的重试间隔 |
| `wifi_nudge` | `true` | 完全没有网络时主动执行 `netsh wlan connect` 催 Windows 连网 |
| `wifi_ssid` / `wifi_interface` | `SEU-ISP` / `WLAN` | 上面那条用的 SSID 和网卡名 |
| `log_file` / `log_max_kb` | `login.log` / `512` | 日志文件，超限自动截断保留后半段 |

## 目录结构

```
seu_isp_login.py        主脚本（Python 3 标准库，无第三方依赖）
config.example.json     配置模板，复制成 config.json 后填自己的账号密码
logout.py               把本机踢下线，用来测试自动登录
install_tasks.ps1       注册两个计划任务（需管理员）
install_tasks.cmd       双击即可（自动提权）
```

## 排查

看 `login.log`：

| 日志 | 含义 |
| --- | --- |
| `认证完成，外网已连通` | 正常 |
| `门户返回 认证成功` 但 `外网校验未通过` | 认证没真正生效，脚本会继续重试 |
| `认证被拒绝` | 账号、密码或运营商后缀不对 |
| `未发现校园网认证门户` | 请求到不了门户：确认连着 SEU-ISP；开着 Clash 的 TUN 模式会劫持去门户的路由，需要关掉 TUN 或让 Clash 晚于认证启动 |
| `已有另一个认证实例在运行，本次跳过` | 两个任务同时被唤醒，属正常 |

## 关于速度

实测一次重启（系统事件日志 + `login.log` 对齐）：

| 阶段 | 耗时 |
| --- | --- |
| 内核启动 → 锁屏就绪 | 27 秒（Windows 开机本身） |
| 锁屏就绪 → WiFi 关联成功 | 20 秒（Windows WLAN 服务扫描 / 关联） |
| WiFi 关联 → 认证完成、外网通 | 9 秒（其中门户响应约 6 秒） |

脚本在锁屏就绪的同一秒就启动，一直在每 2 秒探测门户，网络一通就立刻认证——
也就是说等待时间基本都在 Windows 自己把 WiFi 拉起来，脚本不是瓶颈。
`wifi_nudge` 是能做的一点补救：完全没有网络时主动催一次 `netsh wlan connect`。

## 说明与免责

- `config.json` 里是**明文密码**，已加入 `.gitignore`，请勿提交或外传。
- 仅供学习和个人便利使用，请遵守学校的网络管理规定。
- 只在东南大学（SEU）网络环境下验证过；其他学校的 Drcom 门户接口可能不同，
  但 `seu_isp_login.py` 里的接口地址、参数、运营商后缀都可以在 `config.json` 里改。
