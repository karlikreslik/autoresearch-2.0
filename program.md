# autoresearch — program.md v2.0

> **Note:** This is an evolved version of the original program.md. It adds hypothesis tracking,
> dead zone detection, and exploration/exploitation balance. The meta-researcher (Agent 2) may
> rewrite sections of this file based on experimental history. Current version: **2.0**
> Last updated by: human | Meta-researcher updates: 0

---

## Setup

To set up a new experiment, work with the user to:

1. **Agree on a run tag**: propose a tag based on today's date (e.g. `mar5`). The branch
   `autoresearch/<tag>` must not already exist — this is a fresh run.
2. **Create the branch**: `git checkout -b autoresearch/<tag>` from current master.
3. **Read the in-scope files**: Read these files for full context:
   * `README.md` — repository context.
   * `prepare.py` — fixed constants, data prep, tokenizer, dataloader, evaluation. Do not modify.
   * `train.py` — the file you modify. Model architecture, optimizer, training loop.
4. **Verify data exists**: Check that `~/.cache/autoresearch/` contains data shards and a tokenizer.
   If not, tell the human to run `uv run prepare.py`.
5. **Initialize results.tsv**: Create `results.tsv` with the full header row (see Output format).
6. **Initialize hypothesis_log.md**: Create `hypothesis_log.md` (see Hypothesis tracking).
7. **Confirm and go**: Confirm setup looks good, then begin.

---

## Experimentation

Each experiment runs on a single GPU. Training always runs for a **fixed 5-minute wall clock
budget** (excluding startup/compilation). Launch with: `uv run train.py`.

**What you CAN do:**
- Modify `train.py` — architecture, optimizer, hyperparameters, training loop, batch size, model
  size. Everything is fair game.

**What you CANNOT do:**
- Modify `prepare.py`. It is read-only.
- Install new packages or add dependencies beyond `pyproject.toml`.
- Modify the evaluation harness. `evaluate_bpb` in `prepare.py` is ground truth.

**The goal: lowest val_bpb.** Time budget is fixed so you never need to worry about training
duration. Only constraint: code runs without crashing and finishes within the budget.

**VRAM** is a soft constraint. Acceptable for meaningful gains, but must not blow up dramatically.

**Simplicity criterion**: All else being equal, simpler is better. A small improvement that adds
ugly complexity is not worth it. Removing code and getting equal or better results is a win.

---

## Hypothesis tracking

Before each experiment, you MUST update `hypothesis_log.md`. This file is your research memory.

### Format of hypothesis_log.md

```markdown
# Hypothesis log

## Active hypotheses
- [H-001] Increasing depth beyond 8 will improve val_bpb — UNTESTED
- [H-002] Muon optimizer LR is suboptimal — PARTIALLY TESTED (tried 0.04, not 0.06)
- [H-003] Rotary embeddings would outperform learned pos embeddings — UNTESTED

## Confirmed hypotheses (kept)
- [H-000] Baseline established at 0.997900 — CONFIRMED

## Rejected hypotheses (discarded)
- [H-004] GeLU better than SiLU — REJECTED (val_bpb 1.005 vs 0.997)

## Dead zones (do not re-explore without meta-researcher unlock)
- Learning rate in [0.01, 0.05]: tried 5 variants, no improvement
```

### Rules for hypothesis tracking:
1. Every experiment MUST map to exactly one hypothesis (existing or newly created).
2. After each experiment, update the hypothesis status.
3. If a hypothesis is confirmed (kept), mark it and derive follow-up hypotheses.
4. If rejected, move to the rejected list. Do not re-test without a specific new reason.
5. Dead zones are declared when 3+ variants of the same axis show no improvement.

---

## Exploration / exploitation balance

Maintain a ratio of **~70% exploitation / ~30% exploration** across any 10-experiment window.

