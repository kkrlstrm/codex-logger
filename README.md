# codex-logger

<!-- portfolio-status -->
**Status:** Production-used — I run this against my own live agent workflows. · **Layer:** Runtime observability · **[Portfolio map ›](https://github.com/kkrlstrm)**

> **Now part of [callusguard](https://github.com/kkrlstrm/callusguard).**
> codex-logger is the Codex half of the `record` stage in a five-stage loop — `record → derive → guard → verify → prune` —
> exposed there as `callus record ingest`. callusguard ships all five stages for both Claude Code and Codex, as one install. This repo stays up and works; callusguard is where the loop closes.

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
`~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`, giving you a queryable index of
sessions, turns, tool calls, messages, models, tokens, and subagent relationships.
That means it:

- covers the tools hooks don't — `apply_patch`, `write_stdin`, MCP calls, and
  subagent activity, not just the shell subset,
- works **retroactively** on sessions already on disk, with nothing installed into
  Codex and no change to your workflow,
- runs **local-first** — a zero-config SQLite file by default, optional Postgres
  only if you want to.

It reports what Codex reports and doesn't invent what it doesn't: a tool call's
success/failure comes from the structured `*_end` event Codex emits for it
(`exec_command_end.exit_code`, `patch_apply_end.success`), and a call whose outcome
Codex never states is kept as `unknown` rather than guessed.

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
| `response_item / function_call` (+`_output`) | every tool call — `exec_command`, `apply_patch`, `write_stdin`, MCP — paired by `call_id`, stamped with its `turn_id` |
| `event_msg / *_end` | authoritative outcome per call: `exec_command_end.exit_code`, `patch_apply_end.success` (a non-shell call with no reported status stays `unknown`) |
| `response_item / message` | user prompts + assistant text (the `event_msg` `agent_message`/`user_message` events are the streamed duplicates — not re-ingested) |

Because every tool call and message carries the `turn_id` it happened in, you can
ask turn-level questions — which prompt triggered the failed patch, which subagent
turn burned the most tokens, which model was active for a given MCP call.

## How it compares

| | Shell | `apply_patch` | MCP calls | Subagents | Retroactive | Local-first | Zero setup |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Codex hooks** (build tested) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | — |
| **Cloud tracing plugin** | ✅ | ✅ | ✅ | ✅ | live only | ❌ | ❌ |
| **codex-logger** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

<sub>The "Codex hooks" row reflects the `PreToolUse`/`PostToolUse` firing matrix on the
version noted above, and may change in future Codex releases.</sub>

## Quick start

Nothing to install for the default SQLite backend — it's pure stdlib. Run it from
a checkout, or install the `codex-logger` command:

```bash
pipx install git+https://github.com/kkrlstrm/codex-logger    # or: pip install -e .
```

```bash
codex-logger ingest             # load all rollout files on disk
codex-logger sessions           # list recent sessions
codex-logger stats              # tokens by model + top tools
codex-logger inspect <id>       # one session in detail (id prefix ok)
# equivalently, from a checkout without installing:  python3 -m codex_logger <cmd>
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

tool calls:                       (seq · turn · tool · status · args)
    4 019xxab1 exec_command     success  {"cmd":"pytest -q", ...}
   12 019xxab1 apply_patch      success  {"changes":{"src/app.py": ...}}
   17 019xxcd2 apply_patch      failure  {"changes":{"src/db.py": ...}}
   26 019xxcd2 mcp.fetch        success  {"url":"https://api.example.com/...", ...}
```
<sub>Illustrative session; paths and arguments elided. Each call shows the turn it
ran in, and a status resolved from Codex's own `*_end` events.</sub>

## Commands

```bash
codex-logger ingest [--watch] [--interval N] [--force] [--verbose]
codex-logger sessions [--limit N] [--days N]
codex-logger inspect <session-id-prefix>
codex-logger stats [--days N]
codex-logger install-launchd [--interval N] [--print]   # macOS scheduling
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

-- Which turn triggered a failed patch (turn-level attribution)
SELECT session_id, turn_id, tool_name, status
FROM tool_calls
WHERE tool_name = 'apply_patch' AND status = 'failure';
```

<sub>On Postgres the tables are prefixed (`codex_sessions`, `codex_tool_calls`, …);
on SQLite they're bare as shown. The CLI handles the difference for you.</sub>

## Storage

Default: SQLite at `~/.codex-logger/codex.db` — zero-config, works immediately.

To co-locate with cc-logger's Postgres/Neon warehouse (one dashboard across Claude
Code + Codex), point it at a `postgresql://` URL. Tables are prefixed `codex_*` and
every row carries `source='codex'`, so a `UNION` view against the cc-logger tables
is trivial — and all four commands work against Postgres, not just `ingest`:

```bash
export CODEX_LOGGER_DB="postgresql://…/neondb?sslmode=require"
pip install 'psycopg[binary]'
codex-logger ingest
codex-logger stats          # queries codex_* automatically
```

> The SQLite path is exercised end-to-end in the test suite. The Postgres backend
> mirrors the same schema (and the CLI routes to the prefixed tables), but isn't yet
> covered by a live-DB integration test — that's the next hardening step.

## Schema

`sessions`, `tool_calls`, `messages`, `turns`, plus `ingest_state` for incremental
bookkeeping. `tool_calls` and `messages` each carry a `turn_id`. See
[`codex_logger/store.py`](codex_logger/store.py) for the DDL.

## Privacy & security

`codex-logger` stores your local Codex history verbatim: prompts, assistant
messages, tool arguments, and command output — which can include file contents,
internal URLs, and secrets that scrolled through a terminal. Treat the database as
sensitive developer telemetry:

- The default lives at `~/.codex-logger/codex.db` on your machine. Keep it there.
- Don't commit it to a repo or sync it to shared/cloud storage.
- If you point it at Postgres, use a private database with least-privilege access.

Tool output is capped per row (200 KB) to bound runaway logs; a redaction mode for
prompts/outputs is a planned option, not yet implemented.

## Run it on a schedule (launchd, macOS)

```bash
codex-logger install-launchd --interval 300   # writes a plist wired to this machine
```

This generates `~/Library/LaunchAgents/com.codex-logger.ingest.plist` with your real
Python path and repo path filled in (use `--print` to review it first), then tells
you the `launchctl load` command to run. It ingests changed rollout files every
5 minutes — near-zero work when idle — logging to
`~/Library/Logs/codex-logger.{out,err}.log`. A hand-editable template lives in
[`launchd/`](launchd/).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests run against synthetic Codex `0.140`-style rollout events and cover session
identity, model extraction, tool calls, **status resolved from `*_end` events**,
**per-turn attribution**, token accounting, message extraction, malformed-line
tolerance, and idempotent SQLite writes. The parser is fail-open — a malformed JSONL
line is skipped, never fatal. CI runs the suite on Python 3.10–3.13.

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

---

<!-- portfolio-footer -->
## Where this fits

Part of a portfolio of **governed, AI-native GTM systems** — reference implementations and reusable patterns extracted from a private production stack. In that system this is the flight recorder for the OpenAI Codex CLI.

**Full portfolio map → [github.com/kkrlstrm](https://github.com/kkrlstrm)**

Works with:
- [codex-guard](https://github.com/kkrlstrm/codex-guard) — the control surface that acts on what this observes
- [cc-logger](https://github.com/kkrlstrm/cc-logger) — the Claude Code sibling
