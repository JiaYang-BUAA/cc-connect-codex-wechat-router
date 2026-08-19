# Codex 置顶任务微信通知器

[English](README.en.md)

[![Tests](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml/badge.svg)](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4.svg)](https://www.microsoft.com/windows)

这是 `cc-connect` 与 Codex Desktop 的 Windows 本机伴随服务。它把置顶
Codex 任务的最终答复推送到既有微信会话，并把微信的引用回复路由回原任务。

本仓库不包含任何令牌、数据库、任务记录、微信会话或 cc-connect 上游源码。
面向普通用户的 GitHub Release 已包含经过校验的定制 `cc-connect.exe`；其 Go
源码仍维护在作者 fork 的 `quote-router` 分支，并在每个发行包中记录固定提交。

> 当前稳定版本：通知器 `1.2.1`，配套 cc-connect 路由补丁 `v1.4.1+qr15`。

## 功能

- 推送单独置顶、未归档的 Codex Desktop 用户任务最终答复。
- 识别单独置顶的 Codex 定时任务；新运行结果完成后推送，引用回复会回到该定时任务的目标对话。
- `/rwfolder` 可额外开关置顶文件夹内全部对话的最终答复推送。
- `/rw` 显示当前置顶顺序、运行时长和排队数。
- `/rw3 内容` 投递到当前第 3 个置顶任务；`/rw3 /y 内容` 直接提交。
- 引用最终答复时，任务运行中默认排队；引用排队提示回复 `/y` 可直接提交原消息。
- 普通排队消息会写入 Codex Desktop 原生排队列表，显示在输入框上方；可在
  Desktop 中编辑、排序或删除，通知器队列只在原生通道不可用时回退使用。
- `/rwpush` 开关最终答复推送；引用语音使用微信识别出的文本。
- `/hp` 在微信内显示面向新手的完整操作指南。
- 持久化队列、退避重试、去重、仅回环 HTTP 路由和健康检查。

## 运行要求

- Windows 10 或 Windows 11。
- Python 3.11 或更高版本；通知器运行时只使用 Python 标准库。
- 已登录的 Codex Desktop，且本机任务数据库和 CDP 调试端点可用。
- 首次安装时可扫码登录微信；不需要另外下载或构建 cc-connect。
- PowerShell 7 推荐用于安装、构建和诊断脚本。

## 快速开始

普通用户只需要访问本仓库，不需要打开 cc-connect Fork：

1. 在本仓库的 [Releases](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/latest)
   下载 `windows-x64.zip` 和同名 `.sha256` 文件。
2. 校验 ZIP 后解压：

   ```powershell
   $zip = Get-Item .\cc-connect-codex-wechat-router-*-windows-x64.zip
   $expected = ((Get-Content "$($zip.FullName).sha256") -split '\s+')[0]
   (Get-FileHash $zip.FullName -Algorithm SHA256).Hash.ToLower() -eq $expected
   Expand-Archive $zip.FullName -DestinationPath .\cc-connect-router
   Set-Location .\cc-connect-router
   ```

   校验结果必须为 `True`。

3. 进入解压后的目录并运行引导安装器：

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
   ```

安装器会自动完成以下操作：

- 校验并安装发行包内固定版本的 `cc-connect.exe`；
- 首次使用时显示微信二维码，扫码完成机器人登录；
- 自动发现 Codex、Python 和本地任务数据库；
- 为 cc-connect 和通知器写入同一个随机路由令牌；
- 修改配置前建立时间戳备份；
- 注册并启动 `cc-connect` 与 `Codex Pinned WeChat Notifier` 计划任务。

如果 cc-connect 配置里有多个微信项目，使用项目名重新运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -CcProject '项目名'
```

首次运行只建立偏移基线，不补发历史消息。高级用户仍可使用
`config.example.json`、`install.ps1` 和文末的源码构建命令进行手动安装。

安装完成后，在微信发送 `/rw`。能看到置顶任务列表即表示状态路由、微信回复和通知器均已连通。

## 微信命令

```text
/rw                         查看置顶任务
/rw3 内容                   向第 3 个置顶任务提交内容
/rw3 /y 内容                向第 3 个置顶任务直接提交内容
/rwpush                     开关最终答复推送
/rwfolder                   开关置顶文件夹内对话的答复推送
/hp                         查看详细使用指南
```

## 使用指南

### 首次使用

1. 确认 Codex Desktop 已登录，并已经置顶需要通过微信操作的普通任务或定时任务。
2. 确认 `cc-connect` 和本通知器都已启动；可运行 `notifier.py --selftest` 检查配置和本地路由。
3. 在微信中发送 `/rw`。通知器会按 Codex Desktop 当前侧边栏的置顶顺序列出任务编号、运行状态和运行时长。
4. 记住任务编号后，可以用 `/rw编号 内容` 直接向该任务发送新消息。

### 查看状态

发送 `/rw` 可以查看所有置顶任务，包括单独置顶的定时任务。运行中的任务显示当前处理时间，未运行的任务显示“空闲”；没有置顶任务时会明确提示当前没有置顶任务。编号按当前置顶顺序计算，任务取消置顶或归档后，编号可能变化。

### 回复 Codex 最终答复

收到形如“【聊天名称】”的最终答复通知后，直接引用整条通知并发送文字即可。任务空闲时会立即提交；任务正在处理时，消息默认进入队列，微信会返回前方排队数量。

默认排队时，消息会直接出现在对应 Codex Desktop 任务输入框上方的原生排队列表中，之后由 Desktop 自己按顺序提交。你可以在 Desktop 中修改、调整顺序或删除这条消息；微信端仍会保留一条跟踪记录，用于识别“引用排队提示回复 `/y`”的操作。若 Desktop 原生队列暂时不可用，通知器会自动使用自己的持久化回退队列。

排队确认会在“当前队列”下面按执行顺序列出该任务每条排队消息的内容，包括本次刚提交的消息。多行内容会折叠为一行；内容过长或队列过多时会截断显示，但不修改 Codex Desktop 中保存的原消息。

如果需要跳过队列，在消息前加 `/y`，例如 `/y 请优先检查这个错误`。直接提交失败时，系统会自动退回队列，不会丢失消息。

排队确认消息也可以被引用，引用后只发送 `/y`，即可把原来排队的那条消息改为直接提交。引用内容必须是完整的通知或排队确认；无法识别时，系统会提示重新引用最近一次完整 Codex 答复。

### 按编号继续对话

当历史通知不方便查找时，使用 `/rw编号 内容`，例如：

```text
/rw3 请根据上一版结果继续分析
```

这会把消息发送到当前第 3 个置顶任务。需要直接提交时使用 `/rw3 /y 内容`。如果编号对应的任务已取消置顶或归档，系统会提示无法继续回复。

### 控制通知推送

发送 `/rwpush` 可切换最终答复推送开关。微信会返回“置顶任务回复推送已开启”或“置顶任务回复推送已关闭”。关闭推送只影响最终答复通知，不会停止 Codex 任务、清空队列或禁用微信提交。

发送 `/rwfolder` 可单独开启或关闭置顶文件夹内对话的最终答复推送。此开关默认关闭；开启后，只要对话属于当前置顶文件夹，即使对话本身没有单独置顶，完成后也会推送。通知可以照常引用并回复到原对话。`/rw` 的编号列表仍只包含单独置顶的对话，以免文件夹内大量历史对话占用编号。

单独置顶的定时任务不依赖 `/rwfolder`，与普通单独置顶任务一样受 `/rwpush` 总开关控制。通知器只推送启用该功能后新完成的定时运行，不补发历史运行结果。Codex 会为一次定时运行创建临时运行任务并自动归档；通知器会把结果关联到置顶的定时任务目标，因此引用通知或使用 `/rw编号 内容` 时仍能继续对应目标对话。

## 工作原理

```text
Codex Desktop 数据库和任务记录
             |
         notifier.py ---- 本机 HTTP 路由（仅 127.0.0.1）
             |                         |
       最终答复推送              微信引用和命令
             |                         |
             +------ cc-connect ------+
```

通知器从 Codex Desktop 的全局状态读取当前置顶顺序、置顶文件夹及对话归属。编号是实时顺序，不是任务的永久 ID。路由服务只监听回环地址，答复正文和访问令牌不会写入日志。

## 常见问题

### `/rw` 显示没有置顶任务

先在 Codex Desktop 侧边栏置顶至少一个未归档普通任务或定时任务，然后等待数秒再试。置顶顺序来自 Desktop 本地状态，不读取 cc-connect 自己创建的会话。

### 微信命令没有反应

确认两个计划任务都在运行：

```powershell
Get-ScheduledTask -TaskName 'cc-connect','Codex Pinned WeChat Notifier' |
  Select-Object TaskName, State
```

随后运行自检，并检查 `logs/notifier.log` 与 `%USERPROFILE%\.cc-connect\logs\daemon.log`。不要在 Issue 中粘贴令牌、完整用户 ID、对话正文或本机数据库。

### 引用后无法路由到原对话

请引用完整的 Codex 最终答复通知。转发、手动复制或截断后的文本可能缺少路由指纹；这种情况下使用 `/rw编号 内容` 继续对话。

### 手机和电脑微信换行不同

微信不同客户端会压缩空行。通知器优先保证手机端可读，电脑端可能显示为较紧凑的段落，不影响路由。

任务运行中排队时，提示为：

```text
收到，已提交【聊天名称】，排队中（前方x条）。
引用这条提示回复"/y"直接提交本条消息。

当前队列：
1.第一条排队消息
2.第二条排队消息
```

## 开发与发布

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File .\tools\check-public-repo.ps1
```

创建 `v*` 标签后，Release 工作流会从固定的 cc-connect Fork 提交构建 Windows
二进制、运行两边测试、生成组合 ZIP 与 SHA-256，并发布到本仓库 Releases。

`config.json`、`data/`、`logs/`、构建产物和旧本机部署脚本已由 `.gitignore`
排除。公开发布前仍应检查 `git status --ignored`，不要提交真实令牌或路径。

详见 [README.en.md](README.en.md)、[SECURITY.md](SECURITY.md) 和 [NOTICE.md](NOTICE.md)。
提交改动前请同时阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；版本变化见 [CHANGELOG.md](CHANGELOG.md)。
