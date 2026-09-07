"""CVSS validation and scoring adapted from Strix's reporting tools."""

from __future__ import annotations

from typing import Any

from cvss import CVSS3

CVSS_VALID = {
    "attack_vector": ["N", "A", "L", "P"],
    "attack_complexity": ["L", "H"],
    "privileges_required": ["N", "L", "H"],
    "user_interaction": ["N", "R"],
    "scope": ["U", "C"],
    "confidentiality": ["N", "L", "H"],
    "integrity": ["N", "L", "H"],
    "availability": ["N", "L", "H"],
}


def validate_cvss_breakdown(breakdown: Any) -> list[str]:
    """Require all eight canonical metrics and the reference's legal values."""
    if not isinstance(breakdown, dict) or not breakdown:
        return ["cvss_breakdown: must be an object with the 8 CVSS metrics"]
    return [
        f"Invalid {name}: {breakdown.get(name)}. Must be one of: {valid}"
        for name, valid in CVSS_VALID.items()
        if breakdown.get(name) not in valid
    ]


def calculate_cvss(breakdown: dict[str, str]) -> tuple[float, str, str]:
    """Calculate a validated vector's score, severity, and canonical vector."""
    vector = (
        f"CVSS:3.1/AV:{breakdown['attack_vector']}/AC:{breakdown['attack_complexity']}/"
        f"PR:{breakdown['privileges_required']}/UI:{breakdown['user_interaction']}/"
        f"S:{breakdown['scope']}/C:{breakdown['confidentiality']}/"
        f"I:{breakdown['integrity']}/A:{breakdown['availability']}"
    )
    try:
        cvss = CVSS3(vector)
        score = cvss.scores()[0]
        base_severity = cvss.severities()[0].lower()
    except Exception as exc:
        raise ValueError(f"Failed to calculate CVSS for validated vector: {vector}") from exc
    return score, "info" if base_severity == "none" else base_severity, vector
