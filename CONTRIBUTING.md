# Contributing

Contributions that improve reliability, documentation, tests, or Windows
compatibility are welcome.

## Before opening an issue

1. Run `python .\notifier.py --config .\config.json --selftest`.
2. Check both scheduled tasks and the latest notifier and cc-connect logs.
3. Remove tokens, user IDs, answer bodies, database contents, and local paths
   from logs and screenshots.

Use the bug-report template and include the notifier version, cc-connect patch
version, Windows version, Python version, reproduction steps, and sanitized
logs.

## Development

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File .\tools\check-public-repo.ps1
```

Keep changes focused and add tests for behavioral changes. Do not commit
`config.json`, `data/`, `logs/`, databases, transcripts, tokens, executable
artifacts, or machine-specific deployment backups.

The notifier and cc-connect modifications are maintained separately. Changes
to the Go routing layer belong in the cc-connect fork's `quote-router` branch
and must retain upstream license notices.
