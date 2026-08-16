# Changelog

All notable changes to this project are documented here. Versions refer to the
Python notifier unless a cc-connect routing patch is named explicitly.

## [Unreleased]

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

[Unreleased]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/JiaYang-BUAA/cc-connect-codex-wechat-router/releases/tag/v1.1.0
