# Turbine spec (v1)

Status: draft for build. v0 exists in this folder and is the starting point. It proved the idea. This spec fixes what v0 got wrong.

## 1. What it is
- An agent loop where the worker never decides it's done.
- The worker only proposes better versions of one artifact (code, text, config).
- A plain-code governor scores each proposal 0–1 and forces the exit.
- "Finish" is a quit rule, never a quit option.

## 2. Non-goals
- Not a general agent framework. One artifact, one scorer, one loop.
- No LLM judge inside the governor. A scorer may call a model only if it is declared `judge`, and then it never gates an exit alone (see 6.4).
- No dependencies in the core. Python 3.11+, standard library only. Plotting is an optional extra.

## 3. Vocabulary
- **state**: the artifact being improved (a string or bytes).
- **score(state) → (float 0..1, feedback: str)**: the scorer. Objective, external, never run by the worker.
- **worker**: `step(handoff, trail) → (state, tokens, note)`. Stateless between calls except what it's handed.
- **governor**: pure function `decide(history, ledger) → continue | done | accept | park | halt`.
- **trail**: the worker's own notes from earlier rounds of one attempt.
- **attempt**: one worker instance, one trail. A park starts a new attempt with a new worker.
- **fresh eyes**: a new worker handed only best state, score history, last feedback. No trail.
- **context_level** 0.0–1.0: how much of the trail the next step sees. 0 = none. 1 = all.

## 4. Behavior rules (the governor)
- done: best score ≥ ideal.
- exhausted budget: accept if best ≥ accept bar, else halt.
- stalled (best gained < eps over the last `window` rounds) or max_rounds reached: accept if best ≥ bar, else park.
- park → new attempt with fresh eyes, up to `max_attempts`. Last park returns exit `park`.
- Exit precedence: done > budget > stall/max_rounds.
- The worker's output can never set the exit. The Worker interface has no exit field, and `run()` returns only governor verdicts.

## 5. Known v0 gaps this spec closes
- Budget isn't a hard stop. The ledger is checked between steps, so a step can overshoot. Parallel runs each got their own ledger, so the global cap was never enforced, only reported.
- The scorer runs model-written code with the host's env, network and filesystem.
- Score is a numeric oracle on the hidden cases, so a worker with many rounds can fit them. Visible vs hidden was the only guard.
- Cache reads aren't counted anywhere. A reported "token" cost undercounts.
- Curve experiment at n=1 per level. Conclusions were not supportable.
- `best_feedback` only updates on improvement, so the worker is blind to why a non-improving attempt failed.

## 6. Requirements

### 6.1 Core
- `run(task, scorer, worker_factory, initial_state, governor, ledger, context_level, max_attempts) → Result`.
- `Result` carries: exit, best_score, best_state, rounds, attempts, tokens, seconds, history, and a `held_out_score` (6.5).
- Pure and deterministic given scorer and worker outputs. All randomness is seeded and recorded.

### 6.2 Budget (hard stop)
- One `Ledger` object shared by every run in a process. Thread-safe.
- Before each worker call: `ledger.reserve(max_step_tokens)`. If remaining < reserve → the call is not made and the governor sees `exhausted`.
- After the call: `ledger.settle(reserved, actual)`. If actual > reserved → record an overrun, mark the ledger `exhausted`, and stop all runs.
- Time cap works the same way, using a monotonic clock. Human wait time is excluded via an explicit `ledger.pause()` / `resume()`.
- Spend is appended to a JSONL file: source, input, output, cache_write, cache_read, note, time. Charged tokens = input + output + cache_write. Cache reads are logged separately and reported next to the charged total.
- `python -m turbine.monitor` prints spend vs budget and flags WARN at 80%, HALT at 100%.

### 6.3 Scorer isolation
- Candidate code runs in a subprocess: isolated interpreter flags, temp cwd, scrubbed env (no keys, no tokens), CPU, memory and file-size rlimits, wall timeout.
- Network is denied where the OS allows it. Where it can't be enforced, the result says `network_isolation: false` and the run report prints that line.
- A scorer that times out or crashes returns score 0 with the reason in the feedback. It never raises into the loop.

### 6.4 Judge scorers (optional)
- A scorer may include a model-judged component only as a declared `judge` term with weight ≤ 0.3.
- A judge term can never be the deciding factor for `done` or `accept`. An objective component must also pass.

