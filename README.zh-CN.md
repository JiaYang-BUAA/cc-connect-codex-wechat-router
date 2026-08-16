# Codex 置顶任务微信通知器

[English](README.md)

这是 `cc-connect` 与 Codex Desktop 的 Windows 本机伴随服务。它把置顶
Codex 任务的最终答复推送到既有微信会话，并把微信的引用回复路由回原任务。

本仓库不包含任何令牌、数据库、任务记录、微信会话或 cc-connect 上游源码。
cc-connect 的定制 Go 改动维护在作者 fork 的 `quote-router` 分支，基于上游
`v1.4.1`。

## 功能

- 仅推送置顶、未归档的 Codex Desktop 用户任务最终答复。
- `/rw` 显示当前置顶顺序、运行时长和排队数。
- `/rw3 内容` 投递到当前第 3 个置顶任务；`/rw3 /y 内容` 直接提交。
- 引用最终答复时，任务运行中默认排队；引用排队提示回复 `/y` 可直接提交原消息。
- `/rwpush` 开关最终答复推送；引用语音使用微信识别出的文本。
- 持久化队列、退避重试、去重、仅回环 HTTP 路由和健康检查。

## 安装

1. 将 `config.example.json` 复制为 `config.json`，填写全部占位符。
2. 生成长度至少 32 字节的随机 `router_token`，同时配置到 cc-connect 微信平台的
   `codex_quote_router_token`。
3. 先自检：

   ```powershell
   python .\notifier.py --config .\config.json --selftest
   ```

4. 注册登录后启动的计划任务：

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
     -ConfigPath (Join-Path $PWD 'config.json')
   ```

首次运行只建立偏移基线，不补发历史消息。

## 微信命令

```text
/rw                         查看置顶任务
/rw3 内容                   向第 3 个置顶任务提交内容
/rw3 /y 内容                向第 3 个置顶任务直接提交内容
/rwpush                     开关最终答复推送
/hp                         查看简要使用指南
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
