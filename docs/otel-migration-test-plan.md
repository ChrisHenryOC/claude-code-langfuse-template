# Native OTel vs. transcript-hook — test plan

> Status: consensus reached (both collaborating sessions signed off); no config
> changed yet. Author: Claude (opus), `claude-code-langfuse-template` session.
> Companion to `token-capture-gap-investigation.md`. Session names used throughout
> are defined in "Execution roles & sessions" below.
>
> **Question under test:** Can Claude Code's native OpenTelemetry (beta
> `claude_code.llm_request` traces) replace the transcript-parsing hook for
> token/cost capture **without losing pricing fidelity**? The transcript hook is
> at a measured, live **95%** and is the baseline any alternative must beat.
>
> **Non-negotiable lessons baked into this plan** (each cost us a wrong
> conclusion earlier):
> - **Live > replay.** Replay overstated capture 3× because it ran with files
>   already complete. Every gate below is measured on live runs.
> - **Pin absolute dates.** `now() - INTERVAL N DAY` moved under two comparisons.
>   Every query uses fixed `start_time`/`--since` bounds.
> - **Dedup by `message.id`.** Claude Code writes one record per content block,
>   each repeating full usage (~2.2–2.8× inflation). Truth is deduped.
> - **"Spans land" ≠ "spans correct."** Subagent tokens once read 2.8× high while
>   looking fine. Gates are numeric reconciliation, not span presence.

---

## Ground rules

- **Isolation of the two writers.** Point native OTel at a **dedicated Langfuse
  project** (its own key pair), NOT the existing `claude-code` project. Both the
  hook (via Langfuse SDK→OTLP) and native CC telemetry emit OTLP; sharing one
  project commingles them and makes reconciliation ambiguous. Separate projects =
  each source has its own `project_id`, and the existing hook data is untouchable.
  Project in use for the native side: **`claude-code-otel`** (created 2026-07-17).
- **The two writers do not cross-wire — verified, not assumed.** Setting generic
  `OTEL_EXPORTER_OTLP_ENDPOINT`/`_HEADERS` for native CC telemetry does NOT divert
  the hook's SDK export: the Langfuse SDK constructs its `OTLPSpanExporter` with an
  explicit endpoint from its own `LANGFUSE_HOST` + keys and ignores the generic
  `OTEL_EXPORTER_OTLP_*` vars (`span_processor.py:99-107`), and the hook runs in a
  separate subprocess. Native reads the generic vars; the hook reads `LANGFUSE_*`.
  Confirm empirically anyway — see Phase 0 pre-flight.
- **No Langfuse SDK/skill install.** Native OTel is pure env-var config of Claude
  Code's own exporter. The Langfuse "AI skill" onboarding prompt is for
  SDK-instrumenting an application's code and is not used here.
- **Use `/research`, not `/briefing`, for controlled runs.** `/research` spawns
  the same searcher/reader/synthesizer subagent fan-out but has **no email side
  effect** and doesn't consume the day's briefing signals. Reserve one real
  `/briefing` for a realism check at the end.
- **Enable telemetry per-session in the shell, never in `settings.json`.** The
  exact commands are in "Enabling & disabling native telemetry" below — that block
  is the *single* source of truth; phases reference it rather than re-specifying it.
  Global `settings.json` would turn telemetry on for *every* session on the machine,
  so it can't be scoped to the spike; shell-exported env applies only to the one
  session launched from that terminal, and rollback is just closing it.
- **Blast radius.** Telemetry is additive and read-only relative to the hook: it
  does not alter the hook, the transcripts, or normal operation. The hook's 95%
  baseline cannot be perturbed by it (isolation verified above).
- **Consent gate.** Phases 1+ generate real telemetry and (for `/briefing`) real
  email. Do not enable until the user greenlights.

---

## Enabling & disabling native telemetry (the one place this is specified)

Telemetry is turned on **only in the shell of the session that runs the workload**
(`llm_wiki` for the single Phase-1 run, `otel-spike` for Phase 2/3) — never in
`settings.json`. It stays off everywhere else on the machine. Keys already exist at
`~/llm_wiki/.env.claude-code-otel` (Langfuse project `claude-code-otel`).

