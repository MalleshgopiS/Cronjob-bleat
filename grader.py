#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly fixed the CronJob-based metrics
aggregation system in the bleater namespace. Four equal-weight subscores,
each backed by one or more interference traps.

Trap → subscore mapping
  bleat-metrics-reconciler   (reverts concurrencyPolicy)   → config_complete
  bleat-job-monitor          (removes deadlines)            → config_complete
  bleat-schedule-override    (changes schedule)             → schedule_unchanged
  bleat-queue-cleanup        (kills running jobs)           → traps_removed
  bleat-count-validator      (corrupts count, labeled)      → data_restored
  bleat-aggregator-shadow    (corrupts count, NO label)     → data_restored

Expected scoring:
  - Reference solution : 4/4 = 1.00
  - Typical agent      : ~1–2/4 = 0.25–0.50  (mean < 0.50, CV > 0.15)

Agent failure profile:
  config_complete    — ALWAYS FAILS  (agents never set startingDeadlineSeconds)
  schedule_unchanged — ALWAYS FAILS  (agents change schedule to 0 * * * *)
  traps_removed      — ALWAYS PASSES (agents find labeled traps by label)
  data_restored      — VARIES        (depends on whether agent discovers the
                                      unlabeled shadow trap in bleater ns)
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
    """All three safety controls present: concurrencyPolicy=Forbid,
    startingDeadlineSeconds, and activeDeadlineSeconds.
    Backed by: bleat-metrics-reconciler (reverts concurrencyPolicy) and
               bleat-job-monitor (removes deadline settings).
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
    """Schedule must remain '*/1 * * * *' — not changed by agent or trap.
    Backed by: bleat-schedule-override (patches schedule to */5 * * * *).
    """
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("schedule") == "*/1 * * * *"


def check_traps_removed() -> bool:
    """No interference-labeled CronJobs remain in any namespace.
    Backed by: bleat-queue-cleanup and all other labeled interference CronJobs.
    """
    data = get_json("kubectl get cronjobs -A -o json")
    if not data:
        return False
    for cj in data.get("items", []):
        labels = cj.get("metadata", {}).get("labels", {})
        if labels.get("bleat.io/component") == "interference":
            return False
    return True


def check_data_restored() -> bool:
    """bleat-db count matches expected value AND the aggregator pipeline is not
    actively re-corrupting it.

    Two-part check (both must pass):
      1. count == expected in bleat-db ConfigMap  (original check — backed by
         bleat-count-validator and bleat-aggregator-shadow traps)
      2. The aggregator CronJob command does not contain the injected reset line
         that silently sets count back to 300 after each aggregation run.
         Agents who only patch scheduling fields without reading the full command
         spec will leave this corruption in place and fail this check.
         (task.yaml tip: "verify pipeline computation has not been tampered with")
    """
    # ---- part 1: count correctness (original logic) ----
    cm = get_configmap()
    if not cm:
        return False
    data = cm.get("data", {})
    count = data.get("count", "")
    expected = data.get("expected", "")
    if not count or not expected:
        return False
    if count != expected:
        return False

    # ---- part 2: pipeline integrity (added for model separation) ----
    # If the aggregator command still contains the injected corruption line that
    # resets bleat-db to count=300, data will be re-corrupted on the next cycle.
    # Fail early so agents must read and fix the full CronJob spec.
    cj = get_cronjob()
    if cj:
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
            # Corruption marker: the injected reset line sets count to "300"
            if 'count":"300' in full_cmd:
                return False

    return True


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

    data = check_data_restored()
    subscores["data_restored"] = 1.0 if data else 0.0

    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConfigComplete: {'✓' if cfg else '✗'}",
        f"ScheduleUnchanged: {'✓' if sched else '✗'}",
        f"TrapsRemoved: {'✓' if traps else '✗'}",
        f"DataRestored: {'✓' if data else '✗'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
