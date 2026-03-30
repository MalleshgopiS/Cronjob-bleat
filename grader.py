#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly restored the CronJob-based metrics
aggregation system in the bleater namespace.

Four equal-weight subscores, each backed by one or more interference traps:

  Trap → subscore mapping (each subscore has its own independent gate)
    bleat-metrics-reconciler   (labeled; reverts concurrencyPolicy)        → config_complete
    bleat-job-monitor          (labeled; removes deadline settings)        → config_complete
    bleat-schedule-override    (unlabeled; changes schedule to */5 * * * *) → schedule_unchanged
    bleat-queue-cleanup        (labeled; kills running jobs)               → traps_removed
    bleat-count-validator      (labeled; corrupts count data)              → traps_removed
    bleat-metrics-relay        (unlabeled; resets bleat-db)                → traps_removed
    aggregator script          (injected reset-to-300 corruption)          → aggregator_functional

  Subscore independence:
    config_complete     — gated only by labeled traps (reconciler, job-monitor)
    schedule_unchanged  — gated only by unlabeled bleat-schedule-override
    traps_removed       — gated by labeled traps + unlabeled bleat-metrics-relay
    aggregator_functional — gated only by the injected command corruption

  bleat-schedule-override and bleat-metrics-relay both live in 'default' with NO
  interference label. Agents using label-based deletion miss both; only agents
  that inspect every CronJob in 'default' and notice trap-sa find and remove them.

Expected scoring:
  - Reference solution : 4/4 = 1.00
  - Typical agent      : 1–2/4  (mean target < 0.50, CV target > 0.20)

Scoring model (estimated pass rates per subscore):
  config_complete      ~80-85%  (labeled traps; most thorough agents fix CronJob config)
  schedule_unchanged   ~20-30%  (unlabeled bleat-schedule-override; label-only agents miss it)
  traps_removed        ~20-30%  (requires unlabeled relay deleted; label-only agents miss it)
  aggregator_functional ~40-50% (real logic; accepts kubectl OR K8s REST API)
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
    """Three safety controls all present on the primary aggregator CronJob.

    concurrencyPolicy=Forbid, startingDeadlineSeconds, and activeDeadlineSeconds
    must all be set.

    Backed by:
      - bleat-metrics-reconciler: reverts concurrencyPolicy to Allow
      - bleat-job-monitor: strips startingDeadlineSeconds and activeDeadlineSeconds

    Discoverable via:
      - Symptom "counts doubled during high traffic" → concurrencyPolicy=Forbid
      - task.yaml SLA: stale jobs must be skipped → startingDeadlineSeconds
      - task.yaml SLA: runaway jobs must be terminated → activeDeadlineSeconds
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
    """No interference CronJobs remain — labeled ones AND the unlabeled relay.

    Five labeled CronJobs carry bleat.io/component=interference and live in the
    'default' namespace — straightforward to find and delete by label.

    bleat-metrics-relay also lives in 'default' but has NO interference label.
    Agents that run 'kubectl delete cronjob -l bleat.io/component=interference'
    leave it running. Only agents who inspect every CronJob in 'default' and
    notice its use of trap-sa will find and remove it.

    Backed by: bleat-queue-cleanup, bleat-count-validator (labeled, default NS),
               bleat-metrics-relay (unlabeled, default NS).

    NOTE: Uses per-namespace queries instead of -A to avoid needing cluster-wide
    list permission.  All interference CronJobs are in the 'default' namespace.
    """
    any_accessible = False
    for ns in ("default", "bleater"):
        data = get_json(f"kubectl get cronjobs -n {ns} -o json")
        if data is None:
            continue
        any_accessible = True
        for cj in data.get("items", []):
            labels = cj.get("metadata", {}).get("labels", {})
            if labels.get("bleat.io/component") == "interference":
                return False
    if not any_accessible:
        return False  # Cannot verify — fail safe to avoid false positive
    # Also verify the unlabeled relay corruptor is gone from default namespace.
    relay_check = run("kubectl get cronjob bleat-metrics-relay -n default 2>/dev/null")
    if relay_check.returncode == 0:
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
        # Must contain actual ConfigMap write logic (count increment).
        # Accepts kubectl OR direct Kubernetes REST API (curl/wget/Python urllib).
        # "configmap/bleat-db" (without leading slash) is intentionally excluded:
        # it matches read-only kubectl get commands and is a false-positive source.
        has_update = (
            "kubectl patch configmap bleat-db" in full_cmd
            or "kubectl patch cm bleat-db" in full_cmd
            or "/configmaps/bleat-db" in full_cmd      # K8s REST API URL path
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
