# 东南大学校园网自动登录（SEU-ISP / SEU-WLAN）

东南大学校园网**开机自动认证**：不用再打开浏览器、输入账号密码、选择运营商/服务类型。

同时支持 **SEU-ISP**（运营商宽带）和 **SEU-WLAN**（校园网）——脚本会读当前连着的无线网络，
自动选用对应的一份配置（账号、密码、运营商后缀）。

Python 3 标准库实现，无第三方依赖，已在 Windows 11 + SEU-ISP 上实测通过。

## 特性

- **锁屏阶段就认证**：系统启动时由 SYSTEM 计划任务触发，不用等登录 Windows
- **同时支持 SEU-ISP 与 SEU-WLAN**：按当前 SSID 自动切换账号与「服务类型」后缀
- **登录后兜底 + 断线自愈**：用户登录时再触发一次，之后每 15 分钟检查一次
- **不只信门户返回**：每次认证后真的去请求一次外网，通了才算成功
- **会话卡死自愈**：连续两次「门户说在线但外网不通」会自动注销后重新认证
- **单实例锁**：两个任务同时被唤醒时只跑一个，不会重复提交
- **直连门户**：明确绕过系统代理

## 支持的网络与服务类型

| 无线网络 | 实际提交的账号格式 | 说明 |
| --- | --- | --- |
| SEU-ISP（中国移动） | `一卡通号@cmcc` | 电信 `@dx`、联通 `@lt` |
| SEU-WLAN（校园用户） | `,0,一卡通号` | 无感知认证前缀，**不带后缀**（实测） |

两个网络是同一个门户（`w.seu.edu.cn`），但账号写法不同，脚本按当前 SSID 自动选。

后缀来自门户自己下发的服务类型列表。`config.json` 的 `profiles` 里按 SSID 写好即可，
连上哪个网络就用哪一份（没匹配到时用顶层的 `account` / `password` / `isp_suffix`）。
两个网络**一般用同一个密码**；万一不一样，在对应 profile 里单独写 `password` 即可，
留空则用顶层那个 —— 详见下面的「配置文件怎么填」。

## 配置文件怎么填

`config.json` **支持 `//` 和 `/* */` 注释**：程序会先剥掉注释再解析，所以你可以直接在文件里写备注。
放哪儿：和 exe（或 `seu_isp_login.py`）放在同一个目录，`login.log` 也会生成在那里。

```jsonc
{
  "account": "一卡通号",     // ← 登录校园网时输的那个号
  "password": "密码",        // ← 校园网和宽带是同一个密码
  "profiles": [
    {
      "ssid": "SEU-WLAN",      // 校园网：平时连的多数是这个
      "account_prefix": ",0,", // 校园网实测要这个前缀
      "isp_suffix": "",        // 且不带后缀
      "account": "",           // 留空 = 用上面的 account
      "password": "",          // 留空 = 用上面的 password
      "note": "校园网"
    },
    {
      "ssid": "SEU-ISP",       // 运营商宽带（没有的话这条可删）
      "isp_suffix": "@cmcc",   // 移动 @cmcc、电信 @dx、联通 @lt
      "account": "",
      "password": "",          // 校园网和宽带密码不一样时填这里
      "note": "运营商宽带（中国移动）"
    }
  ]
}
```

几个要点：

- **`ssid` 必须和 Windows 里显示的无线网络名一致**（大小写无所谓）。写错时日志会显示 `网络 未识别`，
  然后退回用顶层默认后缀 —— 这时很容易出现「后缀对不上、认证被拒」。
- **`account_prefix`**：最终提交的账号 = `前缀 + 账号 + 后缀`。SEU-WLAN 实测要用 `,0,` 前缀、后缀留空。
- **`password` 留空就用顶层那个**，写 `""` 和删掉这一行等价。
- 密码填错时门户返回 `Authentication fail`，日志会写成 `认证被拒绝（Authentication fail）`，
  脚本会立刻停下并提示，不会傻重试。
- 其余参数（接口地址、超时、重试节奏、日志大小、没网时催连 WiFi 等）示例文件里逐条都有注释，一般不用动。

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
| `user_account` | `<一卡通号>@cmcc` / `<一卡通号>@xyw`（见上表） |
| `user_password` | 密码 |
| `wlan_user_ip` | 本机 IP，脚本自动获取 |
| `jsVersion` | `1.0` |

成功返回 `dr1003({"result":"1","msg":"认证成功"})`；已经在线时是 `{"result":"0","ret_code":2}`。

