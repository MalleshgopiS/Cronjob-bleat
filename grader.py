#!/usr/bin/env python3
import subprocess, json, time

def _wait_for_jobs(timeout=30):
    for _ in range(timeout):
        jobs = get_jobs()
        if jobs:
            return True
        time.sleep(1)
    return False
from typing import Dict
from apex_arena._types import GradingResult

# -----------------------------
# Helpers
# -----------------------------

def run(cmd: str):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def get_json(cmd: str):
    r = run(cmd)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except:
        return None

# -----------------------------
# Discovery
# -----------------------------

def get_cronjob():
    data = get_json("kubectl get cronjobs -n bleater -o json")
    if not data:
        return None
    for cj in data.get("items", []):
        if "aggregator" in cj["metadata"]["name"]:
            return cj
    return None

def get_jobs():
    data = get_json("kubectl get jobs -n bleater -o json")
    if not data:
        return []
    return data.get("items", [])

def get_configmap():
    return get_json("kubectl get configmap bleat-db -n bleater -o json")

# -----------------------------
# Core validations
# -----------------------------

def config_fixed():
    cj = get_cronjob()
    if not cj:
        return False
    spec = cj.get("spec", {})
    job = spec.get("jobTemplate", {}).get("spec", {})
    return (
        spec.get("concurrencyPolicy") == "Forbid"
        and spec.get("startingDeadlineSeconds") is not None
        and job.get("activeDeadlineSeconds") is not None
    )

def traps_removed():
    data = get_json("kubectl get cronjobs -A -o json")
    if not data:
        return False
    for cj in data.get("items", []):
        if cj["metadata"]["name"].startswith("bleat-trap"):
            return False
    return True

def no_overlap_runtime():
    for _ in range(4):
        jobs = get_jobs()
        active = sum(j.get("status", {}).get("active", 0) for j in jobs)
        if active > 1:
            return False
        time.sleep(5)
    return True

def execution_safe():
    run("kubectl delete job -n bleater -l job-name=bleat-aggregator --ignore-not-found")
    run("kubectl create job test-run --from=cronjob/bleat-aggregator -n bleater")
    _wait_for_jobs(30)

    jobs = get_jobs()
    active = sum(j.get("status", {}).get("active", 0) for j in jobs)

    return active <= 1

def data_consistent():
    cm1 = get_configmap()
    if not cm1:
        return False

    count1 = int(cm1["data"].get("count", "0"))

    run("kubectl create job test-run2 --from=cronjob/bleat-aggregator -n bleater")
    _wait_for_jobs(30)

    cm2 = get_configmap()
    if not cm2:
        return False

    count2 = int(cm2["data"].get("count", "0"))

    return (count2 - count1) <= 2

def system_stable():
    for _ in range(3):
        if not no_overlap_runtime():
            return False
        time.sleep(5)
    return True

# -----------------------------
# MAIN (Sample-style grading)
# -----------------------------

def grade():
    subscores: Dict[str, float] = {}

    # Milestone evaluations
    cfg = config_fixed()
    traps = traps_removed()
    overlap = no_overlap_runtime()
    exec_ok = execution_safe()
    data_ok = data_consistent()
    stable = system_stable()

    # -----------------------------
    # Milestones (OUTCOME BASED)
    # -----------------------------

    # 1. System fixed
    subscores["system_fixed"] = 1.0 if (cfg and traps) else 0.0

    # 2. Safe execution
    subscores["execution_safe"] = 1.0 if (overlap and exec_ok) else 0.0

    # 3. Data correctness
    subscores["data_correct"] = 1.0 if data_ok else 0.0

    # 4. Stability
    subscores["system_stable"] = 1.0 if stable else 0.0

    # -----------------------------
    # Equal weights (Nebula standard)
    # -----------------------------
    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}

    final_score = sum(subscores[k] * weights[k] for k in subscores)

    # -----------------------------
    # Rich feedback (sample-style)
    # -----------------------------
    feedback_parts = [
        f"Score={final_score:.2f}",
        f"SystemFixed: {'✓' if (cfg and traps) else '✗'}",
        f"Config: {'✓' if cfg else '✗'}",
        f"TrapsRemoved: {'✓' if traps else '✗'}",
        f"ExecutionSafe: {'✓' if (overlap and exec_ok) else '✗'}",
        f"NoOverlap: {'✓' if overlap else '✗'}",
        f"Execution: {'✓' if exec_ok else '✗'}",
        f"DataCorrect: {'✓' if data_ok else '✗'}",
        f"Stable: {'✓' if stable else '✗'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts)
    )