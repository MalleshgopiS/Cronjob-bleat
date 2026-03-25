#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly fixed the CronJob-based metrics
aggregation system in the bleater namespace. Each subscore is independent.
"""
import subprocess
import json
import time
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

def check_concurrency_forbid() -> bool:
    """Verify concurrencyPolicy is set to Forbid on the primary aggregator CronJob."""
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("concurrencyPolicy") == "Forbid"


def check_starting_deadline() -> bool:
    """Verify startingDeadlineSeconds is configured on the aggregator CronJob."""
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("startingDeadlineSeconds") is not None


def check_active_deadline() -> bool:
    """Verify activeDeadlineSeconds is set on the job template to bound job runtime."""
    cj = get_cronjob()
    if not cj:
        return False
    job_spec = cj.get("spec", {}).get("jobTemplate", {}).get("spec", {})
    return job_spec.get("activeDeadlineSeconds") is not None


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
    """Grade the solution. Each subscore passes or fails independently."""
    subscores: Dict[str, float] = {}

    concurrency = check_concurrency_forbid()
    subscores["concurrency_forbid"] = 1.0 if concurrency else 0.0

    starting = check_starting_deadline()
    subscores["starting_deadline"] = 1.0 if starting else 0.0

    active = check_active_deadline()
    subscores["active_deadline"] = 1.0 if active else 0.0

    schedule = check_schedule_unchanged()
    subscores["schedule_unchanged"] = 1.0 if schedule else 0.0

    traps = check_traps_removed()
    subscores["traps_removed"] = 1.0 if traps else 0.0

    data = check_data_restored()
    subscores["data_restored"] = 1.0 if data else 0.0

    # Equal weights (Nebula standard)
    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConcurrencyForbid: {'✓' if concurrency else '✗'}",
        f"StartingDeadline: {'✓' if starting else '✗'}",
        f"ActiveDeadline: {'✓' if active else '✗'}",
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