- **Exploitation**: incremental tuning of a known-good direction (e.g. "depth 8 improved things,
  try depth 10 and depth 12").
- **Exploration**: testing a fundamentally different approach (e.g. switching optimizer family,
  trying a different attention pattern, changing tokenizer interaction).

Count your last 10 experiments. If fewer than 2 were exploratory, your next experiment MUST be
exploratory. Write `[EXPLOIT]` or `[EXPLORE]` in the description field of results.tsv.

**Why this matters**: Pure exploitation converges to local minima. Pure exploration wastes budget.
The 70/30 split is a starting point — the meta-researcher may adjust it based on observed results.

---

## Dead zone detection

A **dead zone** is a hyperparameter axis where continued search is unlikely to yield improvement.

Declare a dead zone when:
- You have tested 3+ values on the same axis (e.g. LR: 0.01, 0.02, 0.04)
- None improved val_bpb beyond noise (±0.0005)
- No interaction with other changed parameters is plausible

Once declared, add to `hypothesis_log.md` under "Dead zones". Do NOT re-test a dead zone
unless the meta-researcher explicitly unlocks it in a new version of this file.

**Current declared dead zones:**
*(none — will be populated as experiments accumulate)*

---

## Output format

Once the script finishes it prints a summary:

```
---
val_bpb:          0.997900
training_seconds: 300.1
total_seconds:    325.9
peak_vram_mb:     45060.2
mfu_percent:      39.80
total_tokens_M:   499.6
num_steps:        953
num_params_M:     50.3
depth:            8
```

Extract key metrics:
```bash
grep "^val_bpb:\|^peak_vram_mb:" run.log
```

---

## Logging results

Log to `results.tsv` (tab-separated, NOT comma-separated). Do NOT commit this file.

Header and columns:
```
commit	val_bpb	memory_gb	status	type	hypothesis_id	description
```

1. `commit` — git commit hash (short, 7 chars)
2. `val_bpb` — metric achieved; use 0.000000 for crashes
3. `memory_gb` — peak VRAM in GB, rounded to .1f; use 0.0 for crashes
4. `status` — `keep`, `discard`, or `crash`
5. `type` — `[EXPLOIT]` or `[EXPLORE]`
6. `hypothesis_id` — e.g. `H-002`
7. `description` — short text, no commas

Example:
```
commit	val_bpb	memory_gb	status	type	hypothesis_id	description
a1b2c3d	0.997900	44.0	keep	[EXPLOIT]	H-000	baseline
b2c3d4e	0.993200	44.2	keep	[EXPLOIT]	H-002	Muon LR 0.04
c3d4e5f	1.005000	44.0	discard	[EXPLORE]	H-004	GeLU activation
d4e5f6g	0.000000	0.0	crash	[EXPLORE]	H-005	double model width OOM
```

---

## The experiment loop

LOOP FOREVER:

1. Read current git state (branch/commit).
2. Read `hypothesis_log.md` — understand where you are in the research space.
3. Check exploration/exploitation ratio over last 10 experiments.
4. Select the next hypothesis to test. Write your reasoning in `hypothesis_log.md` before coding.
5. Tune `train.py` with the experimental idea.
6. `git commit`
7. Run: `uv run train.py > run.log 2>&1`
8. Extract results: `grep "^val_bpb:\|^peak_vram_mb:" run.log`
9. If grep is empty: crashed. Run `tail -n 50 run.log`, attempt fix. If unfixable after 2 attempts,
   log as crash and move on.
10. Update `hypothesis_log.md` with result.
11. Log to `results.tsv`.
12. If val_bpb improved: keep commit, advance branch.
13. If val_bpb equal or worse: `git reset --hard HEAD~1`.
14. Check if meta-researcher trigger condition is met (see below).

**Timeout**: Each experiment ~5 min + startup. If >10 min, kill and treat as failure.

**NEVER STOP**: Do NOT pause to ask the human. Do NOT ask "should I keep going?". Run until
manually interrupted. If out of ideas: re-read `prepare.py` and `train.py` for angles you missed,
look at rejected hypotheses for combinations you haven't tried, generate 5 new exploratory
hypotheses.

---

## Meta-researcher trigger

After every **20 experiments**, pause the experiment loop and run:

```bash
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md
```

This invokes the meta-researcher (Agent 2) which may rewrite this `program.md`.

After `meta_runner.py` completes:
1. Re-read `program.md` — it may have changed.
2. Re-read `hypothesis_log.md` — meta-researcher may have added/removed dead zones.
3. Note the new version number at the top of this file.
4. Resume the experiment loop with updated instructions.

**Important**: If `meta_runner.py` is not present or fails, log the failure and continue with
the current `program.md`. Never let meta-researcher failure block research.

---

## Footer

program.md v2.0 | autoresearch 2.0 | 2026
