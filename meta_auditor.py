"""
meta_auditor.py — Vrstva 3: Meta-meta auditor
==============================================
Řeší Deepseekovu kritiku:
  1. Meta-researcher může "podvádět" — gamifikovat metriku bez skutečného zlepšení.
  2. Lineární TSV log nemá paměť kauzality — nevíme PROČ něco fungovalo.

Jak to funguje:
  - Spouštíme nezávislé re-run vybraných "keep" experimentů a porovnáme výsledky.
  - Detekujeme drift: trend metrik vs. skutečná variabilita.
  - Udržujeme strukturovaný kauzální log (JSONL) místo holého TSV.
  - Circuit breaker: pokud detekujeme podvod/divergenci, zastavíme smyčku.

Spouštění:
  python meta_auditor.py --results results.tsv --causal-log causal_memory.jsonl
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean, stdev
from typing import Optional


# ─── Datové struktury ────────────────────────────────────────────────────────

@dataclass
class ExperimentRecord:
    """Jeden záznam v kauzálním logu — více informací než holý TSV řádek."""
    iteration: int
    commit: str
    metric: float
    status: str          # keep | discard | crash | AUDIT_FAIL
    description: str
    hypothesis: str      # Proč jsme to zkusili (doplní meta-researcher)
    causal_parents: list[str]   # Commity, na kterých toto staví
    audit_rerun: Optional[float]  # Výsledek nezávislého re-runu auditem (nebo None)
    audit_delta: Optional[float]  # Rozdíl mezi originálním a re-run výsledkem
    tags: list[str]      # ["architecture", "optimizer", "lr_schedule", ...]
    timestamp: float

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class AuditVerdict:
    commit: str
    original_metric: float
    rerun_metric: float
    delta: float
    passed: bool
    reason: str


# ─── Konfigurace ──────────────────────────────────────────────────────────────

AUDIT_TOLERANCE = 0.005       # Povolená odchylka mezi originálním a re-run výsledkem
DRIFT_WINDOW = 10             # Kolik posledních experimentů sledujeme pro trend
DRIFT_MIN_IMPROVEMENT = 0.001 # Pokud trend klesá méně než toto za okno, jsme "zaseknutí"
SUSPICIOUS_STREAK = 5         # Kolik "keep" za sebou bez skutečného zlepšení = podezření
AUDIT_SAMPLE_RATE = 0.2       # Auditujeme 20 % "keep" experimentů (náhodný výběr)
CIRCUIT_BREAK_THRESHOLD = 3   # Kolik AUDIT_FAIL za sebou = zastavit smyčku


class MetaAuditor:
    def __init__(self, results_tsv: Path, causal_log: Path):
        self.results_tsv = results_tsv
        self.causal_log = causal_log
        self.records: list[ExperimentRecord] = []
        self.consecutive_audit_failures = 0

        if causal_log.exists():
            self._load_causal_log()

    # ─── Načítání ────────────────────────────────────────────────────────────

    def _load_causal_log(self):
        with open(self.causal_log) as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    self.records.append(ExperimentRecord(**d))
        print(f"[Auditor] Načteno {len(self.records)} záznamů z kauzálního logu.")

    def load_tsv(self) -> list[dict]:
        """Načte results.tsv a vrátí seznam řádků."""
        rows = []
        if not self.results_tsv.exists():
            return rows
        with open(self.results_tsv) as f:
            lines = f.readlines()
        if len(lines) < 2:
            return rows
        headers = lines[0].strip().split("\t")
        for line in lines[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= len(headers):
                rows.append(dict(zip(headers, parts)))
        return rows

    # ─── Integrita metrik ─────────────────────────────────────────────────────

    def verify_metric_integrity(self, commit: str, expected_metric: float) -> AuditVerdict:
        """
        Nezávislý re-run: checkout committu, spuštění train.py, porovnání výsledku.
        Odhaluje gamifikaci (agent upraví train.py tak, aby reportoval falešnou metriku).
        """
        print(f"[Auditor] Verifikuji commit {commit[:7]}… (očekávaná metrika: {expected_metric:.6f})")

        try:
            # Checkout commit do dočasné větve
            subprocess.run(
                ["git", "checkout", "-B", "audit/verify", commit],
                check=True, capture_output=True
            )

            # Spustit trénink znovu
            result = subprocess.run(
                ["uv", "run", "train.py"],
                capture_output=True, text=True, timeout=400
            )
            output = result.stdout + result.stderr

            # Extrahovat metriku
            rerun_metric = None
            for line in output.splitlines():
                if line.startswith("val_bpb:"):
                    rerun_metric = float(line.split(":")[1].strip())
                    break

            if rerun_metric is None:
                return AuditVerdict(
                    commit=commit,
                    original_metric=expected_metric,
                    rerun_metric=float("nan"),
                    delta=float("nan"),
                    passed=False,
                    reason="Re-run se zhroutil — nelze extrahovat val_bpb"
                )

            delta = abs(rerun_metric - expected_metric)
            passed = delta <= AUDIT_TOLERANCE

            return AuditVerdict(
                commit=commit,
                original_metric=expected_metric,
                rerun_metric=rerun_metric,
                delta=delta,
                passed=passed,
                reason=(
                    "OK — výsledky konzistentní"
                    if passed
                    else f"SELHÁNÍ — delta {delta:.6f} > tolerance {AUDIT_TOLERANCE}"
                )
            )

        except subprocess.TimeoutExpired:
            return AuditVerdict(
                commit=commit,
                original_metric=expected_metric,
                rerun_metric=float("nan"),
                delta=float("nan"),
                passed=False,
                reason="Re-run timeout"
            )
        except Exception as e:
            return AuditVerdict(
                commit=commit,
                original_metric=expected_metric,
                rerun_metric=float("nan"),
                delta=float("nan"),
                passed=False,
                reason=f"Chyba: {e}"
            )

    # ─── Detekce driftu a gamifikace ─────────────────────────────────────────

    def detect_metric_gaming(self, rows: list[dict]) -> dict:
        """
        Detekuje podezřelé patterny:
        - Příliš mnoho "keep" za sebou bez skutečného zlepšení.
        - Metrika se zlepšuje přesně o stejnou hodnotu (suspiciously uniform).
        - Trend se zastavil, ale agent stále hlásí "keep".
        """
        keeps = [r for r in rows if r.get("status") == "keep"]
        if len(keeps) < 3:
            return {"suspicious": False, "reason": "Nedostatek dat"}

        metrics = [float(r["val_bpb"]) for r in keeps if r.get("val_bpb")]
        if len(metrics) < 3:
            return {"suspicious": False, "reason": "Nedostatek metrik"}

        # Test: uniform delta (agent hardcoded reportuje konstantní zlepšení)
        deltas = [metrics[i] - metrics[i+1] for i in range(len(metrics)-1)]
        if len(deltas) >= 3:
            delta_std = stdev(deltas) if len(deltas) > 1 else 0
            if delta_std < 0.0001 and mean(deltas) > 0:
                return {
                    "suspicious": True,
                    "reason": f"Uniformní delta {mean(deltas):.6f} ± {delta_std:.8f} — možná hardcoded hodnota"
                }

        # Test: trend se zastavil v posledním okně
        recent = metrics[-DRIFT_WINDOW:]
        if len(recent) >= DRIFT_WINDOW:
            total_improvement = recent[0] - recent[-1]
            if total_improvement < DRIFT_MIN_IMPROVEMENT:
                return {
                    "suspicious": True,
                    "reason": f"Trend zaseknutý: zlepšení {total_improvement:.6f} za posledních {DRIFT_WINDOW} iterací"
                }

        # Test: podezřelý počet "keep" za sebou
        consecutive_keeps = 0
        for r in reversed(rows):
            if r.get("status") == "keep":
                consecutive_keeps += 1
            else:
                break
        if consecutive_keeps >= SUSPICIOUS_STREAK:
            return {
                "suspicious": True,
                "reason": f"{consecutive_keeps} keep za sebou — statisticky nepravděpodobné bez plateau"
            }

        return {"suspicious": False, "reason": "Patterny v normálu"}

    # ─── Kauzální paměť ───────────────────────────────────────────────────────

    def ingest_new_experiments(self, rows: list[dict], known_commits: set[str]) -> list[ExperimentRecord]:
        """
        Zpracuje nové řádky z TSV a přidá je do kauzálního logu jako strukturované záznamy.
        Meta-researcher by měl doplnit 'hypothesis' a 'tags' — tady dáváme placeholder.
        """
        new_records = []
        prev_commit = None

        for r in rows:
            commit = r.get("commit", "unknown")
            if commit in known_commits:
                prev_commit = commit
                continue

            try:
                metric = float(r.get("val_bpb", "0"))
            except ValueError:
                metric = 0.0

            record = ExperimentRecord(
                iteration=len(self.records) + len(new_records),
                commit=commit,
                metric=metric,
                status=r.get("status", "unknown"),
                description=r.get("description", ""),
                hypothesis="[DOPLNIT meta-researcherem]",  # Vrstva 2 doplní
                causal_parents=[prev_commit] if prev_commit else [],
                audit_rerun=None,
                audit_delta=None,
                tags=[],        # Vrstva 2 doplní podle kategorie
                timestamp=time.time()
            )
            new_records.append(record)
            prev_commit = commit

        return new_records

    def append_to_causal_log(self, records: list[ExperimentRecord]):
        """Zapíše záznamy do JSONL souboru."""
        with open(self.causal_log, "a") as f:
            for rec in records:
                f.write(rec.to_jsonl() + "\n")
        print(f"[Auditor] Připsáno {len(records)} nových záznamů do kauzálního logu.")

    # ─── Strukturovaná paměť: dotazy ─────────────────────────────────────────

    def query_memory(self, tag: Optional[str] = None, status: Optional[str] = None,
                     top_n: int = 10) -> list[ExperimentRecord]:
        """
        Vrátí relevantní záznamy z kauzální paměti.
        Meta-researcher volá tuto metodu před návrhem nové hypotézy.
        """
        results = self.records
        if tag:
            results = [r for r in results if tag in r.tags]
        if status:
            results = [r for r in results if r.status == status]
        # Seřadit podle metriky (nejlepší první)
        results = sorted(results, key=lambda r: r.metric)
        return results[:top_n]

    def summarize_memory(self) -> str:
        """Vytvoří přehled pro meta-researchera — co fungovalo, co ne."""
        if not self.records:
            return "Kauzální paměť prázdná."

        keeps = [r for r in self.records if r.status == "keep"]
        discards = [r for r in self.records if r.status == "discard"]
        failures = [r for r in self.records if r.status == "AUDIT_FAIL"]

        lines = [
            f"=== Kauzální paměť: {len(self.records)} experimentů ===",
            f"Keep: {len(keeps)}  Discard: {len(discards)}  Audit selhání: {len(failures)}",
        ]

        if keeps:
            best = min(keeps, key=lambda r: r.metric)
            lines.append(f"Nejlepší: commit={best.commit[:7]}, val_bpb={best.metric:.6f}, popis={best.description}")

        # Nejčastější tagy u keep
        from collections import Counter
        tag_counts = Counter(tag for r in keeps for tag in r.tags)
        if tag_counts:
            lines.append("Úspěšné kategorie: " + ", ".join(f"{t}({c})" for t, c in tag_counts.most_common(5)))

        # Vzory, které selhaly
        fail_words: Counter = Counter()
        for r in discards:
            for word in r.description.lower().split():
                if len(word) > 4:
                    fail_words[word] += 1
        if fail_words:
            lines.append("Časté slova v neúspěšných: " + ", ".join(w for w, _ in fail_words.most_common(5)))

        return "\n".join(lines)

    # ─── Circuit breaker ─────────────────────────────────────────────────────

    def should_break_circuit(self) -> tuple[bool, str]:
        """
        Vrátí (True, důvod) pokud by měla smyčka skončit.
        Kontroluje: příliš mnoho audit selhání, plateaus, divergence.
        """
        if self.consecutive_audit_failures >= CIRCUIT_BREAK_THRESHOLD:
            return True, f"{self.consecutive_audit_failures} po sobě jdoucí audit selhání — smyčka zastavena"

        recent_fails = [r for r in self.records[-20:] if r.status == "AUDIT_FAIL"]
        if len(recent_fails) >= 5:
            return True, "Více než 5 audit selhání v posledních 20 experimentech"

        return False, ""

    # ─── Hlavní cyklus auditu ────────────────────────────────────────────────

    def run_audit_cycle(self, sample: bool = True) -> dict:
        """
        Hlavní funkce: načte TSV, zkontroluje integritu, aktualizuje kauzální log.
        Vrátí report pro meta-researchera (vrstva 2).
        """
        rows = self.load_tsv()
        if not rows:
            return {"status": "no_data", "message": "results.tsv je prázdný nebo chybí"}

        known_commits = {r.commit for r in self.records}

        # 1. Ingestion nových experimentů
        new_records = self.ingest_new_experiments(rows, known_commits)

        # 2. Detekce gamifikace
        gaming_check = self.detect_metric_gaming(rows)

        # 3. Nezávislý re-run (auditujeme sample % keep experimentů)
        import random
        audit_results: list[AuditVerdict] = []
        if sample:
            keep_rows = [r for r in rows if r.get("status") == "keep" and r.get("commit") not in known_commits]
            to_audit = random.sample(keep_rows, max(1, int(len(keep_rows) * AUDIT_SAMPLE_RATE))) if keep_rows else []

            for row in to_audit:
                verdict = self.verify_metric_integrity(row["commit"], float(row["val_bpb"]))
                audit_results.append(verdict)
                # Aktualizuj záznam
                for rec in new_records:
                    if rec.commit == row["commit"]:
                        rec.audit_rerun = verdict.rerun_metric
                        rec.audit_delta = verdict.delta
                        if not verdict.passed:
                            rec.status = "AUDIT_FAIL"
                            self.consecutive_audit_failures += 1
                        else:
                            self.consecutive_audit_failures = 0

        # 4. Uložit do kauzálního logu
        self.records.extend(new_records)
        self.append_to_causal_log(new_records)

        # 5. Circuit breaker check
        should_stop, stop_reason = self.should_break_circuit()

        # 6. Sestavit report
        report = {
            "status": "STOP" if should_stop else "CONTINUE",
            "stop_reason": stop_reason,
            "new_experiments": len(new_records),
            "gaming_detection": gaming_check,
            "audit_results": [
                {
                    "commit": v.commit[:7],
                    "passed": v.passed,
                    "delta": v.delta,
                    "reason": v.reason
                }
                for v in audit_results
            ],
            "memory_summary": self.summarize_memory(),
            "circuit_breaker_fires": self.consecutive_audit_failures,
        }

        # Vytisknout report
        print("\n" + "="*60)
        print("META-META AUDIT REPORT")
        print("="*60)
        print(f"Status: {report['status']}")
        if stop_reason:
            print(f"Stop důvod: {stop_reason}")
        print(f"Gamifikace: {gaming_check['suspicious']} — {gaming_check['reason']}")
        for ar in report["audit_results"]:
            icon = "✓" if ar["passed"] else "✗"
            print(f"  {icon} Commit {ar['commit']}: {ar['reason']}")
        print(report["memory_summary"])
        print("="*60 + "\n")

        return report


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meta-meta auditor pro autoresearch-2.0")
    parser.add_argument("--results", type=Path, default=Path("results.tsv"), help="Cesta k results.tsv")
    parser.add_argument("--causal-log", type=Path, default=Path("causal_memory.jsonl"), help="Kauzální log")
    parser.add_argument("--no-rerun", action="store_true", help="Přeskočit nezávislé re-runy (jen analýza)")
    parser.add_argument("--query", type=str, help="Dotaz do kauzální paměti (tag)")
    parser.add_argument("--summary", action="store_true", help="Zobrazit přehled paměti a skončit")
    args = parser.parse_args()

    auditor = MetaAuditor(args.results, args.causal_log)

    if args.summary:
        print(auditor.summarize_memory())
        return

    if args.query:
        results = auditor.query_memory(tag=args.query, top_n=10)
        for r in results:
            print(f"  commit={r.commit[:7]} metric={r.metric:.6f} status={r.status} desc={r.description}")
        return

    report = auditor.run_audit_cycle(sample=not args.no_rerun)

    if report["status"] == "STOP":
        print(f"[Auditor] CIRCUIT BREAKER: {report['stop_reason']}")
        sys.exit(1)  # Signál pro orchestrátor, že smyčka má skončit


if __name__ == "__main__":
    main()
