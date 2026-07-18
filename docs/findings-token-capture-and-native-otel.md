# Findings — Langfuse token capture repair + native-OTel evaluation

> Date: 2026-07-17. Scope: the Claude Code → Langfuse token/cost pipeline. This is
> a self-contained synthesis of two efforts: (1) repairing why the transcript-parsing
> hook had silently stopped capturing subagent tokens, and (2) evaluating whether
> Claude Code's **native** OpenTelemetry could replace the hook for the real goal —
> **accurate per-project cost**. Companion detail lives in
> `token-capture-gap-investigation.md` (the investigation) and
> `otel-migration-test-plan.md` (the evaluation protocol + run results).

## TL;DR

- A hook (`~/.claude/hooks/langfuse_hook.py`) parses Claude Code transcripts and
  writes per-call generations (tokens + cost) to a self-hosted Langfuse.
- It had regressed to capturing **~13%** of subagent tokens. Root cause: Claude Code
  made the **Agent tool asynchronous** (~v2.1.196, late June 2026), which broke five
  distinct assumptions in the hook. All five are fixed and installed; live capture is
  back to **~95%** (per session, parent + subagent, 0 duplicates).
- We then tested native OTel as a possible replacement. For the goal of accurate
  **per-project total cost**, native **cannot replace the hook today**: it has no
  working-folder attribute (so no per-project grouping), it lands **$0** in Langfuse
  (empty `usage_details`), and it reports cache creation as a single aggregate (no
  5m/1h split → wrong pricing). Its one advantage — it captures auxiliary API calls
  the hook can't see — is dominated by cheap `haiku` overhead. **Decision: stay on
  the hook.**

---

## Part 1 — Why subagent tokens vanished, and the five defects

### The trigger: the Agent tool went async

| Claude Code | `Agent` tool_result | Effect on the hook |
|---|---|---|
| ≤ v2.1.195 (through ~06-26) | the subagent's **real output** (5–24 KB) | parent **blocked** on the subagent, so the turn only finalized after the subagent transcript was complete → the hook always expanded a complete file. Worked for months (peak 7,280 subagent gens/day). |
| ≥ v2.1.196 (~late June) | **"Async agent launched successfully"** (~1 KB spawn ack) | parent **continues**; the turn finalizes at spawn while the subagent file is still being written → capture collapsed. |

This is a Claude Code behavior change, not a hook edit (the hook was unchanged since
May 9). It is the "cliff" the daily numbers showed.

### The five defects (all fixed + installed)

1. **Line-1137 no-`transcript_path` flush.** The final-turn `create_trace` omitted
   `transcript_path`, so `discover_subagents(None)` returned `{}` → zero subagent
   expansion on that turn, even when every subagent file was present. Under
   incremental firing the "last turn of the fire" is *most* turns.
2. **Incomplete-file replay → sweep-primary completion capture.** Because the Agent
   tool_result is now a spawn ack (not the completion signal), the fix cannot expand
   a subagent inline — the file is still being written. Solution: **defer** any
   subagent that isn't quiescent (60 s idle) or proven complete by a
   `<task-notification>`, record it as *pending* with the parent turn's `trace_id`,
   and a **decoupled sweep** emits it on a later fire into the same trace.
   Exactly-once is enforced by state bookkeeping — the **Langfuse v3 SDK cannot pin
   observation ids** (`start_observation` takes no id; `TraceContext` is only
   `{trace_id, parent_span_id}`), so a re-emit duplicates rather than upserts; the
   state set is the only guard.
3. **No `message.id` merge in `emit_subagent_span`.** Claude Code writes **one
   transcript record per content block**, each repeating the full `message.usage`, so
   every block became its own generation carrying the whole message's tokens
   (~2.2–2.8× inflation). Fixed with a merge-by-`message.id` pass (the parent path
   already did this).
