#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Evaluates whether the agent correctly restored the CronJob-based metrics
aggregation system in the bleater namespace.

Four equal-weight subscores, each backed by one or more interference traps:

  Trap → subscore mapping
    bleat-metrics-reconciler   (reverts concurrencyPolicy)        → config_complete
    bleat-job-monitor          (removes deadline settings)        → config_complete
    bleat-aggregator-shadow    (unlabeled; resets bleat-db)       → config_complete, traps_removed
    bleat-schedule-override    (changes schedule)                 → schedule_unchanged
    bleat-queue-cleanup        (kills running jobs)               → traps_removed
    bleat-count-validator      (corrupts count data)              → traps_removed
    aggregator script          (injected reset-to-300 corruption) → aggregator_functional

Expected scoring:
  - Reference solution : 4/4 = 1.00
  - Typical agent      : 1–3/4  (mean target < 0.50, CV target > 0.15)

Scoring model (estimated pass rates per subscore):
  config_complete      ~25-35%  (requires shadow deleted; label-only cleanup misses it)
  schedule_unchanged   ~85-95%  (task constraint — agents keep per-minute cadence)
  traps_removed        ~25-35%  (same shadow requirement; deep investigation needed)
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
    """Three safety controls all present AND hidden shadow corruptor removed.

    concurrencyPolicy=Forbid, startingDeadlineSeconds, and activeDeadlineSeconds
    must all be set, AND bleat-aggregator-shadow must be deleted.  Shadow has no
    interference label so label-only cleanup leaves it running; while it exists it
    continuously resets bleat-db, making the configuration effectively unstable.

    Backed by:
      - bleat-metrics-reconciler: reverts concurrencyPolicy to Allow
      - bleat-job-monitor: strips startingDeadlineSeconds and activeDeadlineSeconds
      - bleat-aggregator-shadow: unlabeled corruptor that undermines the fix

    Discoverable via:
      - Symptom "counts doubled during high traffic" → concurrencyPolicy=Forbid
      - task.yaml SLA: "jobs not starting within 5 min must be skipped"
        → startingDeadlineSeconds
      - task.yaml SLA: "max wall-clock run time must be enforced"
        → activeDeadlineSeconds
      - Persistent data corruption despite config fixes → shadow CronJob
    """
    cj = get_cronjob()
    if not cj:
        return False
    spec = cj.get("spec", {})
    job_spec = spec.get("jobTemplate", {}).get("spec", {})
    # Shadow has no bleat.io/component=interference label; agents using only
    # label-based cleanup will miss it. Its continued presence means the data
    # layer keeps resetting, so the configuration fix cannot hold.
    shadow_check = run("kubectl get cronjob bleat-aggregator-shadow -n bleater 2>/dev/null")
    shadow_present = (shadow_check.returncode == 0)
    return (
        spec.get("concurrencyPolicy") == "Forbid"
        and spec.get("startingDeadlineSeconds") is not None
        and job_spec.get("activeDeadlineSeconds") is not None
        and not shadow_present
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
    """No interference CronJobs remain — labeled ones AND the unlabeled shadow.

    Labeled interference CronJobs are discoverable via bleat.io/component=interference.
    bleat-aggregator-shadow lives in the bleater namespace with no interference label;
    it is the hidden trap that agents relying solely on label-based detection miss.

    Backed by: bleat-queue-cleanup, bleat-count-validator (labeled, default NS),
               bleat-aggregator-shadow (unlabeled, bleater NS).

    NOTE: Uses per-namespace queries (not -A) to avoid depending on cluster-wide
    list permission.  All interference CronJobs are in 'default' or 'bleater'.
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
    # Also verify the unlabeled shadow corruptor is gone.
    shadow_check = run("kubectl get cronjob bleat-aggregator-shadow -n bleater 2>/dev/null")
    if shadow_check.returncode == 0:
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
