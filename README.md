# Codex 置顶任务微信通知器

[English](README.md)

[![Tests](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml/badge.svg)](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4.svg)](https://www.microsoft.com/windows)

这是 `cc-connect` 与 Codex Desktop 的 Windows 本机伴随服务。它把置顶
Codex 任务的最终答复推送到既有微信会话，并把微信的引用回复路由回原任务。

本仓库不包含任何令牌、数据库、任务记录、微信会话或 cc-connect 上游源码。
cc-connect 的定制 Go 改动维护在作者 fork 的 `quote-router` 分支，基于上游
`v1.4.1`。

> 当前稳定版本：通知器 `1.1.0`，配套 cc-connect 路由补丁 `v1.4.1+qr3`。

## 功能

- 仅推送置顶、未归档的 Codex Desktop 用户任务最终答复。
- `/rw` 显示当前置顶顺序、运行时长和排队数。
- `/rw3 内容` 投递到当前第 3 个置顶任务；`/rw3 /y 内容` 直接提交。
- 引用最终答复时，任务运行中默认排队；引用排队提示回复 `/y` 可直接提交原消息。
- `/rwpush` 开关最终答复推送；引用语音使用微信识别出的文本。
- `/hp` 在微信内显示面向新手的完整操作指南。
- 持久化队列、退避重试、去重、仅回环 HTTP 路由和健康检查。

## 运行要求

- Windows 10 或 Windows 11。
- Python 3.11 或更高版本；通知器运行时只使用 Python 标准库。
- 已登录的 Codex Desktop，且本机任务数据库和 CDP 调试端点可用。
- 已配置微信平台的 `cc-connect`，并使用配套 `quote-router` 构建。
- PowerShell 7 推荐用于安装、构建和诊断脚本。

## 快速开始

1. 将 `config.example.json` 复制为 `config.json`，填写全部占位符。
2. 生成长度至少 32 字节的随机 `router_token`，同时配置到 cc-connect 微信平台的
   `codex_quote_router_token`。
3. 将 cc-connect 的 `codex_quote_router_url` 设置为
   `http://127.0.0.1:18765/route`，并确认两处令牌完全一致。
4. 先自检：

   ```powershell
   python .\notifier.py --config .\config.json --selftest
   ```

5. 注册登录后启动的计划任务：

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
     -ConfigPath (Join-Path $PWD 'config.json')
   ```

首次运行只建立偏移基线，不补发历史消息。

安装完成后，在微信发送 `/rw`。能看到置顶任务列表即表示状态路由、微信回复和通知器均已连通。

## 微信命令

```text
/rw                         查看置顶任务
/rw3 内容                   向第 3 个置顶任务提交内容
/rw3 /y 内容                向第 3 个置顶任务直接提交内容
/rwpush                     开关最终答复推送
/hp                         查看详细使用指南
```

## 使用指南

### 首次使用

1. 确认 Codex Desktop 已登录，并已经置顶需要通过微信操作的任务。
2. 确认 `cc-connect` 和本通知器都已启动；可运行 `notifier.py --selftest` 检查配置和本地路由。
3. 在微信中发送 `/rw`。通知器会按 Codex Desktop 当前侧边栏的置顶顺序列出任务编号、运行状态和运行时长。
4. 记住任务编号后，可以用 `/rw编号 内容` 直接向该任务发送新消息。

### 查看状态

发送 `/rw` 可以查看所有置顶任务。运行中的任务显示当前处理时间，未运行的任务显示“空闲”；没有置顶任务时会明确提示当前没有置顶任务。编号按当前置顶顺序计算，任务取消置顶或归档后，编号可能变化。

### 回复 Codex 最终答复

收到形如“【聊天名称】”的最终答复通知后，直接引用整条通知并发送文字即可。任务空闲时会立即提交；任务正在处理时，消息默认进入队列，微信会返回前方排队数量。

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

通知器从 Codex Desktop 的全局状态读取当前置顶顺序。编号是实时顺序，不是任务的永久 ID。路由服务只监听回环地址，答复正文和访问令牌不会写入日志。

## 常见问题

### `/rw` 显示没有置顶任务

先在 Codex Desktop 侧边栏置顶至少一个未归档任务，然后等待数秒再试。置顶顺序来自 Desktop 本地状态，不读取 cc-connect 自己创建的会话。

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
```

## 开发与发布

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File .\tools\check-public-repo.ps1
```

`config.json`、`data/`、`logs/`、构建产物和旧本机部署脚本已由 `.gitignore`
排除。公开发布前仍应检查 `git status --ignored`，不要提交真实令牌或路径。

详见 [README.md](README.md)、[SECURITY.md](SECURITY.md) 和 [NOTICE.md](NOTICE.md)。
提交改动前请同时阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；版本变化见 [CHANGELOG.md](CHANGELOG.md)。