4. **Turns spanning a fire boundary dropped — parent AND subagents.**
   `process_transcript` rebuilt `current_user` from scratch each fire but only sees
   `[last_line → EOF]`. A turn that opened on a user record an *earlier* fire consumed
   had `current_user=None`, so the finalize guard skipped the whole turn and
   `last_line` advanced past it. A **general turn-loss bug that predated the async
   work** and was merely exposed by it (task-notifications act as turn boundaries in
   long async runs). Fixed by persisting `open_user` across fires. Also fixed the
   queue-path (Langfuse-unreachable) state write that clobbered the subagent
   bookkeeping.
5. **`agent_id` prefix inconsistency.** `discover_subagents` kept the `agent-`
   filename prefix in the id while the agentId path used the bare id, so the same
   subagent was keyed two ways → mislabeled `subagent_id`, an exactly-once hazard,
   and broken `<task-id>` notification matching. Fixed by normalizing to the bare id.

### Result & residuals

- **Capture: ~13% → ~95% live** (per session; parent + subagent balanced; 0 dupes),
  confirmed on live `/research` runs — not replay.
- **Two residuals** (minor, deferred): (a) **60 s-quiescence truncation** — a
  subagent that goes quiescent > 60 s mid-run then resumes loses its later messages;
  needs a completion-aware check, not bare quiescence. (b) A **parent turn-cluster
  drop** on some long sessions that `open_user` doesn't fully close.

### Meta-lesson (the most transferable output)

Six confident reconstructions overstated across this investigation — matcher
mismatch → timing race → line 1137 → incomplete-file → async cliff → an "agentId
straddle" that turned out wrong. **Every one was corrected only by a controlled live
run**; replay overstated capture repeatedly (once by 3×, by running with files
already complete). The rule that held: **only a live run ever settled a claim.**

---

## Part 2 — Native OTel vs the hook (goal: accurate per-project cost)

### Why look at native at all

The hook is *structurally* forced to parse transcripts: the Stop-hook payload carries
no usage — only `session_id` / `transcript_path`. Every one of the five defects lived
in that reconstruction layer. Claude Code's native OpenTelemetry emits the usage
directly, which would obviate the whole front-end — hence the evaluation.

### Setup (verified)

- Native telemetry is enabled **per session in the shell** (never `settings.json`,
  which is global to every session), pointed at a **dedicated** Langfuse project
  (`claude-code-otel`) so it never commingles with the hook's project.
- Enable = `CLAUDE_CODE_ENABLE_TELEMETRY=1` + `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`
  (beta trace spans) + `OTEL_TRACES_EXPORTER=otlp` + endpoint
  `http://localhost:3000/api/public/otel` + Basic auth `base64(public_key:secret_key)`.
- The two writers do **not** cross-wire: the Langfuse SDK builds its exporter with an
  explicit endpoint from `LANGFUSE_HOST`+keys and ignores the generic `OTEL_*` vars.

### Phase 0 (baseline) — a methodology finding

The hook stamps a generation's `start_time` at **emission**, not API-call time
(`start_day == ingest_day` for every hook gen). So a **time-windowed** comparison is
invalid — a "since 2026-07-16" window read **376 M** hook tokens against **159 M** of
transcript truth, purely from earlier re-emissions. **All reconciliation must be by
`session_id`, not by time window.**

### Phase 1 (the decisive gate) — GATE 1a: FAIL

