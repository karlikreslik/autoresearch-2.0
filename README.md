# autoresearch 2.0 — the researcher now has a manager. The manager is also an AI

Does an archetype write the Matrix, or just a glitchy beta version? Unaccelerated by design. 🥟 Running strictly on CPU, because true reality doesn't need a frame rate.

An extension of [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) with a
meta-research loop — the system now learns how to research, not just what to try.

---

## What's new in v2

| Feature | v1 | v2 |
| --- | --- | --- |
| Agent edits `train.py` | ✓ | ✓ |
| Fixed 5-min budget | ✓ | ✓ |
| Hypothesis tracking | ✗ | ✓ |
| Dead zone detection | ✗ | ✓ |
| Exploration/exploitation balance | ✗ | ✓ |
| Meta-researcher rewrites `program.md` | ✗ | ✓ |
| `program.md` version history | ✗ | ✓ |

---

## Architecture

The original two-layer loop (Researcher → Meta-researcher) has been extended with a third layer:
a **MetaAuditor** that monitors integrity, detects metric gaming, and acts as a circuit breaker.

> *Deepseek's critique: "Two layers aren't enough. You either need a human in the meta-meta-loop,
> or admit your system is just an open loop."*
>
> This is the answer: a genuinely closed loop with three layers.

```
┌──────────────────────────────────────────────────────────────────┐
│                  LAYER 3 — MetaAuditor                           │
│                                                                  │
│  reads:  results.tsv, causal_memory.jsonl                        │
│  writes: causal_memory.jsonl (audit records)                     │
│  role:   integrity checker, drift detection, circuit breaker     │
└──────────────────────┬───────────────────────────────────────────┘
                       │ audit report + circuit breaker signal
┌──────────────────────▼───────────────────────────────────────────┐
│                  LAYER 2 — MetaResearcher                        │
│                                                                  │
│  reads:  causal_memory.jsonl, results.tsv, program.md            │
│  writes: program.md (revised), causal_memory.jsonl (annotations) │
│  role:   hypothesis generation from causal memory, tagging,      │
│          strategy selection, program.md updates                  │
└──────────────────────┬───────────────────────────────────────────┘
                       │ updated program.md
┌──────────────────────▼───────────────────────────────────────────┐
│                  LAYER 1 — Researcher                            │
│                                                                  │
│  reads:  program.md, train.py                                    │
│  writes: train.py, results.tsv                                   │
│  role:   runs experiments (Claude Code agent or direct train.py) │
│  loop:   every ~5 minutes                                        │
└──────────────────────────────────────────────────────────────────┘
```

### Main loop

```
while True:
  1. [L3] Audit: verify integrity of last experiment, update causal log
  2. [L3] Detect gaming, check circuit breaker
  3. [L2] Read causal memory → generate hypothesis → update program.md
  4. [L1] Run researcher agent (Claude Code or direct train.py)
  5. [L2] Annotate result into causal log (hypothesis + tags)
  6. Back to 1.
```

---

## Files

```
train.py                  ← Layer 1 edits (same as v1)
prepare.py                ← do not modify (same as v1)
program.md                ← Layer 1 reads; Layer 2 rewrites
meta_program.md           ← Layer 2's own instructions
meta_runner.py            ← launcher for the original two-layer loop
hypothesis_log.md         ← Layer 1 writes; Layer 2 updates
results.tsv               ← Layer 1 logs (extended format)
meta_results.tsv          ← Layer 2 logs (auto-generated)
causal_memory.jsonl       ← Layer 3 causal log (auto-generated)
program_history/          ← program.md version archive (auto-generated)
  program_v2.0.md
  program_v2.1.md
  ...
```

### Three-layer additions

| File | Layer | Role |
|------|-------|------|
| `meta_auditor.py` | 3 | Integrity checker, drift detection, causal log, circuit breaker |
| `meta_researcher_v2.py` | 2 | Hypothesis generation from memory, experiment tagging, `program.md` updates |
| `orchestrator.py` | — | Ties all three layers into a single loop |

---

## Installation

```bash
# 1. Clone the original autoresearch
git clone https://github.com/karpathy/autoresearch
cd autoresearch

# 2. Copy autoresearch 2.0 files
cp /path/to/autoresearch2/program.md .
cp /path/to/autoresearch2/meta_program.md .
cp /path/to/autoresearch2/meta_runner.py .
cp /path/to/autoresearch2/hypothesis_log.md .

# 3. (Optional) Copy three-layer files
cp /path/to/autoresearch2/meta_auditor.py .
cp /path/to/autoresearch2/meta_researcher_v2.py .
cp /path/to/autoresearch2/orchestrator.py .

# 4. Set your API key (meta_runner.py calls the Anthropic API)
export ANTHROPIC_API_KEY="sk-ant-..."

# 5. Standard setup (same as v1)
uv run prepare.py
```

---

## Running

### Original two-layer loop (unchanged)

Same as v1 — spin up Agent 1 in Claude/Codex and prompt:

```
Have a look at program.md and kick off a new experiment. Let's do the setup first.
```

Agent 1 reads `program.md` (v2.0), sets up the branch, and begins experimenting.
Every 20 experiments it automatically calls `meta_runner.py`.

### Three-layer loop (new)