> **踩坑记录**：老接口 `http://w.seu.edu.cn/drcom/login`（本地认证）**也会返回 `result=1`**，
> 但它不会在 AC 上真正放行——表现为「日志说登录成功，实际还是没网」。
> 本脚本默认走 PORTAL 协议，并以外网是否真的通作为最终判据。

> 东大不同网段的门户地址不一样（例如有人的登录页是 `http://10.9.10.100/`）。脚本会按
> `portal_urls` 依次探测；如果发现门户不是 `w.seu.edu.cn`，还会按探测到的地址推导一个 eportal
> 入口再试一次。换网段时通常需要按实际情况改 `portal_urls` / `portal_login_base`。

## 为什么还要做外网校验

未认证时，任何 HTTP 请求都会被门户劫持成认证页（返回 200 + 一段门户 HTML）。
所以脚本在认证后请求 `http://www.msftconnecttest.com/connecttest.txt`，
只有拿到 `Microsoft Connect Test` 才算成功：

- 门户说成功、外网也通 → 收工
- 门户说成功、外网不通 → 继续重试；连续两次则先注销再重登

## 安装

### 方式一：下载打包好的 exe（不用装 Python，推荐）

1. 到 [Releases](https://github.com/JinYihang1011/seu-network-autologin/releases/latest) 下载
   `seu-autologin-windows-x64.zip`，解压到**一个固定目录**（例如 `D:\seu-autologin`）——
   `config.json` 和 `login.log` 都放在 exe 旁边，别放会被清理的临时目录
2. **双击 `1-首次配置并测试.cmd`**：第一次运行会问你「一卡通号 / 宽带密码 / 校园网密码 / 运营商」，
   自动生成 `config.json` 并立刻试一次认证；看到「认证完成，外网已连通」就成了
3. **双击 `2-安装开机自启.cmd`**（UAC 点「是」）：装好后开机、登录、**切换网络**都会自动认证
4. 出问题就打开同目录的 `login.log`；包里的 `使用说明.txt` 是同样步骤（写给不用命令行的人）

两个 exe 的区别：`seu-autologin.exe` 带控制台（手动运行能看到输出），`seu-autologinw.exe` 无窗口
（计划任务用，不闪黑框）。命令行等价写法：`seu-autologin.exe --once`、`seu-autologin.exe --init`。
未签名的 exe 首次运行会弹 SmartScreen「未知发布者」，点「更多信息 → 仍要运行」即可。

### 方式二：直接用 Python 脚本

```powershell
git clone https://github.com/JinYihang1011/seu-network-autologin.git
cd seu-network-autologin
copy config.example.json config.json
notepad config.json                 # 填 account / password / isp_suffix
python seu_isp_login.py --once      # 先手动跑一次，确认能认证成功
```

然后同样双击 `install_tasks.cmd`（会弹 UAC，选「是」）注册计划任务：

| 任务名 | 触发条件 | 运行身份 |
| --- | --- | --- |
| `SEU-ISP-AutoLogin-Boot` | 系统启动时（锁屏阶段） | SYSTEM / 最高权限 |
| `SEU-ISP-AutoLogin` | 用户登录后立即 + 每 15 分钟 + **网络变化后约 5 秒** | 当前用户 / 普通权限 |

> Windows 的「快速启动」开启时，「关机再开机」其实是从休眠恢复，不会触发开机触发器，
> 这时由登录任务接上；真正的「重启」才走 Boot 任务。两个任务合起来覆盖各种情况。

## 验证（不用重启）

```powershell
python logout.py                 # 调用门户注销接口，本机立刻断网
python seu_isp_login.py --once   # 跑一次认证
```

成功时 `login.log` 长这样（注意里面会写明识别到的网络与服务类型）：

```
第 1 次-1：提交认证（PORTAL 协议（eportal，本机 IP 10.210.x.x，网络 SEU-ISP，账号 123456789@cmcc））→ 门户返回 认证成功｜dr1003({"result":"1","msg":"认证成功"})
    外网校验通过（HTTP 200，内容匹配）
第 1 次：认证完成，外网已连通
```

## 配置项

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `account` | — | **一卡通号**（登录校园网时输的那个号，不是学号） |
| `password` | — | 默认密码（被 profile 覆盖）；校园网和宽带的密码通常不同 |
| `isp_suffix` | `@cmcc` | 默认运营商后缀 |
| `profiles` | SEU-ISP→`@cmcc`、SEU-WLAN→`@xyw` | 按 SSID 的配置，可各自写 `account` / `password` / `isp_suffix` |
| `login_mode` | `auto` | `auto` = 先 PORTAL 协议，失败再回退本地认证；也可强制 `portal` / `drcom` |
| `portal_login_base` | `https://w.seu.edu.cn:801/eportal/` | 登录 / 注销接口地址 |
| `max_wait_seconds` | `300` | 单次运行最多等多久（开机时网络可能起得慢） |
| `fast_retry_seconds` / `fast_retry_window_seconds` | `2` / `120` | 前 2 分钟每 2 秒探测一次门户 |
| `retry_interval_seconds` | `10` | 2 分钟之后的重试间隔 |
| `wifi_nudge` | `true` | 完全没有网络时主动执行 `netsh wlan connect` 催 Windows 连网（按 profiles 顺序试） |
| `wifi_interface` | `WLAN` | 上面那条用的网卡名 |
| `log_file` / `log_max_kb` | `login.log` / `512` | 日志文件，超限自动截断保留后半段 |

## 目录结构

```
seu_isp_login.py        主脚本（Python 3 标准库，无第三方依赖）
config.example.json     配置模板，复制成 config.json 后填自己的账号密码
1-首次配置并测试.cmd    首次运行向导 + 试认证（双击即可）
2-安装开机自启.cmd      注册计划任务（双击，会自动提权）
使用说明.txt            给不用命令行的人看的三步说明
logout.py               把本机踢下线，用来测试自动登录
install_tasks.ps1       注册两个计划任务（需管理员）
install_tasks.cmd       双击即可（自动提权）
build_exe.ps1           用 PyInstaller 自己打包 exe
LICENSE                 MIT
```

## 排查

看 `login.log`：

| 日志 | 含义 |
| --- | --- |
| `认证完成，外网已连通` | 正常 |
| `网络 未识别` | 没读到当前 SSID，会用顶层默认后缀；确认 `profiles` 里的 SSID 拼写与系统一致 |
| `门户返回 认证成功` 但 `外网校验未通过` | 认证没真正生效，脚本会继续重试 |
| `认证被拒绝` | 账号、密码或运营商后缀不对（校园网密码可能与宽带密码不同） |
| `未发现校园网认证门户` | 请求到不了门户：确认连着校园网；开着 Clash 的 TUN 模式会劫持去门户的路由，需要关掉 TUN 或让 Clash 晚于认证启动 |
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
- 只在东南大学（SEU）网络环境下验证过；其他学校的 Drcom 门户接口可能不同，
  但接口地址、参数、运营商后缀都可以在 `config.json` 里改。
- 仅供学习和个人便利使用，请遵守学校的网络管理规定。
- 本项目以 MIT 协议开源，见 [LICENSE](LICENSE)。

## 相关项目

动手前后看过这些同类仓库（截至 2026-09 的 GitHub 搜索结果），各有侧重：

| 项目 | 说明 |
| --- | --- |
| [NN708/seu-wlan-login](https://github.com/NN708/seu-wlan-login) | 最流行的 SEU-WLAN 一键登录（Python + iOS 快捷指令），面向一次性登录 |
| [ChenYuWuAi/seu-autologin](https://github.com/ChenYuWuAi/seu-autologin) | Linux（Ubuntu + systemd）版自动登录 |
| [geniezi/SEU-WLAN-LOGIN](https://github.com/geniezi/SEU-WLAN-LOGIN) | OpenWrt 路由器版；用的是同一套 eportal `?c=Portal&a=login` 接口 |
| [PC-DOS/SEU-WLAN-AutoLogin](https://github.com/PC-DOS/SEU-WLAN-AutoLogin) | Windows GUI 工具（VB.NET），同样支持 SEU-WLAN / SEU-ISP |
| [Flynn-woo/seu-campus-autologin-windows](https://github.com/Flynn-woo/seu-campus-autologin-windows) | Windows 开机自启，工程化完整（安装器/CI/Release），但只适配 `10.9.10.100` 门户 |
| [YunfanGoForIt/SEU-OpenWrt-Gateway](https://github.com/YunfanGoForIt/SEU-OpenWrt-Gateway) | OpenWrt 网关方案，含 portal 自动登录与 OpenClash 共存经验 |

本仓库的定位：**Windows 原生 + 双网络按 SSID 自动切换 + Python 标准库零依赖 +
开机（SYSTEM）/登录/每 15 分钟三重触发 + 认证后真实外网校验**。
