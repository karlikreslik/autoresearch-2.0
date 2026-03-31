"""
meta_researcher_v2.py — Vrstva 2: Meta-researcher s auditorskou integrací
=========================================================================
Původní meta-researcher navrhoval hypotézy a upravoval program.md.
Tato verze přidává:
  1. Povinné čtení kauzální paměti PŘED návrhem hypotézy.
  2. Tagování experimentů (architecture / optimizer / lr_schedule / data / regularization).
  3. Zápis hypothesis pole do každého záznamu.
  4. Respektování circuit breakeru od audítora (vrstva 3).
  5. Skutečnou nelineární paměť: dotazujeme se na kauzální vztahy, ne jen TSV.

Volání:
  python meta_researcher_v2.py \
    --program program.md \
    --causal-log causal_memory.jsonl \
    --iterations 5
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Importujeme auditor
sys.path.insert(0, str(Path(__file__).parent))
from meta_auditor import MetaAuditor, ExperimentRecord


# ─── Kategorie experimentů ────────────────────────────────────────────────────

EXPERIMENT_TAGS = {
    "architecture": ["layer", "head", "embed", "depth", "width", "attention", "ffn", "norm"],
    "optimizer": ["lr", "learning_rate", "muon", "adamw", "momentum", "weight_decay", "schedule"],
    "lr_schedule": ["warmup", "cosine", "decay", "cycle", "cooldown"],
    "regularization": ["dropout", "clip", "grad", "regulariz", "weight"],
    "data": ["batch", "seq_len", "token", "dataset", "augment"],
    "architecture_search": ["block", "residual", "skip", "rope", "alibi", "mqa", "gqa"],
}


def infer_tags(description: str) -> list[str]:
    """Automaticky odhadne kategorie experimentu z popisu."""
    desc_lower = description.lower()
    tags = []
    for tag, keywords in EXPERIMENT_TAGS.items():
        if any(kw in desc_lower for kw in keywords):
            tags.append(tag)
    return tags or ["other"]


# ─── Generátor hypotéz (s pamětí) ────────────────────────────────────────────

class MetaResearcher:
    def __init__(self, program_md: Path, causal_log: Path, results_tsv: Path):
        self.program_md = program_md
        self.auditor = MetaAuditor(results_tsv, causal_log)
        self.iteration_count = 0

    def _read_program(self) -> str:
        if self.program_md.exists():
            return self.program_md.read_text()
        return ""

    def _write_program(self, content: str):
        self.program_md.write_text(content)

    # ─── Paměťové dotazy ─────────────────────────────────────────────────────

    def recall_successful_strategies(self, top_n: int = 5) -> list[ExperimentRecord]:
        """Vrátí nejúspěšnější experimenty z kauzální paměti."""
        return self.auditor.query_memory(status="keep", top_n=top_n)

    def recall_failed_strategies(self, tag: Optional[str] = None) -> list[ExperimentRecord]:
        """Vrátí neúspěšné experimenty — chceme se jim vyhnout."""
        return self.auditor.query_memory(status="discard", tag=tag, top_n=10)

    def find_causal_chains(self, commit: str) -> list[ExperimentRecord]:
        """
        Vrátí řetězec předků daného commitu v kauzálním logu.
        Umožňuje meta-researcherovi pochopit, proč aktuální stav funguje.
        """
        chain = []
        current = commit
        seen = set()
        while current and current not in seen:
            seen.add(current)
            match = next((r for r in self.auditor.records if r.commit == current), None)
            if not match:
                break
            chain.append(match)
            current = match.causal_parents[0] if match.causal_parents else None
        return list(reversed(chain))  # Od základu ke špičce

    # ─── Generování hypotéz ───────────────────────────────────────────────────

    def generate_hypothesis(self) -> dict:
        """
        Klíčová funkce: navrhne příští experiment NA ZÁKLADĚ PAMĚTI.
        Vrací: {description, hypothesis, tags, strategy, priority}
        """
        successes = self.recall_successful_strategies(top_n=5)
        failures = self.recall_failed_strategies()

        # Zjistit, které kategorie jsme ještě nezkoušeli
        tried_tags = set(tag for r in self.auditor.records for tag in r.tags)
        untried = [t for t in EXPERIMENT_TAGS.keys() if t not in tried_tags]

        # Strategie výběru
        if untried:
            # Priorita: neprobádané kategorie
            next_tag = untried[0]
            strategy = f"Průzkum nové kategorie: {next_tag}"
            description = f"Experimentovat s {next_tag}"
            hypothesis = (
                f"Kategorie '{next_tag}' nebyla dosud zkoumána. "
                f"Na základě úspěšnosti v jiných kategoriích předpokládáme, "
                f"že optimalizace {next_tag} přinese měřitelné zlepšení val_bpb."
            )
        elif successes:
            # Prohloubit nejúspěšnější kategorii
            best = successes[0]
            best_tag = best.tags[0] if best.tags else "other"
            failed_in_tag = [r for r in failures if best_tag in r.tags]
            strategy = f"Prohloubení kategorie {best_tag} (nejlepší: {best.metric:.6f})"
            description = f"Variace úspěšného přístupu z commitu {best.commit[:7]}"
            hypothesis = (
                f"Nejlepší výsledek byl dosažen v kategorii '{best_tag}' "
                f"(val_bpb={best.metric:.6f}). Bylo vyzkoušeno {len(failed_in_tag)} "
                f"neúspěšných variant. Navrhujeme zkombinovat s jiným úspěšným přístupem."
            )
        else:
            # Žádná data: baseline průzkum
            strategy = "Baseline průzkum — prázdná paměť"
            description = "Základní hyperparametr tuning (LR, batch size)"
            hypothesis = "Kauzální paměť je prázdná. Začínáme průzkumem základních hyperparametrů."

        return {
            "description": description,
            "hypothesis": hypothesis,
            "tags": infer_tags(description),
            "strategy": strategy,
            "priority": "high" if untried else "medium",
            "avoid": [r.description for r in failures[:3]],
            "build_on": successes[0].commit if successes else None,
        }

    # ─── Aktualizace program.md ───────────────────────────────────────────────

    def update_program_md(self, hypothesis: dict, audit_report: Optional[dict] = None):
        """
        Aktualizuje program.md s:
          - Aktuální hypotézou a strategií.
          - Přehledem kauzální paměti (co funguje, co ne).
          - Instrukcemi pro vrstvu 1 (researcher agent).
        """
        program = self._read_program()

        # Sekce, kterou přidáme/nahradíme
        meta_section = f"""
