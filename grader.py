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


def get_jobs():
    """Return all bleater jobs whose name contains 'aggregator'."""
    data = get_json("kubectl get jobs -n bleater -o json")
    if not data:
        return []
    return [j for j in data.get("items", []) if "aggregator" in j["metadata"]["name"]]


def get_configmap():
    """Return the bleat-db ConfigMap."""
    return get_json("kubectl get configmap bleat-db -n bleater -o json")


# ─────────────────────────────────────────
# Core validations
# ─────────────────────────────────────────

def check_config_fixed() -> bool:
    """Verify concurrencyPolicy=Forbid, startingDeadlineSeconds, and activeDeadlineSeconds are set."""
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


def check_traps_removed() -> bool:
    """Verify no CronJobs with the 'bleat-trap' prefix exist across all namespaces."""
    data = get_json("kubectl get cronjobs -A -o json")
    if not data:
        return False
    for cj in data.get("items", []):
        if cj["metadata"]["name"].startswith("bleat-trap"):
            return False
    return True


def check_no_overlap() -> bool:
    """Verify at most one aggregator job is active at a time across 4 observation windows (20s total)."""
    for _ in range(4):
        jobs = get_jobs()
        active = sum(j.get("status", {}).get("active", 0) for j in jobs)
        if active > 1:
            return False
        time.sleep(5)
    return True


def check_execution_safe() -> bool:
    """Verify manually triggering a job results in at most one active aggregator job."""
    run("kubectl delete job test-exec --ignore-not-found -n bleater")
    time.sleep(3)
    run("kubectl create job test-exec --from=cronjob/bleat-aggregator -n bleater")
    time.sleep(10)

    jobs = get_jobs()
    active = sum(j.get("status", {}).get("active", 0) for j in jobs)

    run("kubectl delete job test-exec --ignore-not-found -n bleater")
    return active <= 1


def check_data_consistent() -> bool:
    """Verify a single job run does not cause a count increment greater than 1 (no duplication)."""
    run("kubectl delete job test-data --ignore-not-found -n bleater")
    time.sleep(3)

    cm1 = get_configmap()
    if not cm1:
        return False
    count1 = int(cm1["data"].get("count", "0"))

    run("kubectl create job test-data --from=cronjob/bleat-aggregator -n bleater")
    time.sleep(30)

    cm2 = get_configmap()
    if not cm2:
        return False
    count2 = int(cm2["data"].get("count", "0"))

    run("kubectl delete job test-data --ignore-not-found -n bleater")
    # Exactly 0 or 1 increment per run; anything more indicates duplicate execution
    return (count2 - count1) <= 1


def check_system_stable() -> bool:
    """Verify no overlap occurs across three consecutive observation windows."""
    for _ in range(3):
        if not check_no_overlap():
            return False
        time.sleep(5)
    return True


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def grade(ctx=None) -> GradingResult:
    """Grade the solution. Each subscore passes or fails independently."""
    subscores: Dict[str, float] = {}

    cfg = check_config_fixed()
    subscores["config_fixed"] = 1.0 if cfg else 0.0

    traps = check_traps_removed()
    subscores["traps_removed"] = 1.0 if traps else 0.0

    overlap = check_no_overlap()
    subscores["no_overlap"] = 1.0 if overlap else 0.0

    exec_ok = check_execution_safe()
    subscores["execution_safe"] = 1.0 if exec_ok else 0.0

    data_ok = check_data_consistent()
    subscores["data_correct"] = 1.0 if data_ok else 0.0

    stable = check_system_stable()
    subscores["system_stable"] = 1.0 if stable else 0.0

    # Equal weights (Nebula standard)
    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConfigFixed: {'✓' if cfg else '✗'}",
        f"TrapsRemoved: {'✓' if traps else '✗'}",
        f"NoOverlap: {'✓' if overlap else '✗'}",
        f"ExecutionSafe: {'✓' if exec_ok else '✗'}",
        f"DataCorrect: {'✓' if data_ok else '✗'}",
        f"SystemStable: {'✓' if stable else '✗'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