The `claude_code.llm_request` span carries `input_tokens`, `output_tokens`,
`cache_read_tokens`, and **`cache_creation_tokens` as a single aggregate — no
`ephemeral_5m` / `ephemeral_1h` split**. This workload's cache creation is **100% 1h**
(priced 2× vs 5m's 1.25×), so the missing split is a **concrete** pricing regression.

### 3-way reconciliation (live `/research`, session `089e4a80`)

| source | calls | tokens | cost in Langfuse | groups by folder? | cache tiers? |
|---|--:|--:|--:|:--:|:--:|
| **T** transcript truth | 72 | 6.49 M | — | — | yes (5m/1h) |
| **H** hook | 71 | 6.44 M (99 %) | **$8.25, all 71 priced** | **yes** (`project=llm-wiki`) | **yes** |
| **N** native | 109 | 8.40 M (129 %) | **$0** (unpriced) | **no** | no (aggregate) |

### The three findings that decided it for per-project cost

1. **Native has no working-folder / project attribute.** resourceAttributes are only
   `service.name=claude-code` + OS; span attributes are user / session / org / model.
   No cwd, dir, repo, or project → **cannot group cost by folder.** The hook derives
   the project from the transcript cwd and tags the trace (`llm-wiki`).
2. **Empty `usage_details` → $0 in Langfuse.** Langfuse computes cost by reading a
   span's `usage_details` token map × its model price table. Native spans have
   `usage_details = {}` (tokens sit in a raw `attributes` JSON the cost engine doesn't
   read), so every native span reads **$0** in Langfuse's UI, dashboards, and totals.
   Getting cost requires either an **OpenTelemetry Collector** between Claude Code and
   Langfuse to rename attributes to the `gen_ai.usage.*` convention, or
   **self-computed** pricing from the raw JSON. Both are engineering, not config. The
   hook populates `usage_details` and Langfuse auto-prices (the $8.25 is live).
3. **Native is more *complete* on raw volume** — 109 distinct **successful** requests
   (`attempt=1`, no retries) vs 72 transcript messages, because it spans auxiliary
   API calls (e.g. `haiku` title-generation) that never become conversation turns and
   are thus invisible to any transcript reader. But that gain is dominated by cheap
   haiku (~0.13 M tokens here).

Attribution note: native distinguishes subagent vs main-loop via `llm_request.context`
(`tool` / `interaction`) + a populated `agent_id`, **not** the documented
`query_source` / `agent.name` (those are on the *metrics* export, which this run did
not enable — only traces).

### Verdict

**Stay on the hook.** For accurate per-project cost, native trades **three
regressions** (no folder grouping, $0 in Langfuse without a collector build, coarse
cache) for **one gain** (auxiliary-call completeness, mostly negligible). The hook
already delivers per-folder, auto-priced, cache-correct cost today; its only gap is
auxiliary calls, which are structurally invisible to a transcript reader. Native
becomes worth revisiting **only** if total spend must include those auxiliary calls
*and* the collector + folder-tagging build is justified. The dual-write grind
(test-plan Phase 2/3) is **not** warranted for this goal.

---

## Reference data

- **Hook:** `~/.claude/hooks/langfuse_hook.py`; repo `hooks/langfuse_hook.py`
  (byte-identical after this work). The Stop hook runs it via
  `uv run --with 'langfuse>=3.0,<4.0'`.
- **Langfuse self-host:** `http://localhost:3000`; OTLP ingest at
  `/api/public/otel` (Basic auth `base64(pk:sk)`); ClickHouse container
  `langfuse-self-host-clickhouse-1`, db `default`, tables `observations` / `traces`
  (ReplacingMergeTree → always query with `FINAL`; the hook's re-emission duplicates
  key on distinct observation id but same `anthropic_message_id`, so dedup by
  message id).
- **Project ids:** hook = `cmorsae2x0008n107rv5i5cqp` (`claude-code`); native =
  `cmrpc70lv0001nt07arirumao` (`claude-code-otel`).
- **Native `llm_request` token attributes** (in the raw `attributes` JSON, NOT
  `usage_details`): `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens` (aggregate), `llm_request.context`, `agent_id`.
- **Ground-truth accountant:** `~/llm_wiki/scripts/usage-report.py` (deduped by
  `message.id`; scans parent `*.jsonl` + `<session>/subagents/*.jsonl`).

## Status of the code

Five hook fixes + these docs are on branch
`fix/subagent-capture-upstream-installed-hook`, pushed to the fork
`ChrisHenryOC/claude-code-langfuse-template` (the upstream `doneyli/...` is
write-protected for this account). The **installed** hook equals this branch, not the
`doneyli` upstream. Open follow-ups: the two residuals above, and purging the
investigation's re-emission duplicate rows from ClickHouse (they inflate any
cross-session aggregate; per-session dedup-by-message-id hides them).
