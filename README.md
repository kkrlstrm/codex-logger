# codex-logger

Telemetry for the **OpenAI Codex CLI** — the sibling of [`cc-logger`](../cc-logger)
for Claude Code. It records every Codex session, turn, tool call, model, and token
count into a queryable database.

## Why it reads files instead of hooks

Claude Code's cc-logger consumes lifecycle **hooks**. Codex has hooks too, but on
the current build (`0.140.0-alpha.2`) `PreToolUse`/`PostToolUse` fire **for shell
commands only** and are discovered from config layers, not agent manifests — so a
hook-based logger would miss `apply_patch`, MCP calls, and subagent activity.

Instead, codex-logger tails the **rollout files** Codex already writes to
`~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`. These append-only JSONL files
record the complete event stream, so a pure tailer captures **everything** a hook
never sees:

| Rollout event | What we extract |
|---|---|
| `session_meta` | session id, `parent_thread_id` (subagent → parent link), `cwd`, `originator` (e.g. `codex_vscode`), `cli_version`, subagent type (e.g. `guardian`) |
| `turn_context` | active `model` per turn (`gpt-5.4-mini`, `codex-auto-review`, …) |
| `event_msg / token_count` | cumulative session tokens + **per-turn** `last_token_usage` (input / cached / output / reasoning / total) |
| `response_item / function_call` + `function_call_output` | every tool call — `exec_command`, `apply_patch`, `write_stdin`, MCP — paired by `call_id`, with exit code + success/failure |
| `response_item / message` | user prompts + assistant text |

No hooks to install, nothing to configure in Codex, works retroactively on sessions
already on disk.

## Install

Nothing to install for the default SQLite backend — it's pure stdlib.

```bash
cd codex-logger
python3 -m codex_logger ingest            # load all rollout files on disk
python3 -m codex_logger sessions          # list recent sessions
python3 -m codex_logger inspect <id>      # one session in detail (id prefix ok)
python3 -m codex_logger stats             # tokens by model + top tools
```

## Commands

```bash
python3 -m codex_logger ingest [--watch] [--interval 10] [--force] [--verbose]
python3 -m codex_logger sessions [--limit 30] [--days 7]
python3 -m codex_logger inspect <session-id-prefix>
python3 -m codex_logger stats [--days 7]
```

`ingest` is incremental — unchanged files are skipped by size+mtime, so re-running
(or the launchd job) is near-zero work when idle. `--watch` polls continuously.

## Storage

Default: SQLite at `~/.codex-logger/codex.db`.

To co-locate with cc-logger's Neon warehouse (one dashboard across Claude Code +
Codex), point it at Postgres — tables are prefixed `codex_*` and every row carries
`source='codex'`, so a `UNION` view against the cc-logger tables is trivial:

```bash
export CODEX_LOGGER_DB="postgresql://…/neondb?sslmode=require"
pip install 'psycopg[binary]'
python3 -m codex_logger ingest
```

## Schema

`sessions`, `tool_calls`, `messages`, `turns`, plus `ingest_state` for incremental
bookkeeping. See [`codex_logger/store.py`](codex_logger/store.py) for the DDL.

## Run it on a schedule (launchd)

```bash
cp launchd/com.kaikarlstrom.codex-logger.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kaikarlstrom.codex-logger.plist
launchctl start com.kaikarlstrom.codex-logger      # run now
```

Ingests changed rollout files every 5 minutes. Logs to
`~/Library/Logs/codex-logger.{out,err}.log`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Status

Working end-to-end on real Codex `0.140.0-alpha.2` rollout data (VS Code extension).
The **guard** sibling (the agent-guard equivalent — a `PreToolUse` command hook that
allow/deny/rewrites shell commands) is a separate follow-up; the outstanding question
there is the live hook-firing matrix, not this logger.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
