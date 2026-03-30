#!/usr/bin/env python3
"""
agent_runner.py — autoresearch 2.0

Fully autonomous Agent 1 (Researcher) loop.
Calls the Claude API directly — no Claude UI needed.
Runs experiments overnight without human supervision.

Usage:
    python agent_runner.py                          # standard run
    python agent_runner.py --tag mar30              # custom run tag
    python agent_runner.py --max-experiments 50     # stop after N experiments
    python agent_runner.py --dry-run                # plan only, do not edit train.py
    python agent_runner.py --resume                 # resume existing branch

How it works:
    1. Reads program.md, train.py, prepare.py for context
    2. Asks Claude to propose a change to train.py
    3. Applies the diff, commits, runs uv run train.py
    4. Reads results, asks Claude to decide keep/discard
    5. Updates results.tsv and hypothesis_log.md
    6. Every 20 experiments: calls meta_runner.py
    7. Loops forever (or until --max-experiments)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-opus-4-5"
TRAIN_TIMEOUT = 620          # 10min + buffer before we kill the run
META_TRIGGER_EVERY = 20      # run meta-researcher every N experiments
RESULTS_FILE = "results.tsv"
HYPOTHESIS_FILE = "hypothesis_log.md"
PROGRAM_FILE = "program.md"
TRAIN_FILE = "train.py"
PREPARE_FILE = "prepare.py"
LOG_FILE = "run.log"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"[FILE NOT FOUND: {path}]"
    return p.read_text(encoding="utf-8")


def write_file(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def run_cmd(cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def git_commit(message: str) -> str:
    """Stage train.py + hypothesis_log.md and commit. Return short hash."""
    run_cmd(f"git add {TRAIN_FILE} {HYPOTHESIS_FILE}")
    rc, out, err = run_cmd(f'git commit -m "{message}"')
    if rc != 0:
        # Nothing to commit
        rc2, out2, _ = run_cmd("git rev-parse --short HEAD")
        return out2.strip()
    rc3, out3, _ = run_cmd("git rev-parse --short HEAD")
    return out3.strip()


def git_reset() -> None:
    """Discard last commit (keep files staged for inspection)."""
    run_cmd("git reset --hard HEAD~1")


def count_experiments() -> int:
    """Count experiment rows in results.tsv."""
    p = Path(RESULTS_FILE)
    if not p.exists():
        return 0
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    return max(0, len(lines) - 1)  # minus header


def append_result(commit: str, val_bpb: float, memory_gb: float,
                  status: str, exp_type: str, hypothesis_id: str, description: str) -> None:
    """Append one row to results.tsv."""
    p = Path(RESULTS_FILE)
    header = "commit\tval_bpb\tmemory_gb\tstatus\ttype\thypothesis_id\tdescription"
    if not p.exists():
        p.write_text(header + "\n", encoding="utf-8")
    row = f"{commit}\t{val_bpb:.6f}\t{memory_gb:.1f}\t{status}\t{exp_type}\t{hypothesis_id}\t{description}"
    with p.open("a", encoding="utf-8") as f:
        f.write(row + "\n")


def parse_train_output(log_path: str) -> dict:
    """Extract metrics from run.log. Returns dict with val_bpb, peak_vram_mb etc."""
    metrics = {}
    try:
        content = Path(log_path).read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if key in ("val_bpb", "peak_vram_mb", "training_seconds",
                           "total_tokens_M", "num_params_M", "depth"):
                    try:
                        metrics[key] = float(val)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return metrics


def setup_branch(tag: str) -> str:
    """Create and checkout autoresearch/<tag> branch."""
    branch = f"autoresearch/{tag}"
    rc, _, _ = run_cmd(f"git checkout -b {branch}")
    if rc != 0:
        # Branch exists — checkout
        run_cmd(f"git checkout {branch}")
    print(f"[agent] Branch: {branch}")
    return branch


def init_results_tsv() -> None:
    """Create results.tsv with header if not exists."""
    if not Path(RESULTS_FILE).exists():
        header = "commit\tval_bpb\tmemory_gb\tstatus\ttype\thypothesis_id\tdescription"
        Path(RESULTS_FILE).write_text(header + "\n", encoding="utf-8")
        print(f"[agent] Created {RESULTS_FILE}")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ─── Claude calls ─────────────────────────────────────────────────────────────

def call_claude(client: anthropic.Anthropic, model: str,
                system: str, user: str, max_tokens: int = 4096) -> str:
    """Single Claude API call. Returns text response."""
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


def propose_experiment(client: anthropic.Anthropic, model: str,
                       program_text: str, train_text: str, prepare_text: str,
                       hypothesis_text: str, results_text: str) -> dict:
    """
    Ask Claude to propose the next experiment.
    Returns dict with keys: hypothesis_id, hypothesis_desc, exp_type,
                            new_train_py, commit_message, reasoning
    """
    system = """You are an autonomous ML researcher running experiments on a GPT training setup.
