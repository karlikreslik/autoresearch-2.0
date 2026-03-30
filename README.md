# autoresearch 2.0 — the researcher now has a manager. The manager is also an AI

Does an archetype write the Matrix, or just a glitchy beta version? Unaccelerated by design. 🥟 Running strictly on CPU, because true reality doesn't need a frame rate.

An extension of [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) with a
meta-research loop — the system now learns how to research, not just what to try.

## What's new in v2

| Feature | v1 | v2 |
|---------|----|----|
| Agent edits `train.py` | ✓ | ✓ |
| Fixed 5-min budget | ✓ | ✓ |
| Hypothesis tracking | ✗ | ✓ |
| Dead zone detection | ✗ | ✓ |
| Exploration/exploitation balance | ✗ | ✓ |
| Meta-researcher rewrites `program.md` | ✗ | ✓ |
| `program.md` version history | ✗ | ✓ |
| Fully autonomous Python loop (no UI needed) | ✗ | ✓ |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    AGENT 1: Researcher               │
│                                                      │
│  reads:  program.md, train.py, hypothesis_log.md     │
│  writes: train.py, results.tsv, hypothesis_log.md    │
│  loop:   every ~5 minutes                            │
│                                                      │
│  every 20 experiments: triggers meta_runner.py ───► │
└─────────────────────────────────────────────────────┘
                                                       │
                              ┌────────────────────────▼─────┐
                              │    AGENT 2: Meta-researcher   │
                              │                               │
                              │  reads:  results.tsv,         │
                              │          program.md,          │
                              │          hypothesis_log.md,   │
                              │          meta_program.md      │
                              │  writes: program.md (revised),│
                              │          hypothesis_log.md,   │
                              │          meta_results.tsv     │
                              │  loop:   every 20 experiments │
                              └───────────────────────────────┘
```

## Two ways to run

### Option A — autonomous (recommended)
`agent_runner.py` drives the entire loop via Claude API. No UI needed. Run it and go to sleep.

```bash
python agent_runner.py
```

### Option B — manual (original v1 style)
Spin up Claude in your browser, point it at `program.md`, and let it run interactively.
Every 20 experiments it will call `meta_runner.py` automatically.

## Files

```
train.py              ← Agent 1 edits (same as v1)
prepare.py            ← do not modify (same as v1)
program.md            ← Agent 1 reads; Agent 2 rewrites
meta_program.md       ← Agent 2's own instructions
agent_runner.py       ← autonomous Agent 1 loop (NEW)
meta_runner.py        ← launcher for Agent 2
hypothesis_log.md     ← Agent 1 writes; Agent 2 updates
results.tsv           ← Agent 1 logs (extended format)
meta_results.tsv      ← Agent 2 logs (auto-generated)
program_history/      ← program.md version archive (auto-generated)
  program_v2.0.md
  program_v2.1.md
  ...
```

## Installation

```bash
# 1. Clone the original autoresearch
git clone https://github.com/karpathy/autoresearch
cd autoresearch

# 2. Copy autoresearch 2.0 files
cp /path/to/autoresearch2/program.md .
cp /path/to/autoresearch2/meta_program.md .
cp /path/to/autoresearch2/agent_runner.py .
cp /path/to/autoresearch2/meta_runner.py .
cp /path/to/autoresearch2/hypothesis_log.md .

# 3. Set your API key (both agent_runner.py and meta_runner.py call the Anthropic API)
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Standard setup (same as v1)
uv run prepare.py
```

## Running — autonomous mode

```bash
# Standard — runs forever, meta-cycle every 20 experiments
python agent_runner.py

# Custom tag
python agent_runner.py --tag mar30

# Stop after N experiments
python agent_runner.py --max-experiments 100

# Dry run — propose experiments but do not edit train.py or train
python agent_runner.py --dry-run

# Resume existing branch
python agent_runner.py --resume

# Skip meta-researcher (Agent 2)
python agent_runner.py --skip-meta

# Use a different model
python agent_runner.py --model claude-sonnet-4-5
```

## Running — manual meta-cycle

```bash
# Standard
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md

# Analyze only, do not write files
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --dry-run

# Force run even with fewer than 20 experiments
python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --force
```

## results.tsv format (v2 — extended)

```tsv
commit	val_bpb	memory_gb	status	type	hypothesis_id	description
a1b2c3d	0.997900	44.0	keep	[EXPLOIT]	H-000	baseline
b2c3d4e	0.993200	44.2	keep	[EXPLOIT]	H-002	Muon LR 0.04
c3d4e5f	1.005000	44.0	discard	[EXPLORE]	H-004	GeLU activation
```

Two new columns vs v1:
- `type`: `[EXPLOIT]` or `[EXPLORE]`
- `hypothesis_id`: reference into hypothesis_log.md

## meta_results.tsv format

```tsv
version	experiments_analyzed	trend	explore_ratio	dead_zones_unlocked	hypotheses_added	program_changed	summary	timestamp
2.1	20	SLOW	0.25	1	3	yes	Unlocked LR dead zone...	2026-03-30T14:23:11
```

## Why this works

**The v1 problem**: the agent optimizes `train.py` but nothing optimizes how the agent optimizes.
Result: local minima, redundant re-testing, no memory across experiments.

**The v2 solution**: a two-level loop.

- Inner loop (Agent 1, every ~5 min): optimizes `train.py` → val_bpb
- Outer loop (Agent 2, every ~100 min): optimizes `program.md` → Agent 1's research efficiency

The meta-researcher does not pick specific experiments — that would be reward hacking. It changes
*how Agent 1 thinks*: what it tracks, what it ignores, where it searches.

## Risks and guardrails

**Meta-level reward hacking**: the meta-researcher could narrow the search space toward fast local
gains. Mitigation: guardrails in `meta_program.md` — max 60% exploration ratio, cannot declare
global winners, cannot delete rejected hypotheses.

**program.md convergence**: successive meta-cycles could converge toward increasingly narrow
instructions. Mitigation: `program_history/` enables rollback; every version is archived before
overwriting.

**Meta-cycle failure blocking research**: if `meta_runner.py` fails, Agent 1 logs the error and
continues with the current `program.md`. A meta-cycle failure never stops research.

## Karpathy's original framing (still applies)

> *One day, frontier AI research used to be done by meat computers in between eating, sleeping,
> having other fun, and synchronizing once in a while using sound wave interconnect in the ritual
> of "group meeting". That era is long gone...*

v2 adds: the agents now also learn *how* research is done, not just *what* to try.

## License

MIT — same as the original autoresearch