```bash
# Full three-layer loop
python orchestrator.py --mode full --max-iter 100

# Status overview (no experiments run)
python orchestrator.py --mode status

# Integrity audit only (verify existing results)
python orchestrator.py --mode audit

# Direct train.py (without Claude Code agent)
python orchestrator.py --mode full --direct-train
```

### Manual meta-cycle (original)

```bash
# Standard run
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md

# Analyze only, do not write files
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --dry-run

# Force run even with fewer than 20 experiments
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --force

# Use a different model
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --model claude-sonnet-4-5
```

---

## Integrating the three-layer extension into an existing autoresearch-2.0 repo

1. Copy the three files into the repository root:
   ```bash
   cp meta_auditor.py meta_researcher_v2.py orchestrator.py /path/to/your/repo/
   ```
2. Add the following instruction to the end of `program.md` (for Layer 1):
   ```
   After each experiment call: python meta_auditor.py --no-rerun
   ```
3. Run the orchestrator instead of the original runner script:
   ```bash
   python orchestrator.py --mode full
   ```

---

## results.tsv format (v2 — extended)

```
commit	val_bpb	memory_gb	status	type	hypothesis_id	description
a1b2c3d	0.997900	44.0	keep	[EXPLOIT]	H-000	baseline
b2c3d4e	0.993200	44.2	keep	[EXPLOIT]	H-002	Muon LR 0.04
c3d4e5f	1.005000	44.0	discard	[EXPLORE]	H-004	GeLU activation
```

Two new columns vs v1:

* `type`: `[EXPLOIT]` or `[EXPLORE]`
* `hypothesis_id`: reference into `hypothesis_log.md`

## causal_memory.jsonl format (new in three-layer)

Each line is a JSON record written by the MetaAuditor:

```json
{
  "commit": "a1b2c3d",
  "metric": 0.997900,
  "status": "keep",
  "tags": ["optimizer", "lr_schedule"],
  "hypothesis": "Reducing LR by 10× will lower val_bpb by ~0.003.",
  "causal_parents": ["9f8e7d6"],
  "audit_status": "PASS",
  "timestamp": "2026-03-30T14:23:11"
}
```

## meta_results.tsv format

```
version	experiments_analyzed	trend	explore_ratio	dead_zones_unlocked	hypotheses_added	program_changed	summary	timestamp
2.1	20	SLOW	0.25	1	3	yes	Unlocked LR dead zone...	2026-03-30T14:23:11
```

---

## Why this works

**The v1 problem**: the agent optimizes `train.py` but nothing optimizes how the agent optimizes.
Result: local minima, redundant re-testing, no memory across experiments.

**The v2 solution**: a two-level loop.

* Inner loop (Layer 1, every ~5 min): optimizes `train.py` → val\_bpb
* Outer loop (Layer 2, every ~100 min): optimizes `program.md` → Layer 1's research efficiency

**The three-layer extension** closes the remaining open loop: a MetaAuditor (Layer 3) that
continuously verifies that measured improvements are real, not artifacts of the optimization
process itself. Layer 2 reads Layer 3's causal memory before proposing any new hypothesis.

The meta-researcher does not pick specific experiments — that would be reward hacking. It changes
*how Layer 1 thinks*: what it tracks, what it ignores, where it searches.

---

## Risks and guardrails

**Meta-level reward hacking**: the meta-researcher could narrow the search space toward fast local
gains. Mitigation: guardrails in `meta_program.md` — max 60% exploration ratio, cannot declare
global winners, cannot delete rejected hypotheses.

**program.md convergence**: successive meta-cycles could converge toward increasingly narrow
instructions. Mitigation: `program_history/` enables rollback; every version is archived before
overwriting.

**Meta-cycle failure blocking research**: if `meta_runner.py` fails, Layer 1 logs the error and
continues with the current `program.md`. A meta-cycle failure never stops research.

**Circuit breaker (three-layer only)**: if the MetaAuditor detects a configurable number of
consecutive integrity failures (`CIRCUIT_BREAK_THRESHOLD`, default 3), the entire loop is halted
and a summary report is written. This prevents a runaway loop from accumulating corrupt results.

---

## What the three-layer extension does NOT add (and why)

**Human in the meta-meta-loop.** Deepseek proposed this as an alternative to a third automated
layer. We chose an automatic auditor with a circuit breaker instead. Advantage: fully autonomous.
Disadvantage: the auditor can have blind spots of its own. If you want a human in the loop, set
`CIRCUIT_BREAK_THRESHOLD = 1` and wire `AUDIT_FAIL` events to a notification hook
(email, Slack webhook, etc.).

**External verification dataset.** The most robust defense against metric gaming is a completely
separate holdout set that the agent never sees. This requires a change to `prepare.py`
(adding a `--holdout` split) and was intentionally left out of this extension to keep it
backwards-compatible with existing repos. To add it yourself, split your data in `prepare.py`
before running and point `meta_auditor.py` at the holdout path via `--holdout-tsv`.

---

## Karpathy's original framing (still applies)

> *One day, frontier AI research used to be done by meat computers in between eating, sleeping,
> having other fun, and synchronizing once in a while using sound wave interconnect in the ritual
> of "group meeting". That era is long gone...*

v2 adds: the agents now also learn *how* research is done, not just *what* to try.

The three-layer extension adds: someone checks that the agents aren't lying.

---

## License

MIT — same as the original autoresearch