You will propose ONE experiment: a specific change to train.py that you believe will improve val_bpb.

You MUST respond with valid JSON only. No markdown fences, no preamble.

Schema:
{
  "hypothesis_id": "H-001",
  "hypothesis_desc": "one sentence description of the hypothesis",
  "exp_type": "[EXPLOIT]" or "[EXPLORE]",
  "reasoning": "2-3 sentences why you chose this experiment",
  "commit_message": "short git commit message",
  "new_train_py": "COMPLETE new content of train.py"
}

Rules:
- new_train_py must be the COMPLETE file, not a diff
- The change must be meaningful but focused — one idea at a time
- Check hypothesis_log.md for dead zones — do not re-test them
- Check exploration/exploitation ratio in results.tsv
- hypothesis_id: if extending existing hypothesis use same ID with suffix (H-002b),
  if new idea create next sequential ID
"""

    user = f"""## program.md (your instructions)
{program_text}

## Current train.py
{train_text}

## prepare.py (read-only, for context)
{prepare_text}

## hypothesis_log.md
{hypothesis_text}

## results.tsv (experiment history)
{results_text if results_text else "(no experiments yet — this is the baseline run)"}

---
Propose the next experiment. If this is the first run, return train.py unchanged for baseline.
"""
    raw = call_claude(client, model, system, user, max_tokens=8192)

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"ERROR: Claude returned invalid JSON for experiment proposal: {e}")
        log(f"Raw (first 300): {raw[:300]}")
        raise


def decide_keep_or_discard(client: anthropic.Anthropic, model: str,
                            metrics: dict, previous_best: float,
                            hypothesis_desc: str, reasoning: str) -> dict:
    """
    Ask Claude to decide keep/discard based on metrics.
    Returns dict with: status, updated_hypothesis_log_section, summary
    """
    system = """You are evaluating an ML experiment result.
Respond with valid JSON only. No markdown fences.

Schema:
{
  "status": "keep" or "discard",
  "summary": "one sentence description for results.tsv",
  "hypothesis_update": "one sentence updating hypothesis status (CONFIRMED/REJECTED/PARTIALLY)"
}
"""
    user = f"""Hypothesis: {hypothesis_desc}
Reasoning: {reasoning}

Previous best val_bpb: {previous_best:.6f}
This run val_bpb:      {metrics.get('val_bpb', 'CRASH'):.6f if 'val_bpb' in metrics else 'CRASH'}
Peak VRAM MB:          {metrics.get('peak_vram_mb', 0):.0f}
Training seconds:      {metrics.get('training_seconds', 0):.0f}

Decision rules:
- keep if val_bpb improved (lower is better)
- discard if val_bpb equal or worse
- Note: small improvements (<0.0005) are noise, consider complexity cost
"""
    raw = call_claude(client, model, system, user, max_tokens=512)
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: parse manually
        status = "keep" if metrics.get("val_bpb", 999) < previous_best else "discard"
        return {"status": status, "summary": hypothesis_desc[:60], "hypothesis_update": ""}


def update_hypothesis_log(client: anthropic.Anthropic, model: str,
                          hypothesis_text: str, hypothesis_id: str,
                          hypothesis_desc: str, exp_type: str,
                          status: str, hypothesis_update: str,
                          val_bpb: float) -> str:
    """Ask Claude to update hypothesis_log.md with this experiment's result."""
    system = """You are updating a research hypothesis log.
Return the COMPLETE updated hypothesis_log.md content. No JSON, just the markdown file content."""

    user = f"""Current hypothesis_log.md:
{hypothesis_text}

Update to record:
- Experiment: {hypothesis_id} — {hypothesis_desc}
- Type: {exp_type}
- Result: {status.upper()} (val_bpb: {val_bpb:.6f})
- Update note: {hypothesis_update}

Rules:
- If kept: move to "Confirmed hypotheses" section
- If discarded: move to "Rejected hypotheses" section
- Add to Research log table
- If this is the 3rd+ discard on the same axis, declare a dead zone
- Return the complete file
"""
    return call_claude(client, model, system, user, max_tokens=4096)


# ─── Main experiment loop ──────────────────────────────────────────────────────

