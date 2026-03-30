#!/usr/bin/env python3
"""
meta_runner.py — autoresearch 2.0

Spouštěč Meta-researchera (Agent 2). Voláno automaticky Researcherem (Agent 1)
každých N experimentů, nebo ručně uživatelem.

Použití:
    python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md
    python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --dry-run
    python meta_runner.py --results results.tsv --program program.md --hypothesis hypothesis_log.md --force

Flags:
    --dry-run   Analyze only, print proposed changes, do NOT write files
    --force     Run even if <20 experiments since last meta-cycle
    --model     Anthropic model to use (default: claude-opus-4-5)
    --window    Number of experiments to analyze (default: 20)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic


# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-opus-4-5"
DEFAULT_WINDOW = 20
META_RESULTS_FILE = "meta_results.tsv"
PROGRAM_HISTORY_DIR = "program_history"
META_PROGRAM_FILE = "meta_program.md"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"[FILE NOT FOUND: {path}]"
    return p.read_text(encoding="utf-8")


def parse_results_tsv(path: str) -> list[dict]:
    """Parse results.tsv into list of dicts. Handles both v1 and v2 formats."""
    p = Path(path)
    if not p.exists():
        return []

    lines = p.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 2:
        return []

    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        row = dict(zip(header, values))
        rows.append(row)
    return rows


def get_current_version(program_text: str) -> str:
    """Extract current version from program.md."""
    match = re.search(r"Current version:\s*\*\*([0-9.]+)\*\*", program_text)
    if match:
        return match.group(1)
    return "2.0"


def bump_version(version: str) -> str:
    """Bump minor version: 2.0 → 2.1, 2.9 → 2.10"""
    parts = version.split(".")
    if len(parts) == 2:
        return f"{parts[0]}.{int(parts[1]) + 1}"
    return version + ".1"


def count_experiments_since_last_meta(meta_results_path: str) -> int:
    """Count how many experiments happened since last meta-cycle."""
    p = Path(meta_results_path)
    if not p.exists():
        return 9999  # never ran, so effectively infinite

    lines = p.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 2:
        return 9999

    # Last line has the last meta-cycle's experiment count
    last = lines[-1].split("\t")
    try:
        return int(last[1])  # experiments_analyzed field (proxy for cumulative count)
    except (IndexError, ValueError):
        return 9999


def save_program_version(program_text: str, version: str) -> None:
    """Archive current program.md before overwriting."""
    history_dir = Path(PROGRAM_HISTORY_DIR)
    history_dir.mkdir(exist_ok=True)
    archive_path = history_dir / f"program_v{version}.md"
    archive_path.write_text(program_text, encoding="utf-8")
    print(f"  [meta] Archived current program.md → {archive_path}")


def log_meta_result(
    version: str,
    n_analyzed: int,
    trend: str,
    explore_ratio: float,
    dead_zones_unlocked: int,
    hypotheses_added: int,
    program_changed: bool,
    summary: str,
) -> None:
    """Append one row to meta_results.tsv."""
    p = Path(META_RESULTS_FILE)
    header = "version\texperiments_analyzed\ttrend\texplore_ratio\tdead_zones_unlocked\thypotheses_added\tprogram_changed\tsummary\ttimestamp"

    if not p.exists():
        p.write_text(header + "\n", encoding="utf-8")

    row = "\t".join([
        version,
        str(n_analyzed),
        trend,
        f"{explore_ratio:.2f}",
        str(dead_zones_unlocked),
        str(hypotheses_added),
        "yes" if program_changed else "no",
        summary,
        datetime.now().isoformat(timespec="seconds"),
    ])

    with p.open("a", encoding="utf-8") as f:
        f.write(row + "\n")

    print(f"  [meta] Logged to {META_RESULTS_FILE}: v{version} | {trend} | changed={program_changed}")


# ─── Core: call Claude ────────────────────────────────────────────────────────

def run_meta_researcher(
    results_rows: list[dict],
    program_text: str,
    hypothesis_text: str,
    meta_program_text: str,
    model: str,
    window: int,
    dry_run: bool,
) -> dict:
    """
    Call Claude with full context and get structured meta-analysis back.
    Returns dict with keys: new_program, new_hypothesis, meta_record, changed
    """

    client = anthropic.Anthropic()

    # Prepare context
    recent_rows = results_rows[-window:] if len(results_rows) >= window else results_rows
    all_rows_tsv = "\t".join(["commit", "val_bpb", "memory_gb", "status", "type", "hypothesis_id", "description"]) + "\n"
    for row in results_rows:
        all_rows_tsv += "\t".join(row.values()) + "\n"

    recent_tsv = "\t".join(["commit", "val_bpb", "memory_gb", "status", "type", "hypothesis_id", "description"]) + "\n"
    for row in recent_rows:
        recent_tsv += "\t".join(row.values()) + "\n"

    current_version = get_current_version(program_text)
    new_version = bump_version(current_version)

    # System prompt — strict JSON output
    system = """You are the Meta-researcher in the autoresearch 2.0 system.
Your job is to analyze experimental results and improve the Researcher's instructions (program.md).

You MUST respond with valid JSON only. No markdown fences, no preamble, no explanation outside JSON.

The JSON schema is:
{
  "changed": true/false,
  "trend": "FAST" | "SLOW" | "STALLED",
  "explore_ratio_recent": 0.0-1.0,
  "explore_ratio_recommended": 0.0-1.0,
  "dead_zones_unlocked": integer,
  "hypotheses_added": integer,
  "summary": "one sentence summary of what you changed and why",
  "new_program_md": "full text of revised program.md (or null if unchanged)",
  "new_hypothesis_log": "full text of revised hypothesis_log.md (or null if unchanged)",
  "meta_section": "markdown text for the Meta-researcher recommendations section"
}

