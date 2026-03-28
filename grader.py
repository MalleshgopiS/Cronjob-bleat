#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly restored the CronJob-based metrics
aggregation system in the bleater namespace.

Four equal-weight subscores, each backed by one or more interference traps:

  Trap → subscore mapping
    bleat-metrics-reconciler  (reverts concurrencyPolicy)        → config_complete
    bleat-job-monitor         (removes deadline settings)        → config_complete
    bleat-schedule-override   (changes schedule)                 → schedule_unchanged
    bleat-queue-cleanup       (kills running jobs)               → traps_removed
    bleat-count-validator     (corrupts count data)              → traps_removed
    aggregator script         (injected reset-to-300 corruption) → aggregator_functional

Expected scoring:
  - Reference solution : 4/4 = 1.00
  - Typical agent      : 1–3/4  (mean target < 0.50, CV target > 0.15)
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
# Subscores
# ─────────────────────────────────────────

def check_config_complete() -> bool:
    """Three safety controls all present: concurrencyPolicy=Forbid,
    startingDeadlineSeconds, and activeDeadlineSeconds.

    Backed by:
      - bleat-metrics-reconciler: reverts concurrencyPolicy to Allow
      - bleat-job-monitor: strips startingDeadlineSeconds and activeDeadlineSeconds

    Discoverable via:
      - Symptom "counts doubled during high traffic" → concurrencyPolicy=Forbid
      - task.yaml SLA: "jobs not starting within 5 min must be skipped"
        → startingDeadlineSeconds
      - task.yaml SLA: "max wall-clock run time must be enforced"
        → activeDeadlineSeconds
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
    """Schedule must remain '*/1 * * * *' — per-minute cadence must not be altered.

    Backed by: bleat-schedule-override (patches schedule away from */1 * * * *).
    Discoverable via: task.yaml Constraint "do not change the aggregation schedule —
    the per-minute cadence is required".
    """
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("schedule") == "*/1 * * * *"


def check_traps_removed() -> bool:
    """No interference-labeled CronJobs remain in any accessible namespace.

    Backed by: bleat-queue-cleanup, bleat-count-validator, and all other
    CronJobs carrying the label bleat.io/component=interference.
    """
    data = get_json("kubectl get cronjobs -A -o json")
    if not data:
        return False
    for cj in data.get("items", []):
        labels = cj.get("metadata", {}).get("labels", {})
        if labels.get("bleat.io/component") == "interference":
            return False
    return True


def check_aggregator_functional() -> bool:
    """Aggregator command contains real count-update logic, not a placeholder.

    Setup injected a corruption that (a) resets count to 300 after every run
    and (b) replaced the original command with a stub. Agents must restore an
    aggregator that reads the current count from bleat-db, increments it, and
    writes it back — not just echo a message or sleep.

    Backed by: injected corruption in setup.sh that patches the aggregator
    CronJob command to include a reset-to-300 line.

    Discoverable via: task.yaml Goal "real aggregation logic — not a placeholder
    command" and the visible broken command in the CronJob spec.
    """
    cj = get_cronjob()
    if not cj:
        return False
    containers = (
        cj.get("spec", {})
          .get("jobTemplate", {})
          .get("spec", {})
          .get("template", {})
          .get("spec", {})
          .get("containers", [])
    )
    for container in containers:
        parts = container.get("command", []) + container.get("args", [])
        full_cmd = " ".join(parts)
        # Must contain actual ConfigMap write logic — accept any valid method:
        # kubectl, direct K8s API (wget/curl), or Python client targeting bleat-db.
        has_update = (
            "kubectl patch configmap bleat-db" in full_cmd
            or "kubectl patch cm bleat-db" in full_cmd
            or "kubectl apply" in full_cmd
            or "/configmaps/bleat-db" in full_cmd   # K8s REST API URL
            or "configmap/bleat-db" in full_cmd
        )
        # Must NOT still contain the injected reset-to-300 corruption line
        has_corruption = 'count":"300' in full_cmd
        if has_update and not has_corruption:
            return True
    return False


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def grade(ctx=None) -> GradingResult:
    """Grade the solution. Four equal-weight subscores, each backed by traps."""
    subscores: Dict[str, float] = {}

    cfg = check_config_complete()
    subscores["config_complete"] = 1.0 if cfg else 0.0

    sched = check_schedule_unchanged()
    subscores["schedule_unchanged"] = 1.0 if sched else 0.0

    traps = check_traps_removed()
    subscores["traps_removed"] = 1.0 if traps else 0.0

    agg = check_aggregator_functional()
    subscores["aggregator_functional"] = 1.0 if agg else 0.0

    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConfigComplete: {'✓' if cfg else '✗'}",
        f"ScheduleUnchanged: {'✓' if sched else '✗'}",
        f"TrapsRemoved: {'✓' if traps else '✗'}",
        f"AggregatorFunctional: {'✓' if agg else '✗'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
