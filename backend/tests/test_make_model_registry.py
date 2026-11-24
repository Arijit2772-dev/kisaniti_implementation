"""scripts/make_model_registry.py: copies the frozen result, and refuses what the protocol does not settle."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("make_model_registry", ROOT / "scripts" / "make_model_registry.py")
assert SPEC and SPEC.loader
mr = importlib.util.module_from_spec(SPEC)
sys.modules["make_model_registry"] = mr
SPEC.loader.exec_module(mr)

FIELDS = [
    "variety",
    "horizon",
    "model",
    "pairs",
    "relative_change",
    "dm_statistic",
    "p_raw",
    "p_holm",
    "ci_low",
    "ci_high",
    "smallest_gain",
    "verdict",
]
MODELS = ["seasonal_naive", "ets"]


def research(tmp: Path, verdicts: dict[tuple[str, int, str], str], horizons: list[int] | None = None) -> Path:
    """A fictional research repo with one committed result file."""
    d = tmp / "rp"
    (d / "results" / "C0_protocol").mkdir(parents=True)
    run = {
        "stage": "test",
        "season": "2025-26",
        "run_at": "2026-10-27T18:18:24+00:00",
        "models": ["naive", *MODELS],
        "horizons": horizons or [1, 7],
    }
    (d / mr.RUN).write_text(json.dumps(run))
    with (d / mr.RESULT).open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        for (v, h, m), verdict in verdicts.items():
            w.writerow(
                {
                    "variety": v,
                    "horizon": h,
                    "model": m,
                    "pairs": 60,
                    "relative_change": -0.2,
                    "dm_statistic": 0,
                    "p_raw": 0.01,
                    "p_holm": 0.02,
                    "ci_low": -0.3,
                    "ci_high": -0.1,
                    "smallest_gain": 0.133,
                    "verdict": verdict,
                }
            )
    git = ["git", "-C", str(d), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "fictional result"], check=True)
    return d


def all_ndd(h: list[int]) -> dict[tuple[str, int, str], str]:
    return {("X", hh, m): "NO_DETECTABLE_DIFFERENCE" for hh in h for m in MODELS}


def test_no_better_model_keeps_naive(tmp_path: Path) -> None:
    doc = mr.build(research(tmp_path, all_ndd([1, 7])))
    assert [(e["horizon_days"], e["in_use"], e["in_use_reason"]) for e in doc["entries"]] == [
        (1, "naive", "BASELINE_ENOUGH"),
        (7, "naive", "BASELINE_ENOUGH"),
    ]
    assert len(doc["evidence"]["results_commit"]) == 40


def test_one_better_model_is_promoted(tmp_path: Path) -> None:
    v = all_ndd([1, 7]) | {("X", 7, "ets"): "BETTER"}
    e = {x["horizon_days"]: x for x in mr.build(research(tmp_path, v))["entries"]}
    assert (e[7]["in_use"], e[7]["in_use_reason"]) == ("ets", "PROMOTED") and e[1]["in_use"] == "naive"
    assert [m["status"] for m in e[7]["models"]] == ["NOT_PROMOTED", "PROMOTED"]


def test_two_better_models_are_refused(tmp_path: Path) -> None:
    v = all_ndd([1, 7]) | {("X", 7, "ets"): "BETTER", ("X", 7, "seasonal_naive"): "BETTER"}
    with pytest.raises(mr.RegistryError, match="names no winner"):
        mr.build(research(tmp_path, v))


def test_unsettled_verdict_is_refused(tmp_path: Path) -> None:
    with pytest.raises(mr.RegistryError, match="not settled"):
        mr.build(research(tmp_path, all_ndd([1, 7]) | {("X", 1, "ets"): "NO_THRESHOLD"}))


def test_untested_horizon_without_treatment_is_refused(tmp_path: Path) -> None:
    with pytest.raises(mr.RegistryError, match="no treatment"):
        mr.build(research(tmp_path, all_ndd([1, 7]), horizons=[1, 7, 90]))
    doc = mr.build(research(tmp_path / "b", all_ndd([1, 7]), horizons=[1, 7, 28]))
    assert doc["untested_horizons"] == [{"horizon_days": 28, "treatment": "SECONDARY"}]
