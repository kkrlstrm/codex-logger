# codex-logger

**The local black box recorder for OpenAI Codex CLI** — full-fidelity local
observability, read from the files Codex already writes.

Codex emits a detailed event stream for every session, but most of that history
stays buried in `~/.codex/sessions`. `codex-logger` turns those rollout files into
a queryable SQLite (or Postgres) database, so you can see what Codex actually did:
prompts, assistant messages, tool calls, patches, MCP calls, subagents, models, and
token usage — no hooks, no cloud, no setup.

```
by model:
  gpt-5.4-mini            8 sessions     276 calls   13,101,062 tok
  codex-auto-review       4 sessions       2 calls    1,723,376 tok
  gpt-5.1-codex-max       3 sessions      11 calls      362,206 tok

top tools:
  exec_command            220  18 failures
  write_stdin              58  6 failures
  apply_patch               9  0 failures
  update_plan               2  0 failures
```
<sub>`codex-logger stats` over a real local Codex history. Note the `apply_patch` and
`write_stdin` calls — a hook-based logger on today's Codex never sees them (see below).</sub>

## Why this exists

**Hooks are not enough.**

Codex has a hook system, but on the build tested here (`0.140.0-alpha.2`)
`PreToolUse` / `PostToolUse` fire **for shell commands only**, and are discovered
from config layers rather than agent manifests. So a hook-based logger — the obvious
design, and the one [`cc-logger`](https://github.com/kkrlstrm/cc-logger) uses for
Claude Code — would silently miss a large part of what a Codex agent does:
`apply_patch`, MCP tool calls, and subagent activity.

`codex-logger` bypasses hooks entirely. It reads the append-only **rollout JSONL
files** Codex already writes to
`~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`, which record the complete event
stream. That means it:

- captures **everything** — shell, `apply_patch`, `write_stdin`, MCP calls,
  subagents — not just the shell subset hooks expose,
- works **retroactively** on sessions already on disk, with nothing installed into
  Codex and no change to your workflow,
- runs **local-first** — a zero-config SQLite file by default, optional Postgres
  only if you want to.

> Codex changes fast. The hook behavior above is what was observed on the version
> noted, not a permanent claim — the point is that reading rollout files is robust
> to whichever tools Codex routes through hooks in a given release.

## Questions it helps answer

- Which Codex sessions burned the most tokens — and in which project directory?
- Which models are actually doing the work? (`gpt-5.4-mini` vs `codex-auto-review`
  vs `gpt-5.1-codex-max` …)
- Which tools fail most often?
- How much work is happening inside subagents (e.g. a `guardian` reviewer) vs the
  main thread?
- What did Codex patch, run, or call during a given session?
- How does Codex usage compare with Claude Code usage — in one warehouse?

## What it captures

Each rollout event maps to normalized columns:

| Rollout event | What we extract |
|---|---|
| `session_meta` | session id, `parent_thread_id` (subagent → parent link), `cwd`, `originator` (e.g. `codex_vscode`), `cli_version`, subagent type (e.g. `guardian`) |
| `turn_context` | active `model` per turn (`gpt-5.4-mini`, `codex-auto-review`, …) |
| `event_msg / token_count` | cumulative session tokens + **per-turn** usage (input / cached / output / reasoning / total) |
| `response_item / function_call` + `function_call_output` | every tool call — `exec_command`, `apply_patch`, `write_stdin`, MCP — paired by `call_id`, with exit code + success/failure |
| `response_item / message` | user prompts + assistant text |

## How it compares

| | Shell | `apply_patch` | MCP calls | Subagents | Retroactive | Local-first | Zero setup |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Codex hooks** (build tested) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | — |
| **Cloud tracing plugin** | ✅ | ✅ | ✅ | ✅ | live only | ❌ | ❌ |
| **codex-logger** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

<sub>The "Codex hooks" row reflects the `PreToolUse`/`PostToolUse` firing matrix on the
version noted above, and may change in future Codex releases.</sub>

## Quick start

Nothing to install for the default SQLite backend — it's pure stdlib.

```bash
cd codex-logger
python3 -m codex_logger ingest            # load all rollout files on disk
python3 -m codex_logger sessions          # list recent sessions
python3 -m codex_logger stats             # tokens by model + top tools
python3 -m codex_logger inspect <id>      # one session in detail (id prefix ok)
```

`sessions` gives you the recent history at a glance:

```
session    model              origin         sub        calls    tokens  started
019f37d0   gpt-5.4-mini       codex_vscode   -              4    74,628  2026-07-06T14:24:12Z
019f37a2   codex-auto-review  codex_vscode   guardian       0    43,344  2026-07-06T13:34:11Z
...
```

`inspect` opens one session — its models, token split, and the full ordered tool
stream, including the patches and MCP calls hooks would miss:

```
session   019xxxxx-...
model     gpt-5.4-mini  (openai)
origin    codex_vscode  subagent=None
cwd       ~/projects/acme
time      ...T19:29:48Z -> ...T19:51:43Z
tokens    in=182,140 cached=160,448 out=48,435 reasoning=27,051 total=257,626
turns     6   tool_calls 41

tool calls:
    4 exec_command     success  {"cmd":"pytest -q","workdir":"~/projects/acme", ...}
   12 apply_patch      success  {"changes":{"src/app.py":{"update": ...}}}
   17 write_stdin      success  {"stdin":"y\n", ...}
   26 mcp.fetch        success  {"url":"https://api.example.com/...", ...}
```
<sub>Illustrative session; paths and arguments elided.</sub>

## Commands

```bash
python3 -m codex_logger ingest [--watch] [--interval N] [--force] [--verbose]
python3 -m codex_logger sessions [--limit N] [--days N]
python3 -m codex_logger inspect <session-id-prefix>
python3 -m codex_logger stats [--days N]
```

`ingest` is incremental — unchanged files are skipped by size+mtime, so re-running
(or the launchd job) is near-zero work when idle. `--watch` polls continuously.

## Query it directly

The schema is small and stable, so plain SQL answers most questions:

```sql
-- Most expensive sessions
SELECT substr(session_id,1,8) AS session, model, total_tokens,
       num_tool_calls, cwd
FROM sessions
ORDER BY total_tokens DESC
LIMIT 20;

-- Tools with the highest failure rate
SELECT tool_name,
       COUNT(*) AS calls,
       SUM(CASE WHEN status = 'failure' THEN 1 ELSE 0 END) AS failures
FROM tool_calls
GROUP BY tool_name
ORDER BY failures DESC;

-- How much work happens inside subagents
SELECT subagent_type, COUNT(*) AS sessions, SUM(total_tokens) AS tokens
FROM sessions
GROUP BY subagent_type;
```

## Storage

Default: SQLite at `~/.codex-logger/codex.db` — zero-config, works immediately.

To co-locate with cc-logger's Postgres/Neon warehouse (one dashboard across Claude
Code + Codex), point it at a `postgresql://` URL. Tables are prefixed `codex_*` and
every row carries `source='codex'`, so a `UNION` view against the cc-logger tables
is trivial:

```bash
export CODEX_LOGGER_DB="postgresql://…/neondb?sslmode=require"
pip install 'psycopg[binary]'
python3 -m codex_logger ingest
```

> The SQLite path is exercised end-to-end in the test suite. The Postgres backend
> mirrors the same schema but isn't yet covered by a live-DB test — that's the next
> hardening step.

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

Tests run against synthetic Codex `0.140`-style rollout events and cover session
identity, model extraction, tool calls, exit status, token accounting, message
extraction, malformed-line tolerance, and idempotent SQLite writes. The parser is
fail-open — a malformed JSONL line is skipped, never fatal.

## Where this fits

`codex-logger` is one piece of a **local agent-ops stack** — capture what coding
agents actually do, measure where the work happens, compare runtimes, and build
guardrails from real execution data:

- [`cc-logger`](https://github.com/kkrlstrm/cc-logger) — Claude Code observability.
- [`agent-guard`](https://github.com/kkrlstrm/agent-guard) — Claude Code policy / control.
- **codex-logger** — Codex observability (this repo).

A Codex-side guard (a `PreToolUse` command hook that allow/deny/rewrites shell
commands, the agent-guard equivalent) is a natural next sibling; the open question
there is Codex's live hook-firing matrix, not this logger.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
