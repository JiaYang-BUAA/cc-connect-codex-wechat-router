# Codex Pinned WeChat Notifier

[简体中文](README.md)

[![Tests](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml/badge.svg)](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4.svg)](https://www.microsoft.com/windows)

Windows companion service for `cc-connect` and Codex Desktop. It sends final
answers from currently pinned Codex Desktop tasks to an existing Weixin
session, then routes quoted replies back to the matching task.

This repository does not contain credentials, Codex databases, transcripts, or
the upstream cc-connect source. End-user GitHub Releases include a verified
custom `cc-connect.exe`; its Go source remains in the author's `quote-router`
fork branch, and every bundle records the pinned source commit.

> Current stable pair: notifier `1.2.1` and cc-connect routing patch
> `v1.4.1+qr15`.

## Features

- Pushes final answers from individually pinned, unarchived Desktop user tasks.
- Recognizes individually pinned Codex automations, pushes new scheduled run
  results, and routes quoted replies to the automation's target task.
- `/rwfolder` optionally includes every task inside pinned Desktop projects.
- `/rw` lists pinned tasks in current sidebar order and runtime status.
- `/rw3 内容` routes to pinned task 3; `/rw3 /y 内容` submits directly.
- Quoted normal replies queue while a task is active.
- Ordinary queued replies are written to Codex Desktop's native queue above
  the composer, where they can be edited, reordered, or removed; the notifier
  queue is retained as a fallback when the native transport is unavailable.
- Quoting a queue acknowledgement and replying `/y` directly submits the
  original queued message.
- `/rwpush` toggles final-answer push notifications.
- `/hp` shows an in-Weixin, beginner-friendly usage guide.
- Quoted Weixin voice messages use Weixin's recognized text.
- Durable queues, retry backoff, duplicate suppression, loopback-only routing,
  and health/self-test endpoints.

## Requirements

- Windows 10/11, PowerShell 7 recommended.
- Python 3.11 or newer. Runtime code uses only the standard library.
- Codex Desktop with its local task database and CDP endpoint enabled.
- First-time setup can log into Weixin by QR code; no separate cc-connect
  download or build is required.

## Install

End users only need this repository; they do not need to visit the cc-connect
fork:

1. Download the `windows-x64.zip` and matching `.sha256` files from this
   repository's [latest Release](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/latest).
2. Verify and extract the archive:

   ```powershell
   $zip = Get-Item .\cc-connect-codex-wechat-router-*-windows-x64.zip
   $expected = ((Get-Content "$($zip.FullName).sha256") -split '\s+')[0]
   (Get-FileHash $zip.FullName -Algorithm SHA256).Hash.ToLower() -eq $expected
   Expand-Archive $zip.FullName -DestinationPath .\cc-connect-router
   Set-Location .\cc-connect-router
   ```

   The checksum result must be `True`.

3. Enter the extracted directory and run the guided installer:

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
   ```

The installer verifies and installs the bundled cc-connect binary, offers QR
login for new Weixin users, discovers Codex/Python/database paths, generates a
shared router token, backs up existing configuration, and registers both
scheduled tasks. If multiple Weixin projects exist, rerun with
`-CcProject 'project-name'`.

The first run establishes a file-offset baseline and does not resend historical
answers. Advanced users can still use `config.example.json`, `install.ps1`, and
the source-build commands below.

After installation, send `/rw` in Weixin. A pinned-task list confirms that the
notifier, local router, and Weixin response path are connected.

## Weixin Commands

```text
/rw                         Show pinned task order and status
/rw3 内容                   Queue or submit content to pinned task 3
/rw3 /y 内容                Directly submit content to pinned task 3
/rwpush                    Toggle final-answer push notifications
/rwfolder                  Toggle replies from tasks in pinned projects
/hp                        Show the detailed usage guide
```

## Usage Guide

### First-time setup

1. Sign in to Codex Desktop and pin the regular tasks or automations you want
   to operate from Weixin.
2. Make sure both `cc-connect` and this notifier are running. Use
   `notifier.py --selftest` to validate configuration and the loopback router.
3. Send `/rw` in Weixin. The notifier lists pinned tasks in the current Codex
   Desktop sidebar order, including task number, runtime status, and elapsed
   time.
4. Use the displayed number with `/rw<number> content` to send a new message
   to a specific pinned task.

### Check task status

`/rw` shows every currently pinned task, including individually pinned
   automations. A running task shows its processing
   time; an idle task shows `空闲`. If no tasks are pinned, the response says so.
   Numbers follow the current pinned order and can change when tasks are
   unpinned or archived.

### Reply to a final answer

When a notification beginning with `【聊天名称】` arrives, quote the complete
   notification and send your reply. Idle tasks accept it immediately. Replies
   to active tasks are queued by default, and Weixin reports how many messages
   are ahead in the queue.

By default, the message is inserted into the target task's native Codex
Desktop queued-follow-up list above the composer. Desktop submits it in order,
and you can edit, reorder, or remove it from the Desktop UI. The notifier keeps
only a tracking record so quoting the queue acknowledgement and replying `/y`
can promote that exact item. If the native Desktop transport is unavailable,
the durable notifier queue is used automatically as a fallback.

The acknowledgement also lists the content of every currently queued message
for that task in execution order, including the newly submitted item. Multiline
content is collapsed onto one line. Very long items or unusually large queues
are shortened only in the Weixin preview; the original Desktop queue is not
modified.

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

Send `/rwfolder` to independently include or exclude tasks inside pinned Codex
Desktop projects. It is off by default. When enabled, an unarchived task in a
pinned project is pushed even if that task is not individually pinned, and the
notification can still be quoted to continue the exact task. `/rw` numbering
continues to list only individually pinned tasks so large projects do not fill
the numbered command list.

Individually pinned automations do not depend on `/rwfolder`; like other
individually pinned tasks, they follow the `/rwpush` master switch. Existing
scheduled run history is baselined and is not replayed. Codex may create and
archive a separate execution task for each scheduled run; its new final answer
is associated with the pinned automation target so quoting the notification or
using `/rw<number> content` continues the target task.

When a quoted reply is sent to a final answer, ordinary text queues if the task
is processing. Prefix new content with `/y` for direct submission. Queue
acknowledgements say:

```text
收到，已提交【聊天名称】，排队中（前方x条）。
引用这条提示回复"/y"直接提交本条消息。

当前队列：
1.第一条排队消息
2.第二条排队消息
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

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change and
[CHANGELOG.md](CHANGELOG.md) for version history.

## Custom cc-connect Build

The Go changes live in the companion fork's `quote-router` branch, based on
upstream `v1.4.1`. Build and deploy from that fork with explicit paths:

```powershell
pwsh -NoProfile -File .\build-quote-router.ps1 `
  -SourceRoot 'C:\src\cc-connect' `
  -OutputRoot (Join-Path $PWD 'artifacts') `
  -PatchVersion 15
pwsh -NoProfile -File .\deploy-quote-router.ps1 `
  -SourceRoot (Join-Path $PWD 'artifacts') `
  -NotifierConfig (Join-Path $PWD 'config.json') `
  -PatchVersion 15
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

## Troubleshooting

- **`/rw` reports no pinned tasks:** pin at least one unarchived task in Codex
  Desktop. Tasks created only inside cc-connect are not part of this list.
- **Commands receive no response:** verify the `cc-connect` and
  `Codex Pinned WeChat Notifier` scheduled tasks, run `--selftest`, then inspect
  the notifier and daemon logs.
- **A quote cannot be routed:** quote the complete final-answer notification,
  or use `/rw<number> content` as a fallback.
- **Different spacing on mobile and desktop:** Weixin clients collapse blank
  lines differently. The notifier optimizes the notification for mobile.

When reporting a problem, remove router tokens, full user IDs, answer bodies,
database contents, and machine-specific paths from logs and screenshots.

## Publishing

Before the first public push:

1. Commit only source, tests, scripts, documentation, and the example config.
2. Run the test and public-repository checks on a clean clone.
3. Publish the cc-connect fork separately, preserving its upstream notices and
   clearly describing the custom commits.

See [SECURITY.md](SECURITY.md) and [NOTICE.md](NOTICE.md) for handling and
upstream attribution guidance.