def run_experiment(train_py_content: str, timeout: int = TRAIN_TIMEOUT) -> dict:
    """Write train.py, run it, return parsed metrics."""
    write_file(TRAIN_FILE, train_py_content)
    log(f"Running train.py (timeout: {timeout}s)...")

    start = time.time()
    try:
        rc, stdout, stderr = run_cmd(
            f"uv run {TRAIN_FILE} > {LOG_FILE} 2>&1",
            timeout=timeout
        )
        elapsed = time.time() - start
        log(f"Training finished in {elapsed:.0f}s (rc={rc})")
    except subprocess.TimeoutExpired:
        log(f"Training TIMEOUT after {timeout}s — treating as crash")
        return {}

    metrics = parse_train_output(LOG_FILE)
    if not metrics:
        log("No metrics found in run.log — crash")
        # Print last 20 lines for debugging
        try:
            lines = Path(LOG_FILE).read_text().splitlines()[-20:]
            log("Last 20 lines of run.log:")
            for line in lines:
                print(f"    {line}")
        except Exception:
            pass
    else:
        log(f"val_bpb={metrics.get('val_bpb', '?'):.6f}  "
            f"vram={metrics.get('peak_vram_mb', 0)/1024:.1f}GB  "
            f"params={metrics.get('num_params_M', 0):.0f}M")
    return metrics


def get_best_val_bpb() -> float:
    """Get best val_bpb from results.tsv. Returns 999 if no kept experiments."""
    p = Path(RESULTS_FILE)
    if not p.exists():
        return 999.0
    best = 999.0
    for line in p.read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 4 and parts[3] == "keep":
            try:
                best = min(best, float(parts[1]))
            except ValueError:
                pass
    return best


