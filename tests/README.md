# WatcherDog test suite

Run these after **any** change to confirm the app still imports and behaves correctly.

## Running

```bash
scripts/run_tests.sh           # everything (auto-installs pytest if missing)
scripts/run_tests.sh -k config # only tests matching "config"
scripts/run_tests.sh -v        # verbose

# or directly:
.venv/bin/python -m pytest
```

No network, Telegram, Ollama, or Hermes access is needed — every external
service is mocked. Tests write only to pytest's temp dirs, never to real
`data/`, `logs/`, or your `.env`.

## What's covered

| File | Module under test | What it checks |
|------|-------------------|----------------|
| `test_smoke.py` | **whole tree** | every `.py` file compiles; every module imports. The broadest "did I break it" net. |
| `test_config.py` | `watcherdog.config` | `.env` parsing, defaults, env-overrides-file, path resolution, `validate*()`. |
| `test_classifier.py` | `watcherdog.classifier` | error / normal / unknown buckets, bot-name extraction. |
| `test_monitor.py` | `watcherdog.monitor` | error normalization & hashing, traceback detection, incremental reads, rotation, offset persistence. |
| `test_analyzer.py` | `watcherdog.analyzer` | Ollama response parsing and fallbacks (network mocked at `urlopen`). |
| `test_alerter.py` | `watcherdog.alerter` | alert formatting, `TelegramAlerter.send` retry/early-exit, `UserClientAlerter`. |
| `test_storage.py` | `watcherdog.storage` | SQLite record/dedupe/recent, excerpt truncation. |
| `test_heartbeat.py` | `watcherdog.heartbeat` | silence detection, grace period, recovery, persistence. |
| `test_run.py` | `run._process_incident` | dedupe window, severity threshold, record-on-failure. |

## Adding tests

Drop a `test_*.py` in this directory. Pure-logic functions need no mocking;
for anything touching the network/subprocess/Telethon, mock at the boundary
(see `test_analyzer.py` for the `urlopen` pattern). The `test_smoke.py` import
list should gain any new top-level module so a broken import is caught early.
