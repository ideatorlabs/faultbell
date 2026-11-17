# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-17

### Added

- `faultbell.watch`: server watch for problems an exception hook never sees.
  - `Watch.record()` and the pure-ASGI `WatchMiddleware`: one alert when a route (counted by its template)
    returns several 5xx (default 3) or many 4xx (default 30) responses within a window (default 5 minutes).
  - `Watch.check_memory()`: alert above a share (default 80%) of the memory limit, read from the argument,
    `FAULTBELL_MEMORY_LIMIT_MB` or the container's cgroup limit.
  - Crash detection: a heartbeat per process; a process that stopped beating without a clean shutdown is
    reported on the next start with its last-seen peak memory. `MemoryHeartbeatStore` and
    `SQLHeartbeatStore` (any SQLAlchemy database, new `sqlalchemy` extra).
  - `Watch.start()` runs memory checks and the heartbeat on a daemon thread; nothing ever raises.

## [0.1.0] - 2026-11-17

### Added

- Initial release.
- `install()` with a logging handler, `sys` / `threading` / `asyncio` hooks and a FastAPI/Starlette
  request-id middleware plus catch-all 500 handler.
- `capture()`, `set_context()`, `context()`, `request_id()`, `flush()`, `stats()`.
- `job_run()` / `job_failed()`: one summary card per job run, nested runs folded, demo items skipped.
- Fingerprint-based de-duplication with a configurable window; suppressed repeats reported on the next card.
- Scrubbing of emails, tokens, JWTs, API keys (configurable prefixes), credential values, long hex/base64
  strings, webhook URLs and phone numbers in every part of a card.
- Channels: Discord (embed + attached traceback), Slack (Block Kit), Microsoft Teams (Adaptive Card),
  Google Chat, Telegram (bot message + traceback file), PagerDuty (Events API v2, one incident per
  problem), email over any SMTP server (text + HTML, traceback attached), any JSON webhook, and
  `CallbackTransport` for anything else.
- Several channels at once, each with an optional `kinds` filter; configured in code
  (`configure(channels=...)`) or with one set of `FAULTBELL_*` variables per channel.
- `python -m faultbell channels` and `python -m faultbell test` to check the setup.

[Unreleased]: https://github.com/ideatorlabs/faultbell/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ideatorlabs/faultbell/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ideatorlabs/faultbell/releases/tag/v0.1.0
