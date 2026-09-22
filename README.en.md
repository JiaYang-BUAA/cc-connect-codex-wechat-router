# Codex Pinned WeChat Notifier

[简体中文](README.md)

[![Tests](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml/badge.svg)](https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4.svg)](https://www.microsoft.com/windows)

Windows companion service for `cc-connect` and Codex Desktop. It sends final
answers from pinned tasks or the 10 most recently active tasks to an existing
Weixin session, then routes quoted replies back to the matching task.

This repository does not contain credentials, Codex databases, transcripts, or
the upstream cc-connect source. End-user GitHub Releases include a verified
custom `cc-connect.exe`; its Go source remains in the author's `quote-router`
fork branch, and every bundle records the pinned source commit.

> Current stable pair: notifier `1.4.0` and cc-connect routing patch
> `v1.4.1+qr16`.

## Features

- `/rwmode` switches between pinned tasks (the default) and the 10 most
  recently active unarchived user tasks, including idle tasks.
- Pinned mode retains individually pinned tasks, pinned automations, and the
  optional `/rwfolder` inclusion of tasks inside pinned Desktop projects.
- Recognizes new Codex automation results and routes quoted replies to the
  target task; temporary execution tasks do not occupy separate recent slots.
- `/rw` shows remaining 5-hour and 7-day quota with reset times, then the
  current mode's task list, runtime status, and queue counts.
- A Weixin alert is sent once when either quota first falls to 10% or below, and once more if it reaches 0%; alerts re-arm after recovery.
- `/rw3 内容` routes to item 3 in the current task list; `/rw3 /y 内容` submits
  directly. Recent-mode numbers stay attached to the last returned list until
  `/rw` refreshes it.
- Quoted normal replies queue while a task is active.
- Ordinary queued replies are written to Codex Desktop's native queue above
  the composer, where they can be edited, reordered, or removed; the notifier
  queue is retained as a fallback when the native transport is unavailable.
- Quoting a queue acknowledgement and replying `/y` directly submits the
  original queued message.
- `/rwpush` is the shared final-answer push switch for both modes.
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

After installation, send `/rw` in Weixin. A reply showing quota, push mode,
and task status confirms that the notifier, local router, and Weixin response
path are connected.

> **Send `/rw` before each usage session.** This is especially important after
> a long period without interacting with the bot, before submitting work from
> Weixin, or while waiting for a final Codex answer. Wait for the status reply
> before continuing. The inbound message activates the current interactive
> context and provides a normal reactive reply opportunity, which improves later
> delivery reliability. It is not permanent keepalive and cannot guarantee that
> an existing gateway push restriction is removed immediately.

## Weixin Commands

```text
/rw                         Show the current task list and quota
/rwmode                     Switch between pinned and recent modes
/rwmode pinned              Select pinned tasks (the default)
/rwmode recent              Select the 10 most recently active tasks
/rw3 内容                   Queue or submit content to item 3 in the task list
/rw3 /y 内容                Directly submit content to item 3 in the task list
/rwpush                     Toggle final-answer push (shared by both modes)
/rwfolder                   Toggle pinned-project replies (pinned mode only)
/hp                         Show the detailed usage guide
```

## Usage Guide

### First-time setup

1. Sign in to Codex Desktop. Pinned mode is the default, so pin the regular
   tasks or automations you want to operate from Weixin. Alternatively, select
   recently active tasks later with `/rwmode recent`.
2. Make sure both `cc-connect` and this notifier are running. Use
   `notifier.py --selftest` to validate configuration and the loopback router.
3. At the start of each usage session, or after a long idle period, send `/rw`
   in Weixin to activate the current interactive context. The notifier shows
   the remaining 5-hour and 7-day Codex quota and reset times, then the current
   mode's task list, including task number, runtime status, and elapsed time.
4. Use the displayed number with `/rw<number> content` to send a new message
   to a specific task in that list.

### Check task status

`/rw` shows the remaining 5-hour and 7-day quota, reset times, push mode, and
current task list. A running task shows its processing time; an idle task
shows `空闲`. If no tasks qualify, the reply still shows quota and switches
and explains that the list is empty. If quota reading is temporarily
unavailable, task status is still returned normally.

Pinned mode follows the current Codex Desktop sidebar order, including
individually pinned automations; numbers can change when tasks are unpinned
or archived. Recent mode sorts by the latest conversation activity and saves
the returned list so new activity does not change numbers you just received.

### Choose a push mode

Send `/rwmode` to switch between modes, or select one explicitly:

- `/rwmode pinned`: the default. Push final answers from individually pinned,
  unarchived tasks and, when `/rwfolder` is enabled, tasks in pinned projects.
- `/rwmode recent`: select up to 10 unarchived user tasks, newest first by
  the conversation activity time recorded by Codex. Both user messages and
  task replies count as activity, and idle tasks are included. This means
  10 tasks, not 10 messages or only tasks that are currently running.

Subagent tasks are excluded. Temporary automation executions are associated
with their target task instead of occupying an additional slot.

A mode change returns the new task list. The recent-mode **push selection
updates dynamically**, but `/rw<number> content` uses the last list returned
by `/rw` or a mode change. Send `/rw` again to refresh its numbers. For example,
if item 3 was “Research,” `/rw3 content` continues to reach “Research” even
when another task becomes active, until you refresh the list.

Both modes share `/rwpush`. Changing modes leaves that switch unchanged and
does not replay historical answers from newly selected tasks. Previously
accepted instructions, queued replies, and notification backlogs are retained
and continue to be processed.

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

In either mode, a notification with a saved routing record still reaches its
original task after a mode change, unpinning, or leaving the recent top 10.
Archived, deleted, or unavailable tasks remain blocked, and unknown quotes
never guess a target. Push mode controls automatic notifications, not the
target of an old quote: submissions outside the current scope are accepted,
but their final answers are still subject to the current notification scope.

### Continue by task number

An “已提交” (submitted) receipt means Desktop confirmed acceptance or native
queue insertion, not that execution finished. If transfer fails, the receipt
instead says the message is saved and waiting to be forwarded. Do not resend:
the notifier retains it for retry. Ambiguous submissions, such as a lost CDP
response, are checked against the native queue and task history first.

The transport supports newer Desktop server-side queues and legacy local
queues separately; it does not overwrite a server-side queue with local state.
Explicit queue submission is independent of Desktop's default send mode.
Desktop internals may still change across releases; keep pending messages and
check the logs when an integration error occurs.

Use `/rw<number> content` when the older notification is difficult to find:

```text
/rw3 Continue the analysis using the previous result
```

This routes the message to item 3 in the current task list. Use `/rw3 /y content`
for direct submission. Pinned mode uses the current pinned order. Recent mode
uses the last list returned by `/rw` or a mode change: a task leaving the top
10 does not silently assign its number to another task. Archived tasks, or
tasks outside the permitted pinned scope while in pinned mode, cannot receive
new submissions.

### Control final-answer notifications

Send `/rwpush` to toggle the shared final-answer push switch. The response
reports whether push is enabled for the current mode. Changing modes does not
automatically enable or disable push. This toggle affects notifications only;
it does not stop Codex tasks, clear queues, or disable Weixin submissions.

In pinned mode, send `/rwfolder` to independently include or exclude tasks
inside pinned Codex Desktop projects. It is off by default and follows the
`/rwpush` master switch. When enabled, an unarchived task in a pinned project
is pushed even if that task is not individually pinned, and the notification
can still be quoted to continue the exact task. Pinned-mode `/rw` numbering
continues to list only individually pinned tasks so large projects do not
fill the numbered command list.

Recent mode does not use the pinned-project switch. Sending `/rwfolder` in
recent mode only explains that it applies to pinned mode; it does not change
the saved setting. Switching back to pinned mode restores its prior effect.

In pinned mode, individually pinned automations do not depend on `/rwfolder`;
like other individually pinned tasks, they follow the `/rwpush` master switch.
Only newly completed runs within the current push selection are notified;
scheduled run history is not replayed. Codex may create and archive a separate
execution task for each scheduled run. Its new final answer is associated
with the automation target so quoting the notification, or using its number
in the current task list, continues the target task.

### Weixin interaction activation, proactive-push limits, and backlog summaries

`context_token` is a temporary reply credential issued by the Weixin gateway
with an inbound user message. cc-connect includes the current conversation token
when sending. It is not a Weixin login
credential, a Codex API key, or a Codex usage-limit token, and its expiry does not
stop the underlying Codex task.

When the user messages the bot, the gateway may issue a fresh `context_token`,
and that inbound message also permits a normal reactive reply. cc-connect caches
the token for later use, but persistence does not extend server-side validity.
Continuous `getUpdates` long polling only receives messages; it does not refresh
the interactive context or proactive-push budget. The gateway publishes neither
a renewal endpoint nor a fixed token lifetime or proactive-push allowance.

Community reports, including user reports on Xiaohongshu, indicate that after an
inbound user message activates an interaction, the bot may proactively send at
most approximately **10 messages** during that interaction. This is not a fixed
allowance guaranteed by Tencent's public API documentation, and an account may
be restricted before reaching 10 messages depending on account state, send rate,
timing, and gateway risk controls. This project documents “up to approximately
10” as a risk warning, not as a dependable delivery guarantee.

**Before controlling Codex from Weixin, send `/rw` and wait for its status
reply.** Send it again after a long idle period. This activates a new interaction
and improves the likelihood of subsequent notifications, but it is not permanent
keepalive and cannot guarantee immediate removal of an existing server-side
restriction. Do not send scheduled bot heartbeats as keepalive: they are
proactive pushes themselves and can consume the limited push budget faster.

Typical symptoms are a completed Codex task with no Weixin notification and a
cc-connect or notifier log containing `ret=-2`, `prepare failed`,
`expired context_token`, or an instruction that the user must message the bot
again. `ret=-2` alone does not prove token expiry; current cc-connect upstream
treats it as a bot-wide proactive-push throttle. In either case, Weixin rejected
the send; the Codex task did not fail and its quota was not exhausted.

The notifier handles this condition as follows:

1. It stops sending individual answers so a later token refresh cannot cause a
   flood of old messages.
2. Replies completed during the outage remain recorded as a backlog, while their
   complete answer text stays in the original Codex tasks.
3. The backlog is counted by task title; no answer text is copied into the summary.
4. After the user sends the bot any new Weixin message, the gateway normally
   issues a fresh credential and the notifier immediately attempts one backlog
   summary. Delivery records are cleared only after that summary succeeds; if
   the gateway still rejects the credential, the backlog remains for a later retry.

Example:

```text
积压消息汇总

【Research】2 messages
【Daily】1 message
```

The summary means the answers are available in Codex; it does not mean they were
lost. Open the corresponding Codex task to read them, or use `/rw<number> content`
to continue a specific task from Weixin. Because one summary can cover multiple
tasks, quoting the summary cannot route a reply to one specific task.

### Quota alerts

When either the 5-hour or 7-day remaining quota first falls from above 10% to
10% or below, the notifier sends one Weixin alert. If that quota later reaches
0%, it sends one more alert. Each message includes both current percentages and
their local reset times. Repeated polls and notifier restarts do not duplicate
an alert in the same state; recovery above 10% re-arms the next warning.

Quota alerts are independent of `/rwpush` and `/rwfolder`. They use a persistent
local Codex App Server connection and poll every 60 seconds by default. Failed
Weixin delivery is retried on a later poll and is not marked as delivered. Only
percentages, reset times, and alert stages are persisted—never account tokens.

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
bodies or credentials. Pinned order and project membership come from Desktop
global state; recent activity comes from task records. Pinned mode uses the
current pinned order, while recent mode saves numbers from the last returned
list. Neither numbering scheme is a permanent task identifier.

## Troubleshooting

- **`/rw` reports no tasks:** in pinned mode, pin at least one unarchived task
  in Codex Desktop, or switch to `/rwmode recent`. In recent mode, confirm
  that Desktop has unarchived user tasks and refresh with `/rw`. Independent
  sessions created only inside cc-connect are not part of these lists.
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