If changed=false, new_program_md and new_hypothesis_log must be null.
If changed=true, new_program_md must be the COMPLETE revised file, not just a diff.
"""

    user = f"""## Your instructions (meta_program.md)

{meta_program_text}

---

## Current program.md (v{current_version})

{program_text}

---

## Current hypothesis_log.md

{hypothesis_text}

---

## Full results history (all experiments)

{all_rows_tsv}

---

## Recent results (last {window} experiments — your primary analysis window)

{recent_tsv}

---

## Your task

Analyze the above and produce a revised program.md (version {new_version}) and updated
hypothesis_log.md. Follow meta_program.md instructions strictly.

Remember:
- Only change program.md if you have concrete, data-driven reasons
- new_program_md must contain the COMPLETE file text
- Update "Current version" field in program.md to {new_version}
- Update "Meta-researcher updates" counter in program.md
- Do not destroy any structural sections
- Your JSON must be parseable — escape all special characters properly
"""

    print(f"  [meta] Calling {model} for meta-analysis...")
    print(f"  [meta] Analyzing {len(results_rows)} total / {len(recent_rows)} recent experiments")

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": user}],
        system=system,
    )

    raw = message.content[0].text.strip()

    # Parse JSON — handle potential markdown fences defensively
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [meta] ERROR: Claude returned invalid JSON: {e}")
        print(f"  [meta] Raw response (first 500 chars):\n{raw[:500]}")
        return {
            "changed": False,
            "trend": "UNKNOWN",
            "explore_ratio_recent": 0.0,
            "explore_ratio_recommended": 0.7,
            "dead_zones_unlocked": 0,
            "hypotheses_added": 0,
            "summary": f"Meta-cycle failed: JSON parse error — {e}",
            "new_program_md": None,
            "new_hypothesis_log": None,
            "meta_section": "",
        }

    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="autoresearch 2.0 — Meta-researcher runner")
    parser.add_argument("--results", required=True, help="Path to results.tsv")
    parser.add_argument("--program", required=True, help="Path to program.md")
    parser.add_argument("--hypothesis", required=True, help="Path to hypothesis_log.md")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, do not write files")
    parser.add_argument("--force", action="store_true", help="Run even if <20 experiments since last cycle")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW, help=f"Analysis window size (default: {DEFAULT_WINDOW})")
    args = parser.parse_args()

    print("\n[meta_runner] autoresearch 2.0 — Meta-researcher starting")
    print(f"[meta_runner] Model: {args.model} | Window: {args.window} | Dry run: {args.dry_run}")

    # Check API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[meta_runner] ERROR: ANTHROPIC_API_KEY not set. Meta-cycle skipped.")
        sys.exit(1)

    # Read inputs
    results_rows = parse_results_tsv(args.results)
    program_text = read_file(args.program)
    hypothesis_text = read_file(args.hypothesis)
    meta_program_text = read_file(META_PROGRAM_FILE)

    print(f"[meta_runner] Loaded {len(results_rows)} experiments from {args.results}")

    # Guard: minimum experiments
    if not args.force and len(results_rows) < args.window:
        print(f"[meta_runner] Only {len(results_rows)} experiments found, need {args.window}. Use --force to override.")
        sys.exit(0)

    # Run meta-researcher
    result = run_meta_researcher(
        results_rows=results_rows,
        program_text=program_text,
        hypothesis_text=hypothesis_text,
        meta_program_text=meta_program_text,
        model=args.model,
        window=args.window,
        dry_run=args.dry_run,
    )

    # Report
    print(f"\n[meta_runner] Analysis complete:")
    print(f"  Trend:                  {result.get('trend', 'UNKNOWN')}")
    print(f"  Recent explore ratio:   {result.get('explore_ratio_recent', 0):.0%}")
    print(f"  Recommended ratio:      {result.get('explore_ratio_recommended', 0.7):.0%}")
    print(f"  Dead zones unlocked:    {result.get('dead_zones_unlocked', 0)}")
    print(f"  Hypotheses added:       {result.get('hypotheses_added', 0)}")
    print(f"  Program changed:        {result.get('changed', False)}")
    print(f"  Summary:                {result.get('summary', '')}")

    if args.dry_run:
        print("\n[meta_runner] DRY RUN — no files written.")
        if result.get("new_program_md"):
            print("\n--- PROPOSED program.md changes (first 2000 chars) ---")
            print(result["new_program_md"][:2000])
        sys.exit(0)

    # Write files
    current_version = get_current_version(program_text)

    if result.get("changed") and result.get("new_program_md"):
        save_program_version(program_text, current_version)
        Path(args.program).write_text(result["new_program_md"], encoding="utf-8")
        print(f"  [meta] Written new program.md (v{bump_version(current_version)})")
    else:
        print("  [meta] program.md unchanged — no new version created")

    if result.get("new_hypothesis_log"):
        Path(args.hypothesis).write_text(result["new_hypothesis_log"], encoding="utf-8")
        print(f"  [meta] Updated hypothesis_log.md")

    # Log meta result
    log_meta_result(
        version=bump_version(current_version) if result.get("changed") else current_version,
        n_analyzed=min(len(results_rows), args.window),
        trend=result.get("trend", "UNKNOWN"),
        explore_ratio=result.get("explore_ratio_recent", 0.0),
        dead_zones_unlocked=result.get("dead_zones_unlocked", 0),
        hypotheses_added=result.get("hypotheses_added", 0),
        program_changed=result.get("changed", False),
        summary=result.get("summary", ""),
    )

    print("\n[meta_runner] Done. Researcher may now re-read program.md and resume.\n")


if __name__ == "__main__":
    main()
