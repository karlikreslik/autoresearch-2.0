"""
orchestrator.py — Třívrstvý autoresearch orchestrátor
======================================================
Deepseekova kritika: "Dvě úrovně nestačí. Potřebuješ buď člověka
v meta-meta-smyčce, nebo přiznat, že tvůj systém je jen otevřená smyčka."

Toto je odpověď: skutečná uzavřená smyčka se třemi vrstvami.

Architektura:
  Vrstva 3 — MetaAuditor     → hlídá integritu, detekuje gamifikaci, circuit breaker
  Vrstva 2 — MetaResearcher  → hypotézy z kauzální paměti, strategie, program.md
  Vrstva 1 — Researcher      → Claude Code / agent spouštějící train.py

Smyčka:
  while True:
    1. [L3] Audit: Ověř integritu posledního experimentu, aktualizuj kauzální log
    2. [L3] Detekuj gaming, zkontroluj circuit breaker
    3. [L2] Přečti kauzální paměť → vygeneruj hypotézu → aktualizuj program.md
    4. [L1] Spusť researcher agenta (Claude Code nebo vlastní train.py smyčka)
    5. [L2] Anotuj výsledek do kauzálního logu (hypotéza + tagy)
    6. Zpět na 1.

Spouštění:
  python orchestrator.py --mode full      # Plná třívrstvá smyčka
  python orchestrator.py --mode audit     # Jen audit (bez spouštění experimentů)
  python orchestrator.py --mode status    # Přehled aktuálního stavu
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Přidat adresář do cesty
sys.path.insert(0, str(Path(__file__).parent))
from meta_auditor import MetaAuditor
from meta_researcher_v2 import MetaResearcher


# ─── Konfigurace ──────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "program_md": "program.md",
    "results_tsv": "results.tsv",
    "causal_log": "causal_memory.jsonl",
    "max_iterations": 100,       # Celkový limit experimentů
    "meta_interval": 5,          # Po kolika experimentech spustit meta-iteraci
    "audit_interval": 3,         # Po kolika experimentech spustit audit
    "researcher_command": [      # Jak spustit vrstvu 1 (přizpůsobit)
        "claude",                # Claude Code CLI
        "--print",
        "program.md",
        "--allowedTools", "Bash,Edit,Write,Read"
    ],
    "train_command": [           # Alternativa: přímé spuštění bez Claude Code
        "uv", "run", "train.py"
    ],
    "use_direct_train": True,    # True = přímý train.py, False = Claude Code
}


class ThreeLayerOrchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.program_md = Path(config["program_md"])
        self.results_tsv = Path(config["results_tsv"])
        self.causal_log = Path(config["causal_log"])

        self.auditor = MetaAuditor(self.results_tsv, self.causal_log)
        self.researcher = MetaResearcher(self.program_md, self.causal_log, self.results_tsv)

        self.experiment_count = 0
        self.run_log: list[dict] = []

    # ─── Vrstva 1: Spouštění experimentu ─────────────────────────────────────

    def run_researcher_layer(self) -> dict:
        """
        Spustí vrstvu 1: buď Claude Code agent, nebo přímý train.py.
        Vrátí výsledek experimentu.
        """
        print(f"[L1-Researcher] Spouštím experiment {self.experiment_count + 1}…")

        if self.config["use_direct_train"]:
            # Přímé spuštění train.py (pro testování bez Claude Code)
            cmd = self.config["train_command"]
        else:
            # Claude Code spustí celou smyčku podle program.md
            cmd = self.config["researcher_command"]

        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minut max
                cwd=str(self.program_md.parent)
            )
            duration = time.time() - start
            output = result.stdout + result.stderr

            # Extrahovat metriku z výstupu
            metric = None
            for line in output.splitlines():
                if line.startswith("val_bpb:"):
                    try:
                        metric = float(line.split(":")[1].strip())
                    except ValueError:
                        pass

            success = result.returncode == 0 and metric is not None

            return {
                "success": success,
                "metric": metric,
                "duration_s": duration,
                "returncode": result.returncode,
                "stdout_tail": output[-2000:] if output else "",
            }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "metric": None,
                "duration_s": 600,
                "returncode": -1,
                "stdout_tail": "TIMEOUT",
            }
        except Exception as e:
            return {
                "success": False,
                "metric": None,
                "duration_s": 0,
                "returncode": -1,
                "stdout_tail": str(e),
            }

    # ─── Vrstva 2+3: Meta cyklus ──────────────────────────────────────────────

    def run_meta_cycle(self, force_audit: bool = False) -> bool:
        """
        Spustí vrstvu 2 a 3.
        Vrátí False pokud má smyčka skončit (circuit breaker).
        """
        should_audit = (
            force_audit
            or self.experiment_count % self.config["audit_interval"] == 0
        )
        should_meta = (
            self.experiment_count % self.config["meta_interval"] == 0
        )

        if should_audit or should_meta:
            print(f"\n[Orchestrator] Meta-cyklus po {self.experiment_count} experimentech")
            continue_loop = self.researcher.run_meta_iteration(run_audit=should_audit)
            if not continue_loop:
                return False

        return True

    # ─── Status přehled ───────────────────────────────────────────────────────

    def print_status(self):
        """Kompaktní přehled stavu všech tří vrstev."""
        rows = self.auditor.load_tsv()
        keeps = [r for r in rows if r.get("status") == "keep"]
        discards = [r for r in rows if r.get("status") == "discard"]
        fails = [r for r in self.auditor.records if r.status == "AUDIT_FAIL"]

        print("\n" + "="*70)
        print("TŘÍVRSTVÝ AUTORESEARCH — STATUS")
        print("="*70)
        print(f"Celkem experimentů:    {len(rows)}")
        print(f"Keep:                  {len(keeps)}")
        print(f"Discard:               {len(discards)}")
        print(f"Audit selhání:         {len(fails)}")
        print(f"Circuit breaker fires: {self.auditor.consecutive_audit_failures}")
        print()
        print("--- Kauzální paměť ---")
        print(self.auditor.summarize_memory())
        print()
        print("--- Gaming detekce ---")
        gaming = self.auditor.detect_metric_gaming(rows)
        status_icon = "⚠️  PODEZŘELÉ" if gaming["suspicious"] else "✓  OK"
        print(f"{status_icon}: {gaming['reason']}")
        print("="*70 + "\n")

    # ─── Hlavní smyčka ────────────────────────────────────────────────────────

    def run(self, max_iterations: Optional[int] = None):
        """
        Hlavní třívrstvá smyčka.
        """
        max_iter = max_iterations or self.config["max_iterations"]
        print(f"[Orchestrator] Spouštím třívrstvý autoresearch (max {max_iter} iterací)")
        print(f"[Orchestrator] Meta-interval: každých {self.config['meta_interval']} experimentů")
        print(f"[Orchestrator] Audit-interval: každých {self.config['audit_interval']} experimentů")

        # Úvodní meta-cyklus (inicializace program.md)
        print("\n[Orchestrator] Inicializační meta-cyklus…")
        if not self.run_meta_cycle(force_audit=False):
            print("[Orchestrator] Inicializace selhala — ukončuji.")
            return

        while self.experiment_count < max_iter:
            print(f"\n[Orchestrator] ─── Experiment {self.experiment_count + 1}/{max_iter} ───")

            # Vrstva 1: Spustit experiment
            result = self.run_researcher_layer()
            self.experiment_count += 1

            # Log
            self.run_log.append({
                "experiment": self.experiment_count,
                "metric": result["metric"],
                "success": result["success"],
                "duration_s": result["duration_s"],
            })

            if not result["success"]:
                print(f"[L1] Experiment selhal (returncode={result['returncode']})")
                print(f"[L1] Tail: {result['stdout_tail'][-500:]}")
            else:
                print(f"[L1] Experiment dokončen: val_bpb={result['metric']:.6f} za {result['duration_s']:.0f}s")

            # Vrstva 2+3: Meta a audit
            if not self.run_meta_cycle():
                print("[Orchestrator] Circuit breaker — smyčka zastavena.")
                break

        # Závěrečný status
        print("\n[Orchestrator] Smyčka dokončena.")
        self.print_status()

        # Uložit run log
        log_path = Path("run_log.jsonl")
        with open(log_path, "w") as f:
            for entry in self.run_log:
                f.write(json.dumps(entry) + "\n")
        print(f"[Orchestrator] Run log uložen do {log_path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Třívrstvý autoresearch orchestrátor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Příklady:
  python orchestrator.py --mode full         # Plná smyčka
  python orchestrator.py --mode audit        # Jen audit integrity
  python orchestrator.py --mode status       # Přehled stavu
  python orchestrator.py --mode meta         # Jen meta-iterace (debug)
  python orchestrator.py --max-iter 50       # Omezit počet experimentů
        """
    )
    parser.add_argument("--mode", choices=["full", "audit", "status", "meta"],
                        default="full", help="Režim spouštění")
    parser.add_argument("--max-iter", type=int, default=None,
                        help="Maximální počet experimentů (override konfigurace)")
    parser.add_argument("--program", type=str, default="program.md")
    parser.add_argument("--results", type=str, default="results.tsv")
    parser.add_argument("--causal-log", type=str, default="causal_memory.jsonl")
    parser.add_argument("--meta-interval", type=int, default=5)
    parser.add_argument("--audit-interval", type=int, default=3)
    parser.add_argument("--direct-train", action="store_true",
                        help="Použít přímý train.py místo Claude Code")
    args = parser.parse_args()

    config = {**DEFAULT_CONFIG}
    config["program_md"] = args.program
    config["results_tsv"] = args.results
    config["causal_log"] = args.causal_log
    config["meta_interval"] = args.meta_interval
    config["audit_interval"] = args.audit_interval
    config["use_direct_train"] = args.direct_train

    orchestrator = ThreeLayerOrchestrator(config)

    if args.mode == "status":
        orchestrator.print_status()

    elif args.mode == "audit":
        report = orchestrator.auditor.run_audit_cycle(sample=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif args.mode == "meta":
        continue_loop = orchestrator.researcher.run_meta_iteration(run_audit=False)
        print(f"Pokračovat: {continue_loop}")

    elif args.mode == "full":
        orchestrator.run(max_iterations=args.max_iter)


# Přidat Optional import
from typing import Optional

if __name__ == "__main__":
    main()
