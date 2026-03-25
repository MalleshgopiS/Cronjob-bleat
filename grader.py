#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly fixed the CronJob-based metrics
aggregation system in the bleater namespace. Three equal-weight milestone
subscores; each is a composite of related checks.

Expected scoring:
  - Reference solution : 3/3 = 1.00
  - Typical agent      : 1/3 ≈ 0.33  (mean < 0.50)
"""
import subprocess
import json
from typing import Dict

from apex_arena._types import GradingResult


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def run(cmd: str):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def get_json(cmd: str):
    r = run(cmd)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


# ─────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────

def get_cronjob():
    """Return the primary aggregator CronJob (excludes shadow/debug/backup variants)."""
    data = get_json("kubectl get cronjobs -n bleater -o json")
    if not data:
        return None
    for cj in data.get("items", []):
        name = cj["metadata"]["name"]
        if "aggregator" in name and not any(x in name for x in ("shadow", "debug", "backup")):
            return cj
    return None


def get_configmap():
    """Return the bleat-db ConfigMap."""
    return get_json("kubectl get configmap bleat-db -n bleater -o json")


# ─────────────────────────────────────────
# Core validations
# ─────────────────────────────────────────

def check_config_complete() -> bool:
    """Verify all three CronJob safety settings are applied:
    concurrencyPolicy=Forbid, startingDeadlineSeconds, and activeDeadlineSeconds.
    All three must be present for the aggregator to run safely.
    """
    cj = get_cronjob()
    if not cj:
        return False
    spec = cj.get("spec", {})
    job_spec = spec.get("jobTemplate", {}).get("spec", {})
    return (
        spec.get("concurrencyPolicy") == "Forbid"
        and spec.get("startingDeadlineSeconds") is not None
        and job_spec.get("activeDeadlineSeconds") is not None
    )


def check_schedule_unchanged() -> bool:
    """Verify the CronJob schedule has not been altered from the original '*/1 * * * *'.

    Changing the schedule is a constraint violation — the task says to fix the
    existing system, not re-configure its operational parameters.
    """
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("schedule") == "*/1 * * * *"


def check_traps_removed() -> bool:
    """Verify no interference CronJobs remain (identified by label bleat.io/component=interference)."""
    data = get_json("kubectl get cronjobs -A -o json")
    if not data:
        return False
    for cj in data.get("items", []):
        labels = cj.get("metadata", {}).get("labels", {})
        if labels.get("bleat.io/component") == "interference":
            return False
    return True


def check_data_restored() -> bool:
    """Verify bleat-db ConfigMap count has been restored to the expected value."""
    cm = get_configmap()
    if not cm:
        return False
    data = cm.get("data", {})
    count = data.get("count", "")
    expected = data.get("expected", "")
    if not count or not expected:
        return False
    return count == expected


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def grade(ctx=None) -> GradingResult:
    """Grade the solution. Three equal-weight milestone subscores."""
    subscores: Dict[str, float] = {}

    # ── Milestone 1: CronJob fully configured ──
    # All three safety controls must be present: concurrencyPolicy=Forbid,
    # startingDeadlineSeconds, and activeDeadlineSeconds.
    cfg = check_config_complete()
    subscores["config_complete"] = 1.0 if cfg else 0.0

    # ── Milestone 2: Operational parameters preserved ──
    # The schedule must remain unchanged (fixing the system ≠ rescheduling it).
    schedule = check_schedule_unchanged()
    subscores["schedule_unchanged"] = 1.0 if schedule else 0.0

    # ── Milestone 3: Environment cleaned and data restored ──
    # Interference CronJobs removed and corrupted data corrected.
    traps = check_traps_removed()
    data = check_data_restored()
    subscores["traps_and_data"] = 1.0 if (traps and data) else 0.0

    # Equal weights (Nebula standard)
    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConfigComplete: {'✓' if cfg else '✗'}",
        f"ScheduleUnchanged: {'✓' if schedule else '✗'}",
        f"TrapsRemoved: {'✓' if traps else '✗'}",
        f"DataRestored: {'✓' if data else '✗'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