### 6.5 Held-out set
- The task provides three case sets: `visible` (worker sees failures and expected values), `hidden` (worker sees only a count), `heldout` (never used in the loop's score, feedback or logs).
- After the loop exits, score best_state on `heldout` once. Report `held_out_score` and the gap to the loop score. A gap over 0.10 is flagged `overfit`.

### 6.6 Runs are replayable
- Every run writes a record: per round the state hash, score, tokens and note.
- `python -m turbine.replay run.json` re-applies the governor to the recorded scores with no model calls. It must reproduce the same exit and the same round count.

### 6.7 Workers
- One interface. Adapters: (a) headless `claude -p`, (b) any OpenAI-compatible HTTP endpoint, (c) a scripted fake for tests.
- A worker adapter returns real token counts from the provider. If the provider returns none, the adapter estimates and marks the row `estimated`.
- The prompt builder is separate and tested. It never includes more trail than `trim_trail(trail, level)` allows.

### 6.8 Curve experiment
- Sweep context_level over {0, 0.25, 0.5, 1.0} with `reps` runs per level, same governor, same start, same per-run budget.
- Report per level: mean and bootstrap 95% interval of gain per 1k tokens, rounds to accept, park rate, and mean best score.
- Also report run-to-run sd at each level. If the intervals of the best two levels overlap, the report says `no winner at this n` and recommends more reps. It must not name a winner.
- Output: `results.json`, `curve.png` (optional extra), one-line verdict.

### 6.9 CLI
- `python -m turbine run task.toml`
- `python -m turbine curve task.toml --reps 3`
- `python -m turbine replay run.json`
- `python -m turbine monitor`
- A task is a folder: `task.toml` (name, budgets, governor settings), `scorer.py`, `cases/`, `start.*`.

## 7. Acceptance tests
Each must exist as an automated test. A milestone is done when its tests pass offline with no network and no API key.

Governor
- A1: truth table. done at score ≥ ideal. accept below ideal when stalled and ≥ bar. park when stalled and < bar. halt when budget gone and < bar. accept when budget gone and ≥ bar. continue otherwise. Cover the exact boundaries at ideal, bar and eps.
- A2: stalled needs `len(history) > window`. With exactly `window` entries it's `continue`.
- A3: exit precedence holds when done and exhausted are true together.

Loop
- A4: a fake worker that returns `"I am done"` in every note and the same state forever still gets `park` or `accept` from the governor, never an early exit.
- A5: `context_level=0` → the fake worker receives an empty trail every step. At 0.25 and 1.0 it receives exactly `trim_trail` length. Add a test for a one-note trail at 0.25.
- A6: after a park, the next worker instance receives an empty trail, the best state, the full score history and the last feedback.
- A7: `run()` returns exit ∈ {done, accept, park, halt} only.

Budget
- A8: 8 threads each running a loop on one shared ledger with a fake worker costing 1,000 tokens: total charged ≤ max_tokens, never over by a step.
- A9: a worker that reports actual > reserved flips the ledger to exhausted and every run exits within one round.
- A10: `pause()`/`resume()` don't count against the time cap.
- A11: JSONL rows sum to the ledger total. Cache reads appear in the log and the report, not in the charged total.

Scorer isolation
- A12: a candidate that sleeps forever scores 0 within the timeout.
- A13: a candidate that reads `os.environ` finds no API keys. A candidate that writes outside its temp dir fails or the write isn't visible to the host (test with a path under the host's tmp).
- A14: a candidate that raises or exits non-zero scores 0 with the reason in feedback.
- A15: the report states `network_isolation: true|false` honestly. Test both branches by stubbing the OS check.

Held-out and replay
- A16: held-out cases never appear in worker prompts, feedback, logs or the run record. Grep-style test over a full fake run.
- A17: a worker engineered to fit hidden cases (fake scorer exposes them) produces `overfit` in the report.
- A18: `replay` reproduces exit and round count from a recorded run, offline.

Curve
- A19: with a seeded fake worker whose quality is independent of context_level, the report says `no winner at this n`.
- A20: with a seeded fake worker that improves only at level 0.25, and enough reps, the report names 0.25 and the interval excludes the others.

Adapters
- A21: prompt builder output at level 0 contains no trail text. At level 1 it contains all of it. Golden-file test.
- A22: the HTTP adapter against a local fake server returns correct token counts and handles a 429 with one retry.

## 8. Build order
Each milestone is its own PR into the Turbine repo, with its tests. Never merge red.
- M1: core + governor + A1–A7.
- M2: ledger + monitor + A8–A11.
- M3: sandboxed scorer + A12–A15.
- M4: held-out + records + replay + A16–A18.
- M5: curve with intervals + A19–A20.
- M6: adapters + A21–A22 and the CLI.
- M7 (optional): first adopter. jp_mo's weekly filter calibration as a Turbine task: state = filter wording, scorer = pair ordering against the owner's grades, held-out = the latest grades.

## 9. Working rules for the builder
- Read this whole file and the v0 code before writing anything.
- Write the tests for a milestone first, then the code.
- Standard library only in the core. Ask before adding any dependency.
- No file in the repo may contain personal data. No names, emails, keys, or real postings or grades. Use synthetic cases.
- If a requirement is unclear or two requirements conflict, stop and write the question at the top of the PR. Don't guess.
- Keep each PR under about 400 changed lines. Split if bigger.

## 10. Open decisions (owner)
- Where it lives: its own repo, or a folder in the current one.
- Licence.
- Which worker backend the builder should assume first: `claude -p` or an OpenAI-compatible endpoint.
