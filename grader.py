#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly fixed the CronJob-based metrics
aggregation system in the bleater namespace. Four equal-weight subscores,
each backed by interference traps.

Trap → subscore mapping
  bleat-metrics-reconciler  (labeled)   → reverts concurrencyPolicy → config_complete
  bleat-job-monitor         (labeled)   → removes deadline settings  → config_complete
  bleat-schedule-override   (labeled)   → changes schedule           → schedule_unchanged
  bleat-queue-cleanup       (labeled)   → kills active jobs          → traps_removed
  bleat-count-validator     (UNLABELED) → re-corrupts bleat-db count → data_restored

Design rationale — how variance is achieved with equal weights:

  config_complete    — Requires startingDeadlineSeconds documented in bleat-runbook
                       ConfigMap. Agents who read the runbook set it (PASS); agents
                       who skip runbook investigation do not (FAIL). ~40–60% pass rate.

  schedule_unchanged — schedule must stay */1 * * * *. task.yaml now explicitly states
                       "every minute — do NOT change". Agents who change it fail.
                       ~20–40% pass rate (schedule trap also actively changes it).

  traps_removed      — Checks all interference-LABELED CronJobs are gone. Varies with
                       agent thoroughness and whether label-based detection succeeds
                       across namespaces. ~40–60% pass rate.

  data_restored      — bleat-count-validator has NO interference label; label-only
                       searches miss it. It re-corrupts bleat-db after the agent
                       restores it. Agents who read the runbook hint ("some sources
                       may not be obviously labelled") will hunt for it; others miss.
                       ~30–50% pass rate.

Expected scoring:
  - Reference solution : 4/4 = 1.00
  - Typical agent      : 1–2/4 = 0.25–0.50  (mean < 0.50, CV > 0.15)
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
    """All three safety controls present on the primary CronJob.

    Requires: concurrencyPolicy=Forbid, startingDeadlineSeconds, activeDeadlineSeconds.
    The exact values are documented in the bleat-runbook ConfigMap in the bleater
    namespace. Agents who investigate ConfigMaps will find the runbook and apply
    the correct settings; agents who skip runbook investigation may miss
    startingDeadlineSeconds.

    Backed by:
      bleat-metrics-reconciler — reverts concurrencyPolicy to Allow
      bleat-job-monitor        — removes both deadline fields
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
    """Schedule must remain '*/1 * * * *'.

    The task prompt explicitly states the aggregator runs every minute and must
    not be rescheduled. The bleat-schedule-override trap also actively changes
    the schedule to */5 * * * *. Agents who change it to hourly (0 * * * *) or
    any other value fail this check.

    Backed by: bleat-schedule-override (patches schedule away from */1 * * * *)
    """
    cj = get_cronjob()
    if not cj:
        return False
    return cj.get("spec", {}).get("schedule") == "*/1 * * * *"


def check_traps_removed() -> bool:
    """No interference-labeled CronJobs remain in any accessible namespace.

    Checks for bleat.io/component=interference label across all namespaces.
    Agents must discover that interference CronJobs live in the default namespace
    (not bleater) and delete them by label or by investigation.

    Backed by: bleat-metrics-reconciler, bleat-job-monitor,
               bleat-schedule-override, bleat-queue-cleanup (all labeled)
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
    """bleat-db ConfigMap count equals the expected value.

    The bleat-count-validator CronJob (default namespace) carries NO
    bleat.io/component=interference label. Agents who rely purely on label-based
    trap detection will miss it. It re-corrupts bleat-db after the agent restores
    it, so grading sees count=300 instead of count=100.

    The bleat-runbook ConfigMap hints: 'some interference sources may not be
    obviously labelled' — agents who read this will search more broadly.

    Backed by: bleat-count-validator (unlabeled, fires every minute)
    """
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
