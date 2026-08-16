# Codex Pinned WeChat Notifier

[简体中文](README.zh-CN.md)

Windows companion service for `cc-connect` and Codex Desktop. It sends final
answers from currently pinned Codex Desktop tasks to an existing Weixin
session, then routes quoted replies back to the matching task.

This repository does not contain credentials, Codex databases, transcripts, or
the upstream cc-connect source. It requires a custom cc-connect build with the
routing changes from the author's `quote-router` branch.

## Features

- Pushes only final answers from pinned, unarchived Desktop user tasks.
- `/rw` lists pinned tasks in current sidebar order and runtime status.
- `/rw3 内容` routes to pinned task 3; `/rw3 /y 内容` submits directly.
- Quoted normal replies queue while a task is active.
- Quoting a queue acknowledgement and replying `/y` directly submits the
  original queued message.
- `/rwpush` toggles final-answer push notifications.
- Quoted Weixin voice messages use Weixin's recognized text.
- Durable queues, retry backoff, duplicate suppression, loopback-only routing,
  and health/self-test endpoints.

## Requirements

- Windows 10/11, PowerShell 7 recommended.
- Python 3.11 or newer. Runtime code uses only the standard library.
- Codex Desktop with its local task database and CDP endpoint enabled.
- `cc-connect` configured with the Weixin platform and matching router token.

## Install

1. Copy `config.example.json` to `config.json` and replace every placeholder.
   Generate a long random `router_token`; use the same value in cc-connect's
   `codex_quote_router_token` option.
2. Verify the configuration without starting the service:

   ```powershell
   python .\notifier.py --config .\config.json --selftest
   ```

3. Register the per-user scheduled task:

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
     -ConfigPath (Join-Path $PWD 'config.json')
   ```

The first run establishes a file-offset baseline and does not resend historical
answers. Runtime state is written to the configured `data` path.

## Weixin Commands

```text
/rw                         Show pinned task order and status
/rw3 内容                   Queue or submit content to pinned task 3
/rw3 /y 内容                Directly submit content to pinned task 3
/rwpush                    Toggle final-answer push notifications
```

## Usage Guide

### First-time setup

1. Sign in to Codex Desktop and pin the tasks you want to operate from Weixin.
2. Make sure both `cc-connect` and this notifier are running. Use
   `notifier.py --selftest` to validate configuration and the loopback router.
3. Send `/rw` in Weixin. The notifier lists pinned tasks in the current Codex
   Desktop sidebar order, including task number, runtime status, and elapsed
   time.
4. Use the displayed number with `/rw<number> content` to send a new message
   to a specific pinned task.

### Check task status

`/rw` shows every currently pinned task. A running task shows its processing
   time; an idle task shows `空闲`. If no tasks are pinned, the response says so.
   Numbers follow the current pinned order and can change when tasks are
   unpinned or archived.

### Reply to a final answer

When a notification beginning with `【聊天名称】` arrives, quote the complete
   notification and send your reply. Idle tasks accept it immediately. Replies
   to active tasks are queued by default, and Weixin reports how many messages
   are ahead in the queue.

Prefix a message with `/y` to submit it directly, for example:

```text
/y Please inspect this error first
```

If direct submission fails, the notifier automatically falls back to the
   queue so the message is not lost.

You can also quote a queue acknowledgement and send only `/y` to promote the
   original queued message to direct submission. The quoted content must be a
   complete Codex notification or queue acknowledgement. Unrecognized quotes
   receive a prompt to quote the latest complete answer again.

### Continue by pinned-task number

Use `/rw<number> content` when the older notification is difficult to find:

```text
/rw3 Continue the analysis using the previous result
```

This routes the message to the current third pinned task. Use `/rw3 /y content`
   for direct submission. If the task is no longer pinned or has been archived,
   the notifier reports that it cannot continue the conversation.

### Control final-answer notifications

Send `/rwpush` to toggle final-answer push notifications. The response is
   either “置顶任务回复推送已开启” or “置顶任务回复推送已关闭”. This toggle
   affects notifications only; it does not stop Codex tasks, clear queues, or
   disable Weixin submissions.

When a quoted reply is sent to a final answer, ordinary text queues if the task
is processing. Prefix new content with `/y` for direct submission. Queue
acknowledgements say:

```text
收到，已提交【聊天名称】，排队中（前方x条）。
引用这条提示回复"/y"直接提交本条消息。
```

## Development

Run the complete test suite:

```powershell
python -m unittest discover -s tests -v
```

Run the publication safety check:

```powershell
pwsh -NoProfile -File .\tools\check-public-repo.ps1
```

The GitHub Actions workflow runs both checks on Windows. Keep local
`config.json`, `data/`, and `logs/` untracked.

## Custom cc-connect Build

The Go changes live in the companion fork's `quote-router` branch, based on
upstream `v1.4.1`. Build and deploy from that fork with explicit paths:

```powershell
pwsh -NoProfile -File .\build-quote-router.ps1 `
  -SourceRoot 'C:\src\cc-connect' `
  -OutputRoot (Join-Path $PWD 'artifacts') `
  -PatchVersion 1
pwsh -NoProfile -File .\deploy-quote-router.ps1 `
  -SourceRoot (Join-Path $PWD 'artifacts') `
  -NotifierConfig (Join-Path $PWD 'config.json') `
  -PatchVersion 1
```

Deployment verifies executable version, SHA-256, loopback health, scheduled
tasks, and new daemon log output. It keeps a timestamped backup and restores it
if verification fails.

## Architecture

```text
Codex Desktop DB/rollouts
          |
      notifier.py ---- loopback HTTP /status /task /route /toggle
          |                                      |
   Weixin send via cc-connect <--- custom cc-connect Weixin router
```

The notifier never exposes the router outside loopback and does not log answer
bodies or credentials. The Desktop pinned order is read from
`.codex-global-state.json`; numbering is current-state numbering, not a
permanent task identifier.

## Publishing

Before the first public push:

1. Commit only source, tests, scripts, documentation, and the example config.
2. Run the test and public-repository checks on a clean clone.
3. Publish the cc-connect fork separately, preserving its upstream notices and
   clearly describing the custom commits.

See [SECURITY.md](SECURITY.md) and [NOTICE.md](NOTICE.md) for handling and
upstream attribution guidance.
