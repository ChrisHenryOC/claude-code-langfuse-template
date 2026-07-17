# Langfuse token-capture gap — investigation brief

> Date: 2026-07-16 (rewritten after root-cause found). Author: Claude (llm_wiki
> session). Audience: whoever maintains the Claude Code → Langfuse hook.
>
> **Scope: TOKEN CAPTURE, not cost.** The cost half (Sonnet 5 unpriced) is routine
> and already handled — see the "Cost note" at the bottom. The real problem: **subagent
> (searcher/synthesizer) generations stopped reaching Langfuse**, so their tokens are
> invisible regardless of price.
>
> **Status: ROOT CAUSE FOUND AND VERIFIED.** This is a rewrite. The first version of
> this brief ranked the wrong cause first (description-key mismatch); that hypothesis
> is disproven below. The verified cause is a **timing race** between subagent-transcript
> flushing and the Stop hook's incremental processing. The evidence chain is reproducible.

---

## TL;DR

> [!warning] STATUS (2026-07-17): three defects fixed; **a fourth found by the FIRST LIVE run.**
> Parts 1 + 2 shipped (`transcript_path` threaded; sweep-primary completion capture;
> `message.id` merge). **Replay** claimed 100 % capture — but the first **live** `/research`
> run (session `31fffe1a`) captured only **28/55 messages (51 %), 47 % tokens.** Merge is
> confirmed live (rows/msg = 1.00 both paths, 0 dupes) and captured subagents are complete,
> but **a whole turn was dropped — its parent gen AND its 3 subagents.** Verified: 44 parent
> messages on disk, 42 in Langfuse; the two missing include `msg_…gsXJrb`, the reader turn's
> own parent gen. So **Defect 4 = turns spanning a fire boundary are dropped (parent + subagents)**,
> a general turn-loss bug that **predates all the async work** and was merely *exposed* by it:
> `process_transcript` rebuilds `current_user` from scratch each fire but only sees
> `[last_line→EOF]`, so a turn that opened on a user record (here a searcher task-notification)
> consumed by an *earlier* fire has `current_user=None` → the finalize guard skips → `last_line`
> advances past the whole turn. (My first-pass "agentId straddle / registration miss" was wrong —
> the tool_use/spawn-ack were adjacent and resolvable; the turn never ran `create_trace` at all.)
> **Fix (installed):** persist the open turn's user across fires (`open_user` in state), restore
> next fire. Controlled replay at the 10 real fire boundaries: 28/55→**55/55**, 5/8→8/8 subagents,
> parent gen recovered, 0 dupes. Also fixed the **queue-path clobber** (line ~1449) that wiped
> `emitted`/`pending`/`open_user`. **Open:** live re-measure — the gate must now check
> **parent-message parity too**, not just subagents. **Replay has overstated every time — only a
> live run settles it.**

**Root cause (2026-07-16, after five revisions):** the dominant cause is a
one-line defect, **not** the timing race the body of this brief was written around.

`process_transcript` finalizes the last open turn of each fire through a `create_trace(...)`
call at **line ~1137 that omits `transcript_path`**. That makes `discover_subagents(None)`
return `{}` → `subagent_count = 0` → **no subagent expansion, ever**, on that turn — even
when every subagent `.jsonl` is already on disk. Under incremental firing (`Processed 1
turns from 6 sessions`), the "last open turn" is *most* turns, so nearly every turn takes
the no-path flush.

**Production trace metadata for session `4b1d4ff2` proves this over the race:** the turn
holding all 7 searcher Agent tool_uses (turn 2) and the turn holding the synthesizer
(turn 7) were both emitted with **no `transcript_path` and `subagent_count = 0`**, while
sibling turns 1/3/5 at the same epoch recorded `subagent_count = 7` — i.e. the subagent
files *were present*, and the Agent turns were dropped anyway because they took the
line-1137 path. A controlled replay flipping only that keyword: **0 → 82** subagent gens.

**There are THREE distinct defects; the line-1137 fix only addresses the first:**

1. **No-`transcript_path` flush (line 1137)** — the turns holding Agent tool_uses never
   *attempt* expansion. Fixed by threading `transcript_path` (one-liner). Ship first.
   **Confirmed fixed live** (see "Measured" below).
2. **Incomplete-file replay** — even once expansion is attempted, `emit_subagent_span`
   reads whatever is on disk *at fire time*. The `.jsonl` files are **created at spawn and
   appended to until the subagent finishes**; the hook fires while they're partial. For
   session `4b1d4ff2`, only **38 of 86 subagent records (44 %) existed at the 06:01:09 fire**.
