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
```

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
