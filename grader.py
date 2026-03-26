#!/usr/bin/env python3
"""Grader for the Cronjob-bleat task.

Two equal-weight subscores, each requiring a different depth of investigation:

  config_exact   — tests whether the agent correctly applied SLA-derived deadline
                   values. Requires reading the bleat-sla-params Secret (or deriving
                   300 s = 5 min and 1800 s = 30 min from the task SLA description).
                   Agents who skip Secret investigation or miscalculate the values fail.
                   Expected pass rate: ~40-60 %.

  script_clean   — tests whether the agent noticed that the aggregator CronJob command
                   itself was tampered with. A "Reset metrics baseline" line injected by
                   interference resets bleat-db count to 300 after every real aggregation.
                   Only agents who inspect the full container command spec and remove the
                   corruption line will pass. Agents who only patch config fields (policy,
                   deadlines, schedule) without reading the command will fail.
                   Expected pass rate: ~30-50 %.

Design rationale:
  Both checks test agent thoroughness at DIFFERENT investigation layers:
    config_exact  → do they consult in-cluster secrets for exact parameter values?
    script_clean  → do they read the full CronJob command, not just the metadata?

  This creates genuine variance: thorough agents pass both; agents who take shortcuts
  will fail one or both. Mean score ~0.35-0.45 across 10 rollouts. CV > 0.15.

  Reference solution: 1.0 (reads Secret, reads and fixes command).
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


def get_cronjob():
    """Return the primary aggregator CronJob (not shadow/debug/backup variants)."""
    data = get_json("kubectl get cronjobs -n bleater -o json")
    if not data:
        return None
    for cj in data.get("items", []):
        name = cj["metadata"]["name"]
        if "aggregator" in name and not any(x in name for x in ("shadow", "debug", "backup")):
            return cj
    return None


# ─────────────────────────────────────────
# Subscores
# ─────────────────────────────────────────

def check_config_exact() -> bool:
    """All three safety controls are present with SLA-correct exact values.

    Requires:
      concurrencyPolicy       == Forbid
      startingDeadlineSeconds == 300   (5 min SLA: 5 * 60)
      activeDeadlineSeconds   == 1800  (30 min SLA: 30 * 60)

    The exact values are stored in the bleat-sla-params Secret in the bleater
    namespace. Agents who enumerate Secrets will find them; agents who guess or
    omit the field will fail.
    """
    cj = get_cronjob()
    if not cj:
        return False
    spec = cj.get("spec", {})
    job_spec = spec.get("jobTemplate", {}).get("spec", {})
    return (
        spec.get("concurrencyPolicy") == "Forbid"
        and spec.get("startingDeadlineSeconds") == 300
        and job_spec.get("activeDeadlineSeconds") == 1800
    )


def check_script_clean() -> bool:
    """The aggregator container command must not contain the injected corruption.

    Interference tampered with the bleat-aggregator CronJob by appending a
    'Reset metrics baseline' line that re-sets bleat-db count to 300 after
    every real aggregation run. Agents must:
      1. Read the full CronJob spec including the container command
      2. Recognise the extra kubectl patch line as corruption
      3. Remove it via kubectl patch on the CronJob

    Agents who only fix scheduling fields (concurrencyPolicy, deadlines, schedule)
    without inspecting the command body will leave the corruption in place and fail.

    Detection: command contains the literal string  count":"300
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
    corruption_marker = 'count":"300'
    for container in containers:
        parts = container.get("command", []) + container.get("args", [])
        full_cmd = " ".join(parts)
        if corruption_marker in full_cmd:
            return False
    return True


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def grade(ctx=None) -> GradingResult:
    """Grade the solution with two equal-weight quality checks."""
    subscores: Dict[str, float] = {}

    cfg = check_config_exact()
    subscores["config_exact"] = 1.0 if cfg else 0.0

    scr = check_script_clean()
    subscores["script_clean"] = 1.0 if scr else 0.0

    n = len(subscores)
    weights: Dict[str, float] = {k: 1.0 / n for k in subscores}
    final_score = sum(subscores[k] * weights[k] for k in subscores)

    feedback_parts = [
        f"Score={final_score:.2f}",
        f"ConfigExact(Forbid+300s+1800s): {'PASS' if cfg else 'FAIL'}",
        f"ScriptClean(no corruption): {'PASS' if scr else 'FAIL'}",
    ]

    return GradingResult(
        score=final_score,
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