3. **`emit_subagent_span` never merges by `message.id`** — line ~520 builds `asst_msgs`
   as a *raw per-record* list and emits one gen per record, while the parent path merges
   via `merge_assistant_parts`. Since Claude Code writes one record per content block each
   repeating the full `message.usage`, subagent gens **and** the `subagent_total_*_tokens`
   in `span_meta` (lines ~523–529) are inflated ~2.2–2.8×. This is the *same bug class* as
   the `usage-report.py` over-count, now inside the hook. **It must ship with Part 2** —
   both touch `emit_subagent_span`, and (below) it silently poisons the validation gate.

So the earlier "file absent" race is **refuted** (all 7 files existed at the first fire —
that's why turn 1 shows `subagent_count=7`); the real residual is **partial content**, and
it is *measured, not speculative*. **Predicted post-1137-fix *subagent-record* capture for
a session like this ≈ 44 %** (overall session token capture stays ~93 %, since parents
dominate and are captured in full). **Treat ~45 % subagent capture as the fix working
*exactly as expected*, not as failure — a reading near 100 % would mean something else is
wrong.** Because that shortfall is predictable and is the majority of subagent tokens, the
completion fix (a **sweep** that emits each subagent file once it's complete — *not* the
now-dead "expand on `tool_result`" plan, since the Agent tool went async and its tool_result
is a spawn ack) is **not conditional**; it's the second half of the fix. State-tracked
exactly-once is prerequisite (deterministic observation IDs are impossible on SDK v3).

The mechanism-level evidence chain that follows (ingestion times, incremental `last_line`)
is valid *data*; Section 5's original mtime timeline was a **misread** (last-append vs
creation) and has been corrected in place.

### Measured — Part 1 live, on a fresh `/research` run (session `22dc7927`, 2026-07-16)

Part 1 was applied to the live hook and a fresh subagent-spawning `/research` run measured:

- **Part 1 confirmed:** all 9 Agent-turn traces now carry `transcript_path` (`has_path=YES`,
  was `NO`); subagents are discovered (`subagent_count` 3→8) and expanded. Capture moved
  **off zero**.
