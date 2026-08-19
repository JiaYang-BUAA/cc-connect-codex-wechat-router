# Changelog

All notable changes to this project are documented here. Versions refer to the
Python notifier unless a cc-connect routing patch is named explicitly.

## [Unreleased]

## [1.2.1] - 2026-08-19

- Restored Codex Desktop submission after Desktop added an `initialRoute` query
  to its primary CDP page URL, while continuing to exclude the avatar overlay.
- Added a total CDP request deadline so unrelated browser events cannot keep a
  submission worker blocked indefinitely.
- Added rollout-based submission recovery after transport failures to avoid
  submitting the same Weixin reply twice when Desktop accepted the first call.
- Kept replies durably queued during Desktop and app-server infrastructure
  outages instead of deleting them after the ordinary retry limit.

## [1.2.0] - 2026-08-18

- Routed ordinary Weixin follow-ups into Codex Desktop's native queued-follow-up
  list, where they appear above the composer and can be edited, reordered, or
  removed before Desktop submits them. The notifier queue remains as a fallback.
- Added an ordered per-task queue-content snapshot to Weixin queue
  acknowledgements, with bounded previews for long messages and large queues.
- Added a combined Windows Release package and guided installer so end users
  can install the notifier and pinned custom cc-connect build from this
  repository alone.
- Fixed queued-reply acknowledgements to count both Codex Desktop's native
  queued follow-ups and earlier WeChat replies for the same task.
- Added pinned Codex Desktop automation targets to `/rw`, final-answer push,
  numbered submission, and quoted-reply routing. New scheduled run results are
  routed back to the pinned automation target without replaying old runs.
- Added optional `/rwfolder` notifications for every task inside pinned Codex
  Desktop projects, with quoted replies routed back to the originating task.
- Updated Codex Desktop submission to use the current in-app AppServer request
  client after the previous bundled-function export was removed.
- Expanded the in-Weixin `/hp` guide for first-time users.
- Improved deployment validation so expected shutdown logs do not trigger a
  false rollback.

## [1.1.0] - 2026-08-16

- Added pinned Codex Desktop final-answer notifications to Weixin.
- Added quoted-reply routing back to the originating task.
- Added `/rw`, `/rw<number> content`, `/y`, and `/rwpush` workflows.
- Added durable queues, queue promotion, duplicate suppression, retry backoff,
  voice transcription routing, health checks, and Windows scheduled-task
  installation.
- Added the cc-connect `v1.4.1+qr3` routing companion.

[Unreleased]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/tag/v1.2.1
[1.2.0]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/tag/v1.2.0
[1.1.0]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/tag/v1.1.0
