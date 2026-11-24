#!/usr/bin/env python3
"""Build the product's forecasting-model registry (models/v1) from the frozen C.0 test-season result. Standard library.

The numbers are copied from the research repository's result file, never typed: the file's SHA-256 and the commit that
last changed it are recorded next to them. Section 9 of the protocol decides what the product uses: the one model that
is BETTER than naive under section 8, or naive ("the baseline is enough") when none is. Anything the protocol does not
settle stops this script instead of being guessed: more than one BETTER model at a variety and horizon (no pairwise
winner is claimed), a verdict other than BETTER or NO_DETECTABLE_DIFFERENCE, or an untested horizon with no treatment.

    scripts/make_model_registry.py            write backend/src/kisaniti_api/models/registry_c0.json
    scripts/make_model_registry.py --check    exit 1 if the committed file differs from a fresh build
Needs the research repository next to this one (../kisaniti_research_paper) or --research PATH.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "backend" / "src" / "kisaniti_api" / "models" / "registry_c0.json"
RESULT = "results/C0_protocol/test_season_inference.csv"
RUN = "results/C0_protocol/test_run.json"
PROTOCOL_COMMIT = "864005d1919f162cd7c9a6ca7c31a3d524387988"  # C.0 freeze, 27 Oct 2026, owner's approval
TREATMENT = {28: "SECONDARY", 60: "DESCRIPTIVE"}  # PREREGISTERED_PROTOCOL.md section 4: no tests at these horizons
BASELINE = "naive"


class RegistryError(Exception):
    pass


def build(research: Path) -> dict[str, object]:
    csv_path, run_path = research / RESULT, research / RUN
    run = json.loads(run_path.read_text())
    if run.get("stage") != "test":
        raise RegistryError(f"{RUN} is stage {run.get('stage')!r}, not the test stage")
    rows = list(csv.DictReader(csv_path.open(newline="")))
    commit = subprocess.run(["git", "-C", str(research), "log", "-1", "--format=%H", "--", RESULT], capture_output=True, text=True, check=True).stdout.strip()
    if len(commit) != 40:
        raise RegistryError(f"{RESULT} is not committed in the research repository")
    order = [m for m in run["models"] if m != BASELINE]
    groups: dict[tuple[str, int], list[dict[str, str]]] = {}
    for r in rows:
        if r["verdict"] not in ("BETTER", "NO_DETECTABLE_DIFFERENCE"):
            raise RegistryError(f"verdict {r['verdict']!r} for {r['variety']} h={r['horizon']} {r['model']}: not settled by the protocol")
        groups.setdefault((r["variety"], int(r["horizon"])), []).append(r)
    entries = []
    for (variety, h), rs in sorted(groups.items()):
        if sorted(r["model"] for r in rs) != sorted(order):
            raise RegistryError(f"{variety} h={h}: models {sorted(r['model'] for r in rs)} differ from the run's {sorted(order)}")
        rs.sort(key=lambda r: order.index(r["model"]))
        better = [r["model"] for r in rs if r["verdict"] == "BETTER"]
        if len(better) > 1:
            raise RegistryError(f"{variety} h={h}: {better} all beat naive; the protocol names no winner between them")
        entries.append(
            {
                "variety": variety,
                "horizon_days": h,
                "in_use": better[0] if better else BASELINE,
                "in_use_reason": "PROMOTED" if better else "BASELINE_ENOUGH",
                "models": [
                    {
                        "model": r["model"],
                        "status": "PROMOTED" if r["verdict"] == "BETTER" else "NOT_PROMOTED",
                        "verdict": r["verdict"],
                        "pairs": int(r["pairs"]),
                        "relative_change": float(r["relative_change"]),
                        "p_holm": float(r["p_holm"]),
                        "ci_low": float(r["ci_low"]),
                        "ci_high": float(r["ci_high"]),
                        "smallest_gain": float(r["smallest_gain"]),
                    }
                    for r in rs
                ],
            }
        )
    tested = {h for _, h in groups}
    untested = []
    for h in sorted(set(run["horizons"]) - tested):
        if h not in TREATMENT:
            raise RegistryError(f"horizon {h} was run but not tested, and the protocol gives it no treatment here")
        untested.append({"horizon_days": h, "treatment": TREATMENT[h]})
    return {
        "contract": "models.registry/v1",
        "evidence": {
            "protocol_commit": PROTOCOL_COMMIT,
            "results_commit": commit,
            "season": run["season"],
            "run_at": run["run_at"],
            "file": "rp:" + RESULT,
            "file_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        },
        "rule": "C0_S8_HOLM_CI_MIN_GAIN",
        "entries": entries,
        "untested_horizons": untested,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--research", type=Path, default=ROOT.parent / "kisaniti_research_paper")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    try:
        text = json.dumps(build(a.research), indent=2) + "\n"
    except RegistryError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    if a.check:
        same = OUT.exists() and OUT.read_text() == text
        print("registry matches the research result" if same else f"{OUT} differs from a fresh build")
        return 0 if same else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