def main():
    parser = argparse.ArgumentParser(description="autoresearch 2.0 — autonomous Agent 1")
    parser.add_argument("--tag", default=None,
                        help="Run tag (default: today's date, e.g. mar30)")
    parser.add_argument("--max-experiments", type=int, default=0,
                        help="Stop after N experiments (0 = run forever)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Propose experiments but do not edit train.py or commit")
    parser.add_argument("--resume", action="store_true",
                        help="Resume existing branch instead of creating new one")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model (default: {DEFAULT_MODEL})")
    parser.add_argument("--meta-every", type=int, default=META_TRIGGER_EVERY,
                        help=f"Run meta-researcher every N experiments (default: {META_TRIGGER_EVERY})")
    parser.add_argument("--skip-meta", action="store_true",
                        help="Never run meta-researcher (Agent 2)")
    args = parser.parse_args()

    # Check API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # Check uv
    rc, _, _ = run_cmd("which uv", timeout=5)
    if rc != 0:
        print("ERROR: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh")
        sys.exit(1)

    # Tag
    tag = args.tag or datetime.now().strftime("%b%d").lower()

    print(f"\n{'='*60}")
    print(f"  autoresearch 2.0 — autonomous agent")
    print(f"  model:   {args.model}")
    print(f"  tag:     {tag}")
    print(f"  dry-run: {args.dry_run}")
    print(f"  max exp: {args.max_experiments or 'unlimited'}")
    print(f"  meta every: {args.meta_every} experiments")
    print(f"{'='*60}\n")

    client = anthropic.Anthropic()

    # Setup branch
    if not args.resume:
        setup_branch(tag)

    # Init files
    init_results_tsv()
    if not Path(HYPOTHESIS_FILE).exists():
        write_file(HYPOTHESIS_FILE, read_file(
            Path(__file__).parent / "hypothesis_log.md"
        ) if (Path(__file__).parent / "hypothesis_log.md").exists() else
            "# Hypothesis log\n\n## Active hypotheses\n- [H-000] Baseline — UNTESTED\n\n## Confirmed hypotheses (kept)\n\n## Rejected hypotheses (discarded)\n\n## Dead zones\n\n## Research log\n\n| # | Hypothesis | Reasoning | Result |\n|---|------------|-----------|--------|\n")

    experiment_num = count_experiments()
    log(f"Starting from experiment #{experiment_num + 1}")

    # ─── Main loop ────────────────────────────────────────────────────────────
    while True:
        experiment_num = count_experiments()

        if args.max_experiments > 0 and experiment_num >= args.max_experiments:
            log(f"Reached max experiments ({args.max_experiments}). Stopping.")
            break

        log(f"\n{'─'*50}")
        log(f"Experiment #{experiment_num + 1}")
        log(f"{'─'*50}")

        # Read current state
        program_text = read_file(PROGRAM_FILE)
        train_text = read_file(TRAIN_FILE)
        prepare_text = read_file(PREPARE_FILE)
        hypothesis_text = read_file(HYPOTHESIS_FILE)
        results_text = read_file(RESULTS_FILE) if Path(RESULTS_FILE).exists() else ""

        # ── Step 1: Propose experiment ────────────────────────────────────────
        log("Asking Claude to propose experiment...")
        try:
            proposal = propose_experiment(
                client, args.model,
                program_text, train_text, prepare_text,
                hypothesis_text, results_text
            )
        except Exception as e:
            log(f"ERROR proposing experiment: {e}. Retrying in 30s...")
            time.sleep(30)
            continue

        hypothesis_id = proposal.get("hypothesis_id", "H-???")
        hypothesis_desc = proposal.get("hypothesis_desc", "unknown")
        exp_type = proposal.get("exp_type", "[EXPLOIT]")
        reasoning = proposal.get("reasoning", "")
        commit_msg = proposal.get("commit_message", f"experiment {hypothesis_id}")
        new_train_py = proposal.get("new_train_py", train_text)

        log(f"Hypothesis: [{hypothesis_id}] {hypothesis_desc}")
        log(f"Type: {exp_type}")
        log(f"Reasoning: {reasoning}")

        if args.dry_run:
            log("DRY RUN — skipping file write, commit, and training")
            log(f"Would write {len(new_train_py)} chars to train.py")
            time.sleep(2)
            continue

        # ── Step 2: Run experiment ────────────────────────────────────────────
        metrics = run_experiment(new_train_py)
        is_crash = not metrics or "val_bpb" not in metrics

        # ── Step 3: Decide keep/discard ───────────────────────────────────────
        previous_best = get_best_val_bpb()

        if is_crash:
            log("Crash — logging and reverting")
            commit_hash = git_commit(f"crash: {commit_msg}")
            append_result(
                commit_hash, 0.0, 0.0, "crash",
                exp_type, hypothesis_id,
                f"CRASH: {hypothesis_desc[:50]}"
            )
            git_reset()
            # Update hypothesis log
            new_hypothesis_text = update_hypothesis_log(
                client, args.model, hypothesis_text,
                hypothesis_id, hypothesis_desc, exp_type,
                "crash", "Run crashed (OOM or bug)", 0.0
            )
            write_file(HYPOTHESIS_FILE, new_hypothesis_text)
            continue

        val_bpb = metrics["val_bpb"]
        memory_gb = metrics.get("peak_vram_mb", 0) / 1024

        log(f"Asking Claude to decide keep/discard (previous best: {previous_best:.6f})")
        try:
            decision = decide_keep_or_discard(
                client, args.model, metrics, previous_best,
                hypothesis_desc, reasoning
            )
        except Exception as e:
            log(f"ERROR in decision: {e}. Falling back to simple comparison.")
            status = "keep" if val_bpb < previous_best else "discard"
            decision = {"status": status, "summary": hypothesis_desc[:60], "hypothesis_update": ""}

        status = decision.get("status", "discard")
        summary = decision.get("summary", hypothesis_desc[:60])
        hypothesis_update = decision.get("hypothesis_update", "")

        log(f"Decision: {status.upper()} (val_bpb: {val_bpb:.6f})")

        # ── Step 4: Commit or reset ───────────────────────────────────────────
        commit_hash = git_commit(f"{status}: {commit_msg}")

        if status == "discard":
            git_reset()
            log("Reverted — discarded experiment")

        # ── Step 5: Log result ────────────────────────────────────────────────
        append_result(
            commit_hash, val_bpb, memory_gb,
            status, exp_type, hypothesis_id, summary
        )

        # ── Step 6: Update hypothesis log ────────────────────────────────────
        log("Updating hypothesis_log.md...")
        try:
            new_hypothesis_text = update_hypothesis_log(
                client, args.model, hypothesis_text,
                hypothesis_id, hypothesis_desc, exp_type,
                status, hypothesis_update, val_bpb
            )
            write_file(HYPOTHESIS_FILE, new_hypothesis_text)
        except Exception as e:
            log(f"WARNING: Failed to update hypothesis log: {e}")

        # ── Step 7: Meta-researcher trigger ───────────────────────────────────
        current_count = count_experiments()
        if (not args.skip_meta and
                current_count > 0 and
                current_count % args.meta_every == 0 and
                Path("meta_runner.py").exists()):
            log(f"\n{'='*50}")
            log(f"META-RESEARCHER TRIGGER ({current_count} experiments)")
            log(f"{'='*50}")
            try:
                rc, out, err = run_cmd(
                    f"python meta_runner.py "
                    f"--results {RESULTS_FILE} "
                    f"--program {PROGRAM_FILE} "
                    f"--hypothesis {HYPOTHESIS_FILE}",
                    timeout=300
                )
                print(out)
                if err:
                    log(f"meta_runner stderr: {err[:200]}")
                log("Re-reading program.md after meta-cycle...")
            except Exception as e:
                log(f"WARNING: meta_runner.py failed: {e}. Continuing with current program.md.")

        # Small pause between experiments
        time.sleep(2)

    log("\nAgent stopped.")
    log(f"Total experiments: {count_experiments()}")
    log(f"Best val_bpb: {get_best_val_bpb():.6f}")


if __name__ == "__main__":
    main()
