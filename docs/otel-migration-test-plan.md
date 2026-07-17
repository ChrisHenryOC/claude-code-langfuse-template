# Native OTel vs. transcript-hook — test plan

> Status: DRAFT for two-Claude consensus. No config changed yet. Author: Claude
> (opus). Companion to `token-capture-gap-investigation.md`.
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
- **Auth:** Langfuse OTLP uses HTTP Basic auth —
  `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public_key:secret_key)>"`.
  The key pair alone selects the project.
- **Use `/research`, not `/briefing`, for controlled runs.** `/research` spawns
  the same searcher/reader/synthesizer subagent fan-out but has **no email side
  effect** and doesn't consume the day's briefing signals. Reserve one real
  `/briefing` for a realism check at the end.
- **Blast radius.** Enabling telemetry is additive env vars in `settings.json`;
  it does not alter the hook or normal operation and is reversed by removing the
  vars. Rollback is in Phase 5.
- **Consent gate.** Phases 1+ change the user's `settings.json` and generate real
  telemetry. Do not enable until the user greenlights.

---

## Phase 0 — Baseline & ground truth (no config change)

Establish the numbers everything else is measured against, before touching anything.

1. **Confirm hook is the shipped version** → verify: `diff ~/.claude/hooks/langfuse_hook.py hooks/langfuse_hook.py` is empty; `git log --oneline -1` shows the Defect-4 commit.
2. **Pick a fixed test window** (e.g. `2026-07-18 00:00` → `2026-07-21 00:00`), written into every query below as a literal → verify: same two timestamps appear in the transcript-side and Langfuse-side commands.
3. **Ground-truth token/cost** for the window from transcripts → `python3 ~/llm_wiki/scripts/usage-report.py --since 2026-07-18 --json` → verify: deduped (script header says "deduped by message.id"); record `tokens` and `cost` per bucket as the denominator.
4. **Baseline hook capture** in Langfuse for the window, deduped by `anthropic_message_id` → verify: record `distinct_msgs` and summed `usage_details` tokens; note any raw-vs-distinct gap (leftover dup rows from earlier reprocessing) so it isn't mistaken for a capture change.
5. **Dedicated native-OTel Langfuse project** (`claude-code-otel`), capture its public/secret keys → verify: `curl` the OTLP endpoint with the new key's Basic auth returns 2xx/4xx (reachable, authenticated), and the project shows 0 traces (clean slate).
6. **Pre-flight isolation check** — after enabling native telemetry (Phase 1), fire the hook once and confirm hook traces still land in the **old** `claude-code` project, and native spans land only in `claude-code-otel` → verify: each project's new traces come from exactly one source (no hook traces in the native project, no native spans in the hook project). This empirically confirms the SDK/env-var isolation noted in Ground rules.

Exit criterion: three reference numbers exist — transcript truth, hook capture, and an empty native project ready to receive.

---

## Phase 1 — The pivotal cheap gate: cache-tier granularity

This is the single fact that most likely decides the whole direction, and it costs
one run. Do it before any multi-day investment.

1. **Enable native telemetry** (dedicated project) → add to `settings.json` env:
   `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`,
   `OTEL_TRACES_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`,
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel`,
   `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk:sk)>"` → verify:
   a fresh `claude -p` run produces at least one `claude_code.llm_request` span in
   the new project.
2. **Inspect one `llm_request` span's token attributes and diff against the same
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
3. **Confirm subagent attribution exists** on the same run → verify:
   `claude_code.token.usage` metrics (or span attributes) carry `query_source`
   ∈ {main, subagent, auxiliary} and `agent.name`, and a subagent run shows
   `query_source=subagent`.

Exit criterion: we know whether native telemetry can express per-call, per-tier
cache tokens at all. A hard "no" here means stop and stay on the hook.

---

## Phase 2 — Dual-write reconciliation (the core test)

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
4. **Native span-schema assertion** — pin the expected `claude_code.llm_request`
   attribute names (token fields incl. the cache-tier breakdown, `query_source`,
   `agent.name`) in a test that reads one recent native span → verify: the
   attributes exist under the expected names. Native traces are beta ("span names
   and attributes may change between releases"); a silent CC attribute rename must
   fail this assertion, not be assumed away — the same drift class that broke the
   transcript hook.
5. **Existing suite** → verify: `pytest tests/test_hook_unit.py` and
   `tests/test_syntax.sh` pass; installed copy byte-identical to repo.

Exit criterion: `make test` (or equivalent) green; these run on every hook change.

---

## Phase 5 — Decision, rollout, rollback

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

**Rollback (either way, if the spike misbehaves):** remove the telemetry env vars
from `settings.json`; the dedicated Langfuse project can be left dormant or
deleted. The hook is unaffected throughout — it never depended on any of this.

---

## Measurement mechanics (the part that's bitten us)

- **Distinguishing sources:** separate `project_id` per writer (dedicated native
  project). Never reconcile by eyeballing one commingled project.
- **Dedup, always:** `uniqExact(metadata['anthropic_message_id'])` for the hook;
  distinct call/span id for native. Report raw-vs-distinct so silent duplication
  is visible.
- **Fixed windows:** literal `start_time >= '2026-07-18 00:00:00' AND start_time <
  '2026-07-21 00:00:00'` on the Langfuse side; `--since 2026-07-18` (and window
  end) on the transcript side. No relative intervals.
- **`FINAL` on every ClickHouse read** (ReplacingMergeTree).
- **Token definition:** input + output + all cache tiers, excluding the derived
  `total` key — identical on all three sides so the ratios mean something.
