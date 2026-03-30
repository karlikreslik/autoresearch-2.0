# autoresearch 2.0

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

## Files

```
train.py              ← Agent 1 edits (same as v1)
prepare.py            ← do not modify (same as v1)
program.md            ← Agent 1 reads; Agent 2 rewrites
meta_program.md       ← Agent 2's own instructions
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
cp /path/to/autoresearch2/meta_runner.py .
cp /path/to/autoresearch2/hypothesis_log.md .

# 3. Set your API key (meta_runner.py calls the Anthropic API)
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Standard setup (same as v1)
uv run prepare.py
```

## Running

Same as v1 — spin up Agent 1 in Claude/Codex and prompt:

```
Have a look at program.md and kick off a new experiment. Let's do the setup first.
```

Agent 1 reads `program.md` (v2.0), sets up the branch, and begins experimenting.
Every 20 experiments it automatically calls `meta_runner.py`.

## Manual meta-cycle

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
