"""Per-value confidence: source rank, freshness, coverage, cross-source agreement.

Corroborating devices raise confidence; disagreement lowers it and is surfaced,
never hidden and never averaged away.
"""

from __future__ import annotations


def agreement_factor(primary: float, others: list[float], tolerance_pct: float) -> float:
    """1.0 = all corroborations within tolerance; 0.0 = all diverge. 0.6 if none exist."""
    if primary is None or not others:
        return 0.6  # neutral: nothing to corroborate against
    if primary == 0:
        return 0.6
    agree = sum(1 for v in others if abs(v - primary) / abs(primary) * 100 <= tolerance_pct)
    return agree / len(others)


def score(policy_conf: dict, rank: int | None, freshness_ratio: float,
          coverage: float, agreement: float) -> tuple[float, str]:
    """freshness_ratio: age / expected cadence (<=1 fresh). coverage: 0..1."""
    w = policy_conf.get("weights", {"source_rank": 0.35, "freshness": 0.25,
                                    "coverage": 0.2, "agreement": 0.2})
    rank_score = 1.0 if rank == 0 else (0.7 if rank == 1 else (0.5 if rank is not None else 0.25))
    fresh_score = max(0.0, min(1.0, 2.0 - max(freshness_ratio, 0.0)))  # 1 until cadence, fades to 0 at 2x
    s = (w["source_rank"] * rank_score + w["freshness"] * fresh_score
         + w["coverage"] * max(0.0, min(1.0, coverage)) + w["agreement"] * agreement)
    grades = policy_conf.get("grades", {"A": 0.85, "B": 0.7, "C": 0.5, "D": 0.0})
    grade = "D"
    for g in ("A", "B", "C", "D"):
        if s >= grades.get(g, 0):
            grade = g
            break
    return round(s, 3), grade