- **Message capture: 9 of 69 distinct subagent messages (13 %)** — low because research
  turns finalize instantly, so `emit_subagent_span` reads the files while very incomplete
  (Defect 2, made visible; more severe than the briefing's 44 % because of faster fan-out).
- **True *token* capture: 7.9 %, not 13 %** (Defect 3). Langfuse recorded 1.056M subagent
  tokens across 25 gen rows for those 9 messages; deduped by `message.id` that's **0.386M**,
  against **4.876M** true subagent tokens on disk → 0.386 / 4.876 = **7.9 %**. The
  message-count metric flatters capture because the recorded tokens are simultaneously
  *incomplete* (9/69 messages) and *inflated* (2.78× per unmerged records).

Row-per-message asymmetry in the same session, which isolates Defect 3 cleanly:

| path | distinct msgs | gen rows | rows/msg |
|---|--:|--:|--:|
| parent | 21 | 21 | 1.00 |
| subagent | 9 | 25 | 2.78 |

### Built & verified — Part 2 sweep (replay against production sessions)

The sweep + `message.id` merge were built and installed; replay verification:

| check | result |
|---|---|
| rows per message (Defect 3) | **2.78 → 1.00** |
| emitted tokens vs deduped disk | **4.876M == 4.876M**, exact |
| subagents mid-flight | **0 emitted, 8 deferred** — nothing partial |
| after completion, sweep | **69/69 and 44/44 messages (100 %, was 13 %)** |
| duplicates | **0** |
| second sweep | **no-op** |

Repo tests pass; live hook ran clean end-to-end. **But replay ≠ live:**

> [!warning] First LIVE run (session `31fffe1a`, v2.1.212) — 51 %, not 100 %
>
> | check | live result |
> |---|---|
> | subagent message capture | **28/55 = 51 %** |
> | token capture vs deduped disk (3.257M) | 1.535M = **47 %** |
> | rows/msg parent / subagent | 1.00 / 1.00 ✓ (merge holds live) |
> | duplicates | 0 ✓ |
> | captured subagents | complete (2/2, 5/5, 7/7, 12/12, 2/2) ✓ |
>
> **A whole turn was dropped — parent gen AND its 3 subagents** (the reader batch "Extract
> source cards A/B/C"). Verified: 44 parent messages on disk vs 42 in Langfuse; the two missing
> include `msg_…gsXJrb`, the reader turn's own parent gen. So this is **general turn-loss, not
> subagent-specific**, and it **predates all the async/sweep work** — async only exposed it.
>
> **Defect 4 — turns spanning a fire boundary are dropped.** `process_transcript` rebuilds
> `current_user` from scratch each fire but only sees `[last_line→EOF]`; a turn that opened on a
> user record (here a searcher task-notification) already consumed by an earlier fire has
> `current_user=None` → the `if current_user and current_assistants` finalize guard skips it →
> `last_line` advances past the entire turn. (An earlier draft here blamed an "agentId straddle /
> registration miss" — **wrong**: the tool_use/spawn-ack were adjacent and resolvable; the turn
> never ran `create_trace` at all.) **Fix (installed):** persist the open turn's user across fires
> (`open_user` in state). Replay at the 10 real fire boundaries: 28/55→**55/55**, parent gen
> recovered, 0 dupes. Also fixed: the queue-path state write (line ~1449) that wiped
> `emitted`/`pending`/`open_user`. Live re-measure pending — **check parent-message parity too.**

Two further limitations to record:

> [!note] Scope + tuning caveats
> - **New sessions only.** The sweep helps sessions going forward; sessions whose Agent turns
>   are already behind the `last_line` watermark are never revisited, so **the historical gap
>   (pre-fix) stays a gap** — it can only be backfilled by a separate one-off reprocessing pass.
> - **The 60 s quiescence threshold is a judgment call, not a measured value.** A subagent is
>   emitted only once its transcript is 60 s unmodified *or* a `<task-notification>` proves
>   completion. It's deliberately conservative because an early emit is **unrecoverable**
>   (no upsert on SDK v3). Failure mode: a subagent that stalls >60 s mid-run then resumes
>   would be emitted partial. Left to be tuned against the first real briefing rather than blind.

---

### Original (superseded) framing — kept for the evidence, not the conclusion

Subagent generations vanish because of a **timing race**, not a matcher bug:

- The Stop hook fires every few seconds, processes only new transcript lines
  `[last_line → EOF]`, emits generations, then advances `last_line` to EOF **and never
  looks back**.
- Subagent expansion (`emit_subagent_span`) requires the subagent's
  `subagents/agent-<id>.jsonl` to exist — but that file is flushed only when the
  subagent **finishes**, 5–85 s *after* the parent writes the `Agent` tool_use line.
- The fires land in that gap. They finalize the Agent-tool_use turns while the `.jsonl`
  files don't yet exist, emit the **parent** gens (which don't need those files), skip
  subagent expansion, and move `last_line` past the turns. When the files finally land,
  the hook is already past them. The subagent tokens are lost permanently.

Parent capture is **complete and correct**. Subagent capture is **zero**. That asymmetry
is the signature — but per the correction above, the operative reason the Agent turns
skip expansion is the line-1137 no-`transcript_path` flush, not (in the measured session)
file-flush timing.

**Scope of the impact (deduped by `message.id` — see the warning below).** Fixing this
recovers the subagent tokens, which are **~12 % of true tokens corpus-wide** (~25 % on a
subagent-heavy briefing session), **not** the ~61 % the first brief implied. That old
figure came from summing un-deduped transcript records (a ~2.3× over-count); most of the
apparent shortfall was measurement error, not lost subagents. The race is still a real,
worth-fixing defect — it just accounts for an eighth of the tokens, not the bulk.

> [!warning] Live Langfuse data is currently contaminated — clean before any before/after
> Something reprocessed heavily today (2026-07-16): **12,786 generations against 8,483
> distinct `anthropic_message_id`s = ~1.5 duplicate copies each**, and the llm-wiki 14-day
> slice ballooned from 717 → 3,726 gens within one working session (the `max_sessions=10`
> cap is now being hit repeatedly on a drain backlog). This is **live proof that
> re-emission duplicates today** — and since deterministic observation IDs are impossible on
> SDK v3 (see part 0), the sweep's exactly-once lives entirely on its **state file**; if that
> state is lost, this contamination recurs and only a purge fixes it. **No validation is
> meaningful until the duplicate rows are purged** from ClickHouse.

---

## Evidence chain (valid data; see the TL;DR correction for the operative cause)

All numbers below are from **one session**: today's `/briefing`, session
`4b1d4ff2-758c-4a7a-9cc2-698f8eb46665`, project `-Users-chrishenry-llm-wiki`. The
ingestion-time / mtime / incremental-`last_line` evidence here is sound; the conclusion it
was originally used to support (race as primary) is superseded by the line-1137 finding.

### 1. Ground truth (transcripts)

| | value |
|---|--:|
| Parent assistant **records** carrying `usage` | 118 (one record per content block — see note) |
| Parent **distinct** `message.id` (= real API calls) | **50** |
| `Agent` tool_uses in the parent turn stream | 8 |
| Subagent files (`agent-*.jsonl`) | 8 |
| Subagent assistant records across those files | 86 |

> [!warning] Token counts must be deduped by `message.id`
> Claude Code writes **one JSONL record per content block**, and each record repeats the
> **full** `message.usage`. A single API response with `thinking + text + 7×tool_use` is
> 9 records, all stamped with the same usage — e.g. `msg_011Cd5n77V…` here: 9 records,
> each `(out=4344, in=2, cache_read=58258)`. Summing records therefore over-counts by the
> average blocks-per-message. For this session: parent **2.32×** (13.9M naive → 6.0M
> deduped), subagent **1.90×** (3.8M → 2.0M). Corpus-wide (14 d) the inflation is ~**2.27×**.
> **Every token figure in any comparison must dedup by `message.id` first.** The hook
> already does this correctly (one gen per distinct id → 50, not 118); the *transcript-side*
> accounting did not.

### 2. What Langfuse captured for this session

**50 parent gens, 0 subagent gens.** (No `subagent_type`-tagged observations; no
subagent-only-model observations.)

### 3. The code is correct — a full-transcript replay proves it

A replay harness (below) runs the **exact installed** `process_transcript` /
`create_trace` / `emit_subagent_span` against this session's transcript, with a mock
Langfuse client that counts generations instead of emitting them, `last_line = 0`
(whole transcript at once), run **now** (all subagent files present):

```
turns = 7
parent gens emitted:   50   (= 50 distinct parent message ids)   ← matches production exactly
subagent spans opened:  7   (of 8 Agent tool_uses — the 8th is lost to a SECOND bug, below)
subagent gens emitted: 82   ← production emitted 0
```

So the emitter, the description-matcher, and the discovery logic all **work**. The delta
between replay (82 subagent gens) and production (0) is entirely *when* the code ran
relative to file flushing.

> [!note] Second, independent bug: the final turn loses `transcript_path`
> The replay opened only **7** of 8 Agent spans even with all files present. The 8th is the
> synthesizer, which lives in the session's final turn. `process_transcript` finalizes the
> last open turn through a `create_trace(...)` call (line ~1137) that **omits
> `transcript_path`**, so `discover_subagents(None)` returns `{}` and that turn's subagents
> are never expanded — regardless of the race. This bites the last turn of *every* fire's
> processing window. It's smaller than the race but has the same fix surface: ensure
> `transcript_path` is threaded to the final-turn call (and it's another reason the backfill
> sweep below is the robust remedy — it doesn't depend on any turn-time path being correct).

### 4. Production ran incrementally, in the flush gap

Langfuse **ingestion** times (`observations.created_at`) for this session's 50 parent
gens cluster into four batches that match the hook-fire log line-for-line:

| Ingested (local) | parent gens | matching fire |
|---|--:|---|
| 06:01:09 | 7 | `Processed … 06:01:09` |
| 06:01:30 | 5 | `Processed … 06:01:30` |
| 06:02:10 | 4 | `Processed … 06:02:10` |
| 06:09:30 | 34 | `Processed … 06:09:30` |

The session was processed **incrementally across four fires at 06:01–06:09**, not in one
late pass. (The `updated: 17:21` in the state file is a later no-op re-touch; the
transcript's final line is 06:09:29 and the last batch ingested at 06:09:30.)

### 5. The residual is **incomplete** subagent files, not **absent** ones

> [!warning] Correction — an earlier version of this section misread mtime as "file landed."
> The subagent `.jsonl` files are **created at spawn** (birth times 06:00:39–06:00:59, same
> instant as their `.meta.json`) and **appended to** until the subagent finishes; the
> 06:01:04–06:02:24 values are **last-append mtimes**, not creation times.
> `discover_subagents` only checks `.exists()`, so all 7 were discoverable ~10 s before the
> first fire — which is exactly why turn 1 recorded `subagent_count=7`. **The "file absent"
> race is refuted.**

The real residual is **partial content**: `emit_subagent_span` replays whatever records are
on disk *at fire time*, and the searchers wrote until 06:02:24 while turn 2 was finalized at
06:01:09. Counting subagent assistant records with `timestamp ≤ 06:01:09`:

```
present at the 06:01:09 fire:  38 of 86 records  = 44%
```

So even **with** the line-1137 fix (turn 2 emitting with `transcript_path`, discovering all
7 searchers), it would read incomplete files and capture ~**44%** of this session's subagent
records. **This is the predicted post-fix number and the success criterion — ~45%, not
~100%.** A post-fix reading near 100% would mean something *else* changed, not that the fix
over-performed. The ~56% shortfall is real, predictable, and the majority of subagent tokens
— which is why the completion fix below is not optional.

### 6. Why the failure is silent

The one place that would log a subagent problem — the `except` around
`emit_subagent_span` in `create_trace` — calls `debug(...)`, and `debug()` is gated on
`CC_LANGFUSE_DEBUG`, which is `"false"` in `~/.claude/settings.json`. A non-match doesn't
even reach that `except`; it falls through to a leaf span with no log at all. **Log
silence here is not evidence of "no error" — those lines are unwritable by config.**

---

## Post-mortem of the two prior diagnoses

**First brief — "description-key mismatch (most likely)": WRONG, dropped.** Matching
works: `discover_subagents` returns 8/8 for this session standalone, and an independent
check across the corpus found 176/176 `tool_use.input.description` values matching a
`meta.json` key. (The replay's 7/8 is the separate final-turn `transcript_path` bug noted
above, not a match failure.) The matcher is not what broke.

**First brief — "expansion succeeds ~6% of the time / chronic fragility": WRONG SHAPE.**
It's not chronic; it's a **hard cliff**. The last subagent generation ever recorded is
**2026-07-07 04:24:36**; zero since. The all-project subagent stream (peak 7,280 gens on
2026-06-10) fell to zero after July 7. Within the narrower **llm-wiki slice** it was
already near-zero through late June with a lone 55-gen spike on July 7 — so for that
slice the "14-day window" was measuring the tail of a dead feature, which is what read as
"6%." Pin all windows to **absolute dates**, not `now() - INTERVAL 14 DAY`.

**Reviewer critique — "turn extraction advances `last_line` without emitting": RIGHT
MECHANISM, mis-scoped.** `last_line` advancing past unfinished work is exactly it. But it
is **subagent-specific**, not a global turn-skip: parent gens emit fine (50/50 here),
because only subagent expansion depends on a file that lands after the fire. The earlier
"parents are healthy in aggregate" and the interim "parents are also dropped (118→50)"
were both artifacts — 118 is **one record per content block**, not real API calls; the 50
distinct message ids are fully captured.

Net correction to the record: **it was never the matcher, never a max_sessions cap, and
parents were never lossy. It is a flush-vs-fire race that strands the subagent `.jsonl`
behind an advancing watermark.**

---

## The cliff — the Agent tool went ASYNC (late June), VERIFIED

The cliff is a **Claude Code behavior change**, not the hook, and not a cadence shift
through line 1137. The Agent tool switched from **synchronous to asynchronous** execution,
and that is visible directly in the transcript's Agent `tool_result` payload:

| Claude Code | Agent `tool_result` contains |
|---|---|
| **≤ v2.1.178** (verified 06-17…06-20; sync) | the subagent's **real output** — 5–24 KB of structured results |
| **≥ ~v2.1.196 → v2.1.211** (verified 07-16; async) | **"Async agent launched successfully"** — a 1,114-byte spawn ack |

**Mechanism:** while sync, the parent **blocked** on the subagent — the `tool_result` *was*
the result, so the Agent turn could not finalize until the subagent had finished writing.
Expansion therefore always read a **complete** file. That is the historical 33,393 gens.
Once async, the parent fires the Agent and continues; the turn finalizes **at spawn**, when
the subagent's `.jsonl` is still near-empty → capture collapses.

**Line 1137 did not cause the cliff.** It has existed since May 9 and coexisted with capture
working fine through 06-26 (because sync execution finalized the Agent turns *late*, after
completion). It is a real *chronic* defect — worth fixing — but orthogonal to the cliff.

**Date:** the boundary is ~**v2.1.196 (~06-27/29)**, not 07-07; the early-July traffic is
stragglers, and version is **not monotonic** across sessions (some 07-01 / 07-12 sessions
ran older builds). Pin any bisect to the **`version` field in the transcript**, not the date.

---

## Remediation (for the maintainer)

Matcher work is irrelevant; the parent emitter is correct. There are **three** defects.
Ship part 1, verify capture reaches the *predicted* ~44 % (message count), then ship part 2
**together with the Defect-3 merge** — they touch the same function and Defect 3 poisons the
validation gate if left in.

### Part 1 — Thread `transcript_path` into the final-turn flush (one-liner, ship first)

`process_transcript`'s final-turn `create_trace(...)` (line ~1137) omits `transcript_path`
that the mid-loop call (line ~1099) passes. Add it. This is the confirmed dominant cause in
the measured session (Agent turns currently never even *attempt* expansion). After it lands,
**run one briefing and measure subagent-record capture** (spans emitted ÷ subagent records
in the transcript):

- **~44 %** → working exactly as predicted. The remaining loss is incomplete-file replay
  (part 2), not a regression. Proceed to part 2.
- **~near 100 %** → unexpected; something *else* changed — investigate before proceeding.
- Also thread `transcript_path` for `derive_release()` on that path (traces from the no-path
  flush likely have a null/default `release` — a cheap confirming signal that part 1 landed).

### Part 2 — Sweep-primary completion capture + Defect-3 merge

> [!warning] The original "expand on Agent `tool_result` arrival" plan is DEAD — do not
> build it. Now that the Agent tool is **async** (see the cliff section), the `tool_result`
> is a 1,114-byte spawn ack that arrives *at spawn*, when the subagent file is empty.
> Triggering on it would read a zero-record file — **worse than today's ~8 %.**

The capture mechanism must depend on **no parent-side signal**, because Claude Code has now
changed Agent semantics underneath the hook twice. That means the **sweep is the primary
mechanism** (§1 below): glob the subagent files and emit them once they're complete,
independent of what the parent turn does. Ship it together with:

- **A completeness check before emitting** — only emit a subagent whose `.jsonl` looks
  finished (e.g. a terminal/result record present, or mtime stable for N seconds). This is
  what actually closes the incomplete-file residual; the sweep provides the retries.
- **`<task-notification>` as an optional *fast path*, not the mechanism.** These user records
  carry `<tool-use-id>` = the Agent tool_use id (the direct id linkage the original brief
  wanted — no description matching) and a `completed` status at the subagent's file-completion
  time. But coverage is **unreliable**: verified **3/8** in briefing `4b1d4ff2` vs **8/8** in
  research `22dc7927`, *same* CC version. Use it to emit promptly when present; never depend
  on it for completeness — the sweep backstops the misses.
- **Merge by `message.id` (Defect 3):** build `asst_msgs` (line ~520) by merging records that
  share a `message.id` — mirror the parent path's `merge_assistant_parts` — so one gen is
  emitted per message, not per content-block record. Also fix `subagent_total_*_tokens` in
  `span_meta` (lines ~523–529), which sum the same unmerged records.

Do **not** ship any completion capture without the merge: once message capture approaches
100 %, unmerged subagent tokens read ~2.2–2.8× high, so the validation gate ("≥95 % of
transcript tokens") would *pass or overshoot while wrong* — the bug hides itself at exactly
the moment you'd use the gate to declare victory. State-tracked exactly-once is prerequisite
(part 0) — deterministic IDs are impossible on SDK v3, so the state file is the *only* dedup.

### 0. Exactly-once via state — deterministic observation IDs are IMPOSSIBLE on SDK v3

> [!warning] An earlier draft made "deterministic observation IDs (re-emission upserts)" a
> prerequisite. **That cannot be built.** Verified against `langfuse==3.15.0`:
> `start_observation` takes **no `id`** parameter, `TraceContext` is only
> `{trace_id, parent_span_id}`, and observations are OTel spans with SDK-minted ids. The
> only deterministic helper is `create_trace_id(seed=)` — **traces, not observations.** So
> there is no upsert; a re-emitted observation is always a *new row*. Do not send the next
> person hunting this feature.

The sweep must therefore guarantee exactly-once **from its own state**: track emitted
`agent_id`s (and message ids) in the state file and never re-emit. The honest consequence:
**if the state file is lost, duplicates reappear and only a ClickHouse purge fixes them** —
which is exactly what produced today's contamination (~1.5 duplicate copies per message).
Keep the purge in the runbook; it is the *only* remedy for state loss. Purge the existing
duplicate rows before trusting any count.

### 1. Decoupled subagent backfill sweep — THE primary mechanism (part 2's core)

This is the completion mechanism, not an alternative — it is the only approach that depends
on no parent-side signal, which is the property that survives Claude Code changing Agent
semantics again. A pass, independent of turn processing, that runs on each fire (and/or one
short-delayed pass). Because subagent transcripts are immutable and self-contained,
reconciliation after the fact is race-free:

- For the session(s) touched this fire, glob **every** `subagents/agent-*.jsonl` under it
  — not just those referenced in the new-lines window.
- Track already-emitted `agent_id`s in the state file (cheap; avoids a Langfuse query per
  fire). Emit any not-yet-emitted subagent whose `.jsonl` now exists.
- **Exactly-once is on the state file, not on upsert** (see part 0 — upsert is impossible).
  The emitted-set tracking *is* the dedup; there is no second line of defense, so state loss
  → duplicates → purge.
- **Id linkage came free from the async change:** the spawn-ack `tool_result` carries
  `agentId: <id>`, so subagents resolve **by id** parsed from the tool_result (description
  matching as fallback) — the original brief's "match by `agent_id`, not description" ask,
  delivered by the same change that broke everything else. No `{agent_id → trace_id}` map was
  needed for linkage.
- **Attach to the right trace:** record the spawning turn's `trace_id` with the pending
  subagent so the sweep emits its gens into that **same trace** (via `TraceContext`), nesting
  under the turn that spawned it even when the subagent finishes on a later fire.

Effect: the searcher that finishes writing at 06:01:15 is picked up by the 06:01:30 (or
06:09:30) fire and attached to its turn. Emission no longer has to coincide with the turn,
so the flush-vs-fire race stops mattering.

### 2. Per-session self-check metric — the canary

After processing a session, log `#(agent-*.jsonl present)` vs `#(subagent gens emitted)`.
Divergence is the exact bug signature — emit a WARN (or a Langfuse tag). This fixes
nothing, but a divergence on **2026-07-08** would have surfaced the regression in a day
instead of nine. It is also the acceptance test for #1: once the sweep works, the two
numbers track.

### Alternative considered, not recommended — "hold back `last_line`"

Don't advance `last_line` past a turn whose `Agent` tool_uses can't yet be resolved; leave
the watermark at the turn's start so the next fire retries. Set aside because it is
fiddlier and has two failure modes: parent gens for that turn get re-emitted on every
retry (so idempotency is required *anyway*), and a subagent that never completes (crash /
kill) stalls the watermark forever unless a timeout/max-retries escape is added. Fix #1
sidesteps both — it never blocks the main path; it reconciles afterward.

### Validate

Preconditions — all three, or the gate lies: (a) the transcript-side accountant
(`usage-report.py`) must dedup by `message.id`; (b) the current duplicate ClickHouse rows
must be purged; **(c) Defect 3 must be fixed (the `emit_subagent_span` merge) — otherwise
subagent tokens in Langfuse read ~2.2–2.8× high and the gate passes or overshoots while
wrong.** With (a) transcript inflated ~2.3×, (b) Langfuse inflated ~1.5×, and (c) subagent
Langfuse inflated ~2.8×, every uncorrected term corrupts the comparison. Then re-run the
§ Methodology comparison against **new** briefings until Langfuse generation tokens reach
≥ ~95 % of the **deduped** transcript tokens for the llm-wiki slice, and confirm the
self-check metric reads `files == spans` on a fresh subagent-spawning session. Do **not**
target the old un-deduped total — that number is only reachable by a bug.

---

## ⚠️ Two versions of the hook exist — do not clobber the installed one

There are two copies of `langfuse_hook.py` and they differ by ~600 lines:

| | Path | Lines | Subagent expansion? |
|---|---|--:|---|
| **Installed (running)** | `~/.claude/hooks/langfuse_hook.py` | 1287 | **Yes** — `discover_subagents` / `emit_subagent_span` |
| Repo (this template) | `hooks/langfuse_hook.py` | 849 | **No — none at all** |

The installed copy is authoritative; everything in this brief was produced by it. A
reinstall / template update that copies the repo version over the installed one would drop
subagent capture from "raced" to **structurally impossible**. Remediation should **upstream
the installed version (plus the sweep + self-check) into the repo**, or make the install
step non-clobbering, so both converge on the subagent-aware version. Start with
`diff ~/.claude/hooks/langfuse_hook.py hooks/langfuse_hook.py`.

---

## Reproduce / verify

**Confirm the race on any subagent-spawning session** (all times local):

```bash
D=~/.claude/projects/<project-dir>; S=<session-uuid>
# Agent tool_use write times (parent transcript):
python3 - "$D/$S.jsonl" <<'PY'
import json,sys
for i,ln in enumerate(open(sys.argv[1])):
    try: r=json.loads(ln)
    except: continue
    for c in ((r.get("message") or {}).get("content") or []):
        if isinstance(c,dict) and c.get("type")=="tool_use" and c.get("name")=="Agent":
            print(i, r.get("timestamp"), (c.get("input") or {}).get("description","")[:40])
PY
# Subagent .jsonl mtimes (these land LATE — after the fires):
for f in "$D/$S"/subagents/agent-*.jsonl; do stat -f '%Sm %N' -t '%H:%M:%S' "$f"; done | sort
# Hook fire times:
grep "Processed" ~/.claude/state/langfuse_hook.log | grep '<date>'
# Langfuse ingestion times (should match the fires, not the transcript):
docker exec langfuse-self-host-clickhouse-1 clickhouse-client --query "
SELECT toTimeZone(created_at,'America/Los_Angeles'), count()
FROM default.observations FINAL
WHERE project_id='cmorsae2x0008n107rv5i5cqp' AND type='GENERATION'
  AND trace_id IN (SELECT DISTINCT id FROM default.traces
                   WHERE project_id='cmorsae2x0008n107rv5i5cqp' AND session_id='$S')
GROUP BY 1 ORDER BY 1"
```

**Offline replay harness** (proves the code is correct when files are present): load the
installed hook via `importlib`, force `mod.DEBUG=True`, replace the Langfuse client with a
mock whose `start_observation(as_type="generation")` increments a counter (split parent vs
`[agent_type] API call …` by the observation `name`), then call
`mod.process_transcript(mock, session_id, transcript_path, state={}, project_name=…)`.
Run it under the same interpreter the hook uses:
`uv run --no-project --with 'langfuse>=3.0,<4.0' --python 3.12 replay.py`. Expect it to
emit the full subagent gen count (82 for the session above) that production dropped.

---

## Environment & source data

- **Running hook (authoritative):** `~/.claude/hooks/langfuse_hook.py` (1287 lines) —
  **not** this repo's `hooks/langfuse_hook.py`. Diagnose against the installed copy.
- **Transcripts (ground truth):**
  - Parent: `~/.claude/projects/<project-dir>/<session-uuid>.jsonl`
  - Subagents: `<project-dir>/<session-uuid>/subagents/agent-<id>.jsonl` + sidecar
    `agent-<id>.meta.json` (`{agentType, description}`). **Caveat:** there is one record
    per content block, each repeating the full `message.usage`, so any token sum must
    first dedup by `message.id` (see the warning near the top). Per-message usage is exact;
    naive record sums are not.
- **Langfuse self-host:** http://localhost:3000. ClickHouse container
  `langfuse-self-host-clickhouse-1`, db `default`, tables `observations` / `traces`
  (both ReplacingMergeTree → always query with `FINAL`). `observations.created_at` is the
  **ingestion** time (used above to prove incremental processing); `start_time` is the API
  call time. **claude-code project_id:** `cmorsae2x0008n107rv5i5cqp`. llm-wiki slice:
  traces where `has(tags,'llm-wiki')`.
- **Reference (transcript-side tool):** `~/llm_wiki/scripts/usage-report.py` — the
  transcript-side token/cost accountant. **Not yet trustworthy as "truth":** the
  2026-07-16 change made it scan `subagents/` (closing the subagent blind spot), but it
  still appends every record's usage with **no dedup by `message.id`**, so it over-counts
  ~2.3× (and scanning `subagents/` added more double-counted tokens). It needs a
  dedup-by-`message.id` pass before it can anchor any comparison.

---

## Methodology

Two independent measurements, **same absolute window** (pin dates, e.g.
`start_time >= '2026-07-01 00:00:00'`), same token definition (input + output + all cache
tiers, excluding the derived `total` key), deduped.

- **Transcript side:** `python3 ~/llm_wiki/scripts/usage-report.py --days N` (scans parent
  `*.jsonl` + `<session>/subagents/*.jsonl`; attributes subagents to the root session).
  **Dedup by `message.id` first** — until the script does this itself, its totals are
  inflated ~2.3× and must not be compared against Langfuse as-is.
- **Langfuse side (ClickHouse):** sum `usage_details` over `observations FINAL` for
  `type='GENERATION'`, filtered to the llm-wiki trace set. Run via
  `docker exec langfuse-self-host-clickhouse-1 clickhouse-client --query "<SQL>"`.

Isolation checks used above: (a) subagent-only models (`sonnet-4-6`) appear 0× in Langfuse
while present in transcripts; (b) `mapContains(metadata,'subagent_type')` reads 0 for
recent sessions that demonstrably spawned subagents.

---

## Cost note (routine — out of scope, already actioned)

`claude-sonnet-5` was absent from Langfuse's `models` table, so captured sonnet-5 gens
priced at **$0** (opus-4-8 and sonnet-4-6 were priced correctly; captured Opus reconciles
to ~7% of the transcript rate, cache included). Fixed via `add-sonnet-5-price.sql`.
Langfuse prices at ingestion, so this affects **new** traces only; historical sonnet-5
rows stay $0 unless backfilled in ClickHouse. Normal per-model maintenance, unrelated to
the token-capture race above.