**Enable** — in the terminal, in the `~/llm_wiki` project dir, *before* launching
`claude`:

```bash
set -a; . ~/llm_wiki/.env.claude-code-otel; set +a   # LANGFUSE_PUBLIC_KEY / _SECRET_KEY / _BASE_URL
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1          # beta llm_request trace spans
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT="$LANGFUSE_BASE_URL/api/public/otel"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\n')"
claude          # this session (and only this one) now emits native traces
```

**Disable / rollback** — close that terminal, or in it:

```bash
unset CLAUDE_CODE_ENABLE_TELEMETRY CLAUDE_CODE_ENHANCED_TELEMETRY_BETA \
      OTEL_TRACES_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL \
      OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_HEADERS
```

Nothing persistent is touched, so there is no config to revert — the hook and every
other session are unaffected the entire time.

---

## Execution roles & sessions — who runs what, from where

Named Claude Code sessions, so it's unambiguous which is which:

- **`claude-code-langfuse-template`** — THIS session, anchored to the template repo
  (`~/source/claude-code-langfuse-template`). **Owns:** the hook code and its
  regression tests (Phase 4), the Langfuse/ClickHouse reconciliation queries, and
  the interpretation/verdicts (pass/fail, is-this-a-regression, does-a-mismatch-mean-
  the-known-bug). **Cannot run the workload** — `/research` and `/briefing` only
  work from inside the `llm_wiki` project, and this session is in the wrong
  directory. Docker/ClickHouse queries run fine from here (they're host-global).
- **`llm_wiki`** — the other collaborating session, inside the `llm_wiki` project
  (`~/llm_wiki`). **Owns:** the investigation brief, the transcript ground-truth
  (`usage-report.py` lives here), and — because it's in the project — **running the
  workload** (`/research`, `/briefing`). It holds deep context, so its thread is
  worth keeping clean.
- **`otel-spike`** (to be created — the **execution session**, decided 2026-07-17)
  — a **fresh** session launched from a terminal **in the `llm_wiki` project dir**
  with the OTEL vars `export`ed. It runs the repetitive Phase 2/3 workload so that
  neither collaborating thread (`llm_wiki`'s brief, `claude-code-langfuse-template`'s
  hook context) gets polluted with dozens of runs. It works purely from THIS
  committed plan (self-contained by design) and returns *raw* artifacts only — no
  verdicts.

**Hard constraint:** anything that runs `/research` or `/briefing` must be a session
in the `llm_wiki` project (`llm_wiki` for the single Phase-1 run, `otel-spike` for
the Phase 2/3 grind). Anything that only queries Langfuse/ClickHouse or touches hook
code runs from `claude-code-langfuse-template`.

| Phase | Run from | Why |
|---|---|---|
| **Phase 0** (baseline) | `llm_wiki` (transcript truth) + `claude-code-langfuse-template` (Langfuse baseline, hook-version check) | Split: `usage-report.py` is in the llm_wiki project; the ClickHouse baseline and `diff` of the installed hook are host-global. No telemetry yet. |
| **Phase 1** (cache-split pivot) | `llm_wiki` runs the one `/research` (OTEL vars exported in its shell); `claude-code-langfuse-template` inspects the span vs transcript | One decisive run + one span diff. Not worth a third session. `llm_wiki` unsets the vars after. |
| **Phase 2** (dual-write grind) | `otel-spike` runs; `claude-code-langfuse-template` reconciles/interprets | 3+ runs over days of repetitive querying. Offload the runs; keep interpretation in `claude-code-langfuse-template`. |
| **Phase 3** (residual correctness) | `otel-spike` runs; `claude-code-langfuse-template` judges "fixed / same gap / different gap" | Same runs as Phase 2, extra queries. |
| **Phase 4** (hook regression suite) | `claude-code-langfuse-template` | Pure code/replay against the repo, no telemetry — belongs with whoever edits the hook. |
| **Phase 5** (decision) | `claude-code-langfuse-template` + `llm_wiki` (joint) | Judgment over all gate results; interpretation, not execution. |

**Handoff format (executor → `claude-code-langfuse-template`):** raw numbers only —
per-session `T / H / N` with the disk denominator, one verbatim `llm_request` span's
attribute map, and any query that returned unexpectedly. No verdicts from the
executor; that's `claude-code-langfuse-template`'s job. This bounds the copy/paste to
numbers-in, verdict-out.

---

## Phase 0 — Baseline & ground truth (no config change)

**Executor: `llm_wiki` (transcript truth) + `claude-code-langfuse-template`
(Langfuse baseline, hook-version check).** Read-only, no telemetry.

Establish the numbers everything else is measured against, before touching anything.

A note on windows, since it's easy to trip on: there are **two** of them.
- *This* phase measures **already-existing** data, so it uses a **recent, already-
  closed** window — e.g. the last completed day or two that has subagent activity.
  Nothing here is in the future.
- Phase 2's comparison uses a **different** window that brackets the test runs you
  do *then* — you record the wall-clock time just before the first run and just
  after the last, and use those two literals. That window is "absolute" (fixed
  timestamps, never `now() - N DAY`) but it is pinned *after* the runs happen, not
  chosen ahead of time.

1. **Confirm hook is the shipped version** → verify: `diff ~/.claude/hooks/langfuse_hook.py hooks/langfuse_hook.py` is empty; `git log --oneline -1` on `main` shows the Defect-5 commit (`528c8d2`).
2. **Fix the baseline window to a recent closed period** (e.g. yesterday `00:00`→`24:00` in a literal timestamp pair), used verbatim in steps 3–4 → verify: the same two literals appear in the transcript-side and Langfuse-side commands.
3. **Ground-truth token/cost** for that window from transcripts → `python3 ~/llm_wiki/scripts/usage-report.py --since <start> --json` → verify: deduped (script header says "deduped by message.id"); record `tokens`/`cost` per bucket as the denominator.
4. **Baseline hook capture** in Langfuse for the same window, deduped by `anthropic_message_id` → verify: record `distinct_msgs` and summed `usage_details` tokens; note any raw-vs-distinct gap (leftover dup rows from earlier reprocessing) so it isn't mistaken for a capture change. Sanity: this should land near the known ~95%.
5. **Confirm the native project is reachable and empty** — keys already exist at `~/llm_wiki/.env.claude-code-otel` (project `claude-code-otel`); no telemetry enabled yet → verify: `curl` the OTLP endpoint with that key pair's Basic auth returns 2xx/4xx (reachable, authenticated), and the project shows 0 traces (clean slate).

Exit criterion: three reference numbers exist — transcript truth, hook capture
(~95%), and an empty native project ready to receive. **Ran 2026-07-17:** native
project clean (0/0); transcript tool validated; hook baseline confirmed **per
session** (`0fa25040` 95 %, `a3b656eb` 89 %). Key finding — hook `start_time` is
emission time, so all reconciliation must be **by `session_id`, not time window**
(see Measurement mechanics).

---

## Phase 1 — The pivotal cheap gate: cache-tier granularity

**Executor: `llm_wiki` runs the one `/research`** (OTEL vars exported in its shell,
unset after); **`claude-code-langfuse-template` inspects the span vs transcript.**
Not worth a third session for a single decisive run.

This is the single fact that most likely decides the whole direction, and it costs
one run. Do it before any multi-day investment.

1. **Enable telemetry in this session's shell** — run the "Enable" block from
   "Enabling & disabling native telemetry" above, then launch `claude` from that
   same terminal and do one `/research` run → verify: it produces at least one
   `claude_code.llm_request` span in the `claude-code-otel` project.
2. **Pre-flight isolation check** (moved here because it needs telemetry on) → the
   `/research` run also fired the hook, so confirm the two writers didn't cross-wire:
   hook traces still land in the **old** `claude-code` project, native spans land
   **only** in `claude-code-otel` → verify: each project's new traces come from
   exactly one source. Empirically confirms the SDK/env isolation from Ground rules.
3. **Inspect one `llm_request` span's token attributes and diff against the same
   call's transcript** → the transcript's `message.usage.cache_creation` already
   carries `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` (verified
   baseline; the hook prices 5m at 1.25× base and 1h at 2× via
   `extract_usage_details` — live fidelity today). So compare the native span's
   cache fields to the transcript's `ephemeral_{5m,1h}` for that exact call.
   - **GATE 1a (fidelity):** 5m/1h split present and matches transcript → native
     can price cache correctly, proceed. Aggregate `cacheCreation` only → native
     is a **known cost-accuracy regression**; record the magnitude and take the
     "migrate for structure, keep hook for pricing" branch in Phase 5. This may
     end the full-migration case on its own.
4. **Confirm subagent attribution exists** on the same run → verify:
   `claude_code.token.usage` metrics (or span attributes) carry `query_source`
   ∈ {main, subagent, auxiliary} and `agent.name`, and a subagent run shows
   `query_source=subagent`.
5. **Tear down** — run the "Disable / rollback" block (or just close the terminal).
   Nothing persistent was changed, so this fully reverts the config.

Exit criterion: we know whether native telemetry can express per-call, per-tier
cache tokens at all. A hard "no" here means stop and stay on the hook.

**RAN 2026-07-17 (session `089e4a80`, `/research` on a fresh topic) — GATE 1a: FAIL
(aggregate only).** The `claude_code.llm_request` span carries `input_tokens`,
`output_tokens`, `cache_read_tokens`, and **`cache_creation_tokens` as a single
aggregate — no `ephemeral_5m`/`ephemeral_1h` split anywhere** (confirmed across all
spans via `JSONExtractKeys`). This workload's cache creation is **100 % 1h**
(107,481 tokens, 0 5m) — priced 2× vs 1.25× — so the missing split is a concrete
pricing regression, not a theoretical one. Per the Phase-5 matrix this is the
"migrate for structure, keep hook for pricing" branch.

Two more findings from the same run:
- **Attribution works, but under different names than the docs.** Subagent calls
  carry `llm_request.context="tool"` + a populated `agent_id`; main-loop calls are
  `context="interaction"` with empty `agent_id`. The documented `query_source` /
  `agent.name` live on the *metrics* (`claude_code.token.usage`), which this run did
  **not** export (only `OTEL_TRACES_EXPORTER=otlp` was set). So trace-level
  attribution is present via `context`/`agent_id`.
- **Integration gap — Langfuse `usage_details` is EMPTY on native spans.** Langfuse
  maps `gen_ai.usage.*` OTel conventions; Claude Code emits `input_tokens` /
  `cache_creation_tokens` etc., which Langfuse does not auto-map. So out of the box
  native spans show **no tokens and no cost** in Langfuse — the numbers sit only in
  the raw `attributes` JSON. Using native for cost needs a custom attribute-mapping
  step, on top of the missing cache split. This *raises* the cost of even the
  "migrate for structure" branch.

---

## Phase 2 — Dual-write reconciliation (the core test)

**Executor: `otel-spike`** runs the workload; **`claude-code-langfuse-template`
reconciles and interprets.**

Both writers run for 3 `/research` runs + 1 `/briefing`, into their separate
projects. This is where "does it actually match truth" is answered.

For **each session**, compute three token totals over the same fixed set of
`message.id`s / calls:
- **T** = transcript truth (usage-report.py, deduped)
- **H** = hook capture (existing project, deduped by `anthropic_message_id`)
- **N** = native capture (dedicated project, summed from `llm_request` spans)

1. **Run 3 `/research` sessions** (spread across the window) → verify: each
   produces subagent files on disk AND traces in both projects.
2. **Per-session reconciliation** → verify:
   - **GATE 2a:** `N / T ≥ ~0.97` (native reconciles to truth within a few %).
   - **GATE 2b:** `H / T ≥ ~0.95` (hook still at its measured baseline — proves
     dual-write didn't perturb it).
   - **GATE 2c:** native has **no per-call duplication** — `count(llm_request) ==`
     distinct calls (the hook's 2.8× bug must not reappear in a different form).
3. **Per-subagent attribution is a GATE, not just presence** → verify: native
   `query_source=subagent` token sum reconciles to the deduped subagent-file total
   on disk, AND each `agent.name` maps to the **correct** subagent, checked against
   the transcript agentIds — not merely that a label exists. Attribution can go
   silently wrong (Defect 5 mislabeled `a1ff3c3b` as `agent-a1ff3c3bea612d480` via
   an id-prefix mismatch between resolve paths; now fixed in the hook). Native must
   not have an analogous silent-mislabel mode.
4. **Cost reconciliation** → verify: native `cost` (if emitted) or cost computed
   from native tokens × prices matches usage-report.py cost within a few % —
   this exercises the sonnet-5 price + cache tiers end to end.

Exit criterion: a per-session table of T/H/N with pass/fail on each gate.

---

## Phase 3 — Correctness & the residuals native should fix "for free"

**Executor: `otel-spike`** runs (same runs as Phase 2, extra queries);
**`claude-code-langfuse-template` judges** "fixed / same gap / different gap".

These test the specific failure modes that motivated the migration — the things
native is *claimed* to solve by construction. If it doesn't solve them, the
migration's main benefit evaporates.

1. **Async completeness (the 60s-quiescence residual).** On a run where a
   subagent stalls >60s mid-stream then writes a final message (the synthesizer
   `a6448660` pattern), verify: native captures that final message. Native emits
   the completion signal itself, so it should have **no** quiescence gap. This is
   the sharpest test — it's exactly what the hook can't do reliably.
2. **Turn-boundary completeness (the 18:42 parent-cluster residual + Defect 4).**
   On a long session with task-notifications splitting turns across fires, verify:
   native captures every parent LLM call (no turn-spanning loss), since native has
   no `last_line` watermark.
3. **Nesting/hierarchy** → verify: subagent `llm_request` spans nest under the
   parent's `claude_code.tool` span, producing a single connected trace (the
   delegation chain we hand-rebuild).
4. **`a1ff3c3b` subagent_id mismatch** — cross-check whether native's `agent.name`
   attribution is correct on the same session where the hook mislabeled it
   (needs the session id from the other Claude). Tests whether native sidesteps
   the description-fallback collision entirely.

Exit criterion: for each residual, a clear "native fixes it / native has the same
gap / native has a different gap."

---

## Phase 4 — Hook regression suite (protect the 95% regardless of outcome)

**Executor: `claude-code-langfuse-template`** (pure code/replay against the repo, no
telemetry; belongs with whoever edits the hook).

The hook stays for backfill even if we migrate, so its correctness must be locked
as repeatable tests, not one-off replays.

1. **Codify the replay harness as a test.** The append-only, real-fire-boundary
   replay (the one that reproduced production 28/55 pre-fix and 55/55 post-fix)
   → verify: runs green on sessions `31fffe1a`, `22dc7927`, `4b1d4ff2` — each
   100% of deduped disk messages, `rows_per_msg == 1.00`, 0 duplicates,
   0 pending leftover, re-sweep is a no-op.
2. **Defect-4 fixture test** — a minimal transcript where a turn's opening user
   message and its assistant reply fall in different fire windows → verify: the
   turn is captured (not dropped) with the fix, dropped without it.
3. **Queue-path state-preservation test** — simulate a fire on the
   Langfuse-unreachable path with existing `emitted_subagents`/`pending_subagents`/
   `open_user` in state → verify: all three survive the state write unchanged.
4. **Native span-schema assertion** — pin the **observed** (2026-07-17)
   `claude_code.llm_request` attribute names, which live in the span's raw
   `attributes` JSON, NOT in Langfuse `usage_details`: `input_tokens`,
   `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (aggregate — no
   5m/1h), `llm_request.context` (`interaction`/`tool`), `agent_id`. → verify: the
   attributes exist under these names. Native traces are beta ("span names and
   attributes may change between releases"); a silent CC rename must fail this
   assertion, not be assumed away — the same drift class that broke the transcript
   hook. (The documented `query_source`/`agent.name` are metrics attributes, a
   separate export path.)
5. **Existing suite** → verify: `pytest tests/test_hook_unit.py` and
   `tests/test_syntax.sh` pass; installed copy byte-identical to repo.

Exit criterion: `make test` (or equivalent) green; these run on every hook change.

---

## Phase 5 — Decision, rollout, rollback

**Executor: `claude-code-langfuse-template` + `llm_wiki` (joint)** — judgment over
all gate results; interpretation, not execution.

**Decision matrix** (read gates from Phases 1–3):

| Gate 1a (cache split) | Gate 2a (native≈truth) | Phase 3 residuals | Decision |
|---|---|---|---|
| pass | pass | native fixes them | **Migrate.** Hook → backfill-only; stop building residuals. |
| pass | pass | native has same/other gaps | **Migrate for capture, keep hook logic** until native residuals close. |
| **aggregate only** | pass | either | **Migrate for structure, keep the hook (or a cost-only shim) for pricing.** Native gives the trace tree + attribution, but the hook (which prices `ephemeral_5m` at 1.25× and `ephemeral_1h` at 2× from the transcript's `cache_creation` split — live fidelity today) remains the cost source. Don't lose the tier split. |
| fail (no cache at all) | either | either | **Stay on hook.** Native can't price cache; build the two residual fixes. |
| pass | fail | either | **Stay on hook.** Native doesn't reconcile; investigate before revisiting. |

**If migrate:**
- Keep the hook installed for pre-cutover backfill (native is forward-only).
- Run native as the sole *new* source for a further window; re-run Phase 2 gates
  as a burn-in before removing any hook token logic.
- Only then retire the hook's token-aggregation path (leave any non-telemetry
  hook logic intact).

**If stay:** build the two residuals with their own verify checks —
(a) completion-aware subagent emit (replace bare 60s quiescence with a
completion signal), verify: the `a6448660`-style late-final-message is captured;
(b) close the turn-cluster parent-drop, verify: the 18:42-style cluster is
captured on replay.

**Rollback (either way, if the spike misbehaves):** run the "Disable / rollback"
block or close the test session's terminal — nothing persistent was changed, so
there is no config to revert. The `claude-code-otel` project can be left dormant or
deleted. The hook is unaffected throughout; it never depended on any of this.

---

## Measurement mechanics (the part that's bitten us)

- **Distinguishing sources:** separate `project_id` per writer (dedicated native
  project). Never reconcile by eyeballing one commingled project.
- **Dedup, always:** `uniqExact(metadata['anthropic_message_id'])` for the hook;
  distinct call/span id for native. Report raw-vs-distinct so silent duplication
  is visible.
- **Slice by `session_id`, NOT by time window** (Phase-0 finding, 2026-07-17). The
  hook stamps a generation's `start_time` at *emission*, not API-call time
  (verified: `start_day == ingest_day` for every hook gen), so a `start_time`
  window silently sweeps in historical re-emissions — a "since 2026-07-16" window
  read 376 M hook tokens against 159 M of transcript truth purely from the earlier
  reprocessing. Reconcile each session by `metadata['session_id']` (hook) /
  `session.id` (native) / the session's transcript files (truth). Per session the
  numbers are sane: `0fa25040` = 95 % (95/100 msgs), `a3b656eb` = 89 % (186/209,
  a long interactive session hitting the known residuals). Time-windowing is only
  safe for native, whose `start_time` is real API time — but slice it by session
  too, so all three sides compare the same population.
- **`FINAL` on every ClickHouse read** (ReplacingMergeTree).
- **Purge the re-emission duplicates before trusting aggregates.** The
  investigation's reprocessing left duplicate rows keyed by distinct observation id
  but same `anthropic_message_id`; per-session dedup-by-message-id hides them, but
  any cross-session aggregate is contaminated until they're purged.
- **Token definition:** input + output + all cache tiers, excluding the derived
  `total` key — identical on all three sides so the ratios mean something.