## [META-RESEARCHER] Aktuální instrukce — iterace {self.iteration_count}
Generováno: {time.strftime('%Y-%m-%d %H:%M:%S')}

### Aktuální hypotéza
**Popis experimentu:** {hypothesis['description']}
**Hypotéza:** {hypothesis['hypothesis']}
**Kategorie:** {', '.join(hypothesis['tags'])}
**Strategie:** {hypothesis['strategy']}
**Priorita:** {hypothesis['priority']}

### Co zkusit
- Zaměř se na: {hypothesis['description']}
- Stav na commitu: {hypothesis.get('build_on', 'HEAD')}

### Čeho se vyhnout
{chr(10).join(f'- NEZKOUSEJ: {d}' for d in hypothesis['avoid']) if hypothesis['avoid'] else '- (žádné explicitní zákazy)'}

### Přehled kauzální paměti
{self.auditor.summarize_memory()}

### Audit status
{f"POZOR: {audit_report.get('stop_reason', '')}" if audit_report and audit_report.get('status') == 'STOP' else "Auditor: OK"}
{f"Detekce gamifikace: {audit_report['gaming_detection']['reason']}" if audit_report else ""}

---
"""

        # Nahradit nebo přidat sekci
        marker = "## [META-RESEARCHER]"
        if marker in program:
            # Najít a nahradit existující sekci
            start = program.index(marker)
            end = program.find("\n## ", start + len(marker))
            if end == -1:
                end = len(program)
            program = program[:start] + meta_section + program[end:]
        else:
            # Přidat na konec
            program = program + "\n" + meta_section

        self._write_program(program)
        print(f"[MetaResearcher] program.md aktualizován (iterace {self.iteration_count})")

    # ─── Aktualizace kauzálního logu po experimentu ───────────────────────────

    def annotate_latest_experiment(self, hypothesis: dict):
        """
        Po skončení experimentu (researcher vrstva 1) doplní do posledního záznamu
        hypotézu a tagy — záznamy v kauzálním logu jsou tak kompletní.
        """
        if not self.auditor.records:
            return

        latest = self.auditor.records[-1]
        latest.hypothesis = hypothesis["hypothesis"]
        latest.tags = hypothesis["tags"]

        # Přepsat poslední řádek v JSONL
        causal_log = self.auditor.causal_log
        if causal_log.exists():
            lines = causal_log.read_text().splitlines()
            if lines:
                lines[-1] = latest.to_jsonl()
                causal_log.write_text("\n".join(lines) + "\n")

        print(f"[MetaResearcher] Kauzální záznam {latest.commit[:7]} anotován: tags={latest.tags}")

    # ─── Hlavní smyčka (meta-úroveň) ─────────────────────────────────────────

    def run_meta_iteration(self, run_audit: bool = True) -> bool:
        """
        Jedna iterace meta-researchera:
        1. Spustí audit (vrstva 3) — zkontroluje integritu a paměť.
        2. Přečte kauzální paměť.
        3. Vygeneruje hypotézu.
        4. Aktualizuje program.md.
        5. Vrátí True = pokračovat, False = zastavit.
        """
        self.iteration_count += 1
        print(f"\n[MetaResearcher] === Meta-iterace {self.iteration_count} ===")

        audit_report = None
        if run_audit:
            # Vrstva 3: audit integrity
            audit_report = self.auditor.run_audit_cycle(sample=True)

            # Respektovat circuit breaker
            if audit_report["status"] == "STOP":
                print(f"[MetaResearcher] Auditor zastavil smyčku: {audit_report['stop_reason']}")
                return False

        # Vygenerovat hypotézu na základě paměti
        hypothesis = self.generate_hypothesis()
        print(f"[MetaResearcher] Hypotéza: {hypothesis['hypothesis'][:120]}…")
        print(f"[MetaResearcher] Strategie: {hypothesis['strategy']}")

        # Aktualizovat program.md pro vrstvu 1
        self.update_program_md(hypothesis, audit_report)

        # (Po té, co vrstva 1 dokončí experiment, zavoláme annotate_latest_experiment)
        return True


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meta-researcher v2 pro autoresearch-2.0")
    parser.add_argument("--program", type=Path, default=Path("program.md"))
    parser.add_argument("--causal-log", type=Path, default=Path("causal_memory.jsonl"))
    parser.add_argument("--results", type=Path, default=Path("results.tsv"))
    parser.add_argument("--iterations", type=int, default=1, help="Počet meta-iterací")
    parser.add_argument("--no-audit", action="store_true", help="Přeskočit audit (debug)")
    parser.add_argument("--hypothesis-only", action="store_true", help="Jen vypsat hypotézu a skončit")
    args = parser.parse_args()

    researcher = MetaResearcher(args.program, args.causal_log, args.results)

    if args.hypothesis_only:
        hypothesis = researcher.generate_hypothesis()
        print(json.dumps(hypothesis, ensure_ascii=False, indent=2))
        return

    for i in range(args.iterations):
        should_continue = researcher.run_meta_iteration(run_audit=not args.no_audit)
        if not should_continue:
            print("[MetaResearcher] Smyčka zastavena audítorem.")
            sys.exit(1)
        if i < args.iterations - 1:
            time.sleep(2)  # Krátká pauza mezi meta-iteracemi

    print("[MetaResearcher] Meta-iterace dokončeny.")


if __name__ == "__main__":
    main()
