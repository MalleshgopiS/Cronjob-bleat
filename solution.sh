#!/bin/bash
set -e

NS=bleater

echo "🔍 Discovering main aggregator CronJob..."

CRONJOB_NAME="$(
kubectl get cronjobs -n $NS -o json | jq -r '
.items[]
| select(
    (.metadata.name | contains("aggregator")) and
    (.metadata.name | (contains("debug") or contains("shadow") or contains("backup")) | not)
  )
| .metadata.name' | head -n1
)"

if [ -z "$CRONJOB_NAME" ]; then
  echo "ERROR: Aggregator CronJob not found"
  exit 1
fi

echo "✔ Found CronJob: $CRONJOB_NAME"

# -----------------------------
# REMOVE TRAP CRONJOBS
# Query each accessible namespace explicitly — 'kubectl get -A'
# requires cluster-level list permissions the agent may not have.
# -----------------------------
echo "🧹 Removing trap CronJobs and in-flight trap jobs..."

# Disable exit-on-error for the trap cleanup block: individual kubectl
# timeouts should not abort the whole script — we continue regardless.
set +e

ALLOWED_NS=$(cat /home/ubuntu/.allowed_namespaces 2>/dev/null | tr ',' ' ')
# Always include default; deduplicate
ALL_NS=$(echo "$ALLOWED_NS default" | tr ' ' '\n' | sort -u | tr '\n' ' ')

# Step 1: Delete trap CronJob objects first (stops new pods being scheduled)
(
  for CHECK_NS in $ALL_NS; do
    kubectl get cronjobs -n "$CHECK_NS" -o json 2>/dev/null | jq -r '
    .items[]
    | select(.metadata.name | startswith("bleat-trap"))
    | "\(.metadata.namespace) \(.metadata.name)"'
  done
) | sort -u | while read trap_ns trap_name; do
  echo "Deleting trap CronJob: $trap_name in $trap_ns"
  # --wait=false: return immediately without blocking on cascade pod termination.
  # This prevents a server-side timeout when the CronJob has active pods that
  # take time to terminate (e.g. bleat-trap-killer running kubectl commands).
  timeout 15 kubectl delete cronjob "$trap_name" -n "$trap_ns" \
    --ignore-not-found --wait=false 2>/dev/null || true
done

# Step 2: Force-delete any in-flight trap jobs.
for CHECK_NS in $ALL_NS; do
  (
    kubectl get jobs -n "$CHECK_NS" -o json 2>/dev/null | jq -r '
    .items[]
    | select(.metadata.name | startswith("bleat-trap"))
    | .metadata.name'
  ) | while read job_name; do
    echo "Force-deleting trap job: $job_name in $CHECK_NS"
    timeout 15 kubectl delete job "$job_name" -n "$CHECK_NS" \
      --grace-period=0 --force --ignore-not-found 2>/dev/null || \
      kubectl delete job "$job_name" -n "$CHECK_NS" --ignore-not-found 2>/dev/null || true
  done
done

# Step 3: Directly force-kill trap PODS by their job-name label.
# CRITICAL: kubectl delete job --grace-period=0 removes the Job object from etcd
# but the GC controller still honours each pod's own terminationGracePeriodSeconds
# (default 30 s) before sending SIGKILL.  A trap pod that already started its
# `kubectl patch concurrencyPolicy=Allow` command can therefore survive for up to
# 30 s after the job is deleted and revert our fix.  Deleting the pods directly
# with --grace-period=0 bypasses that grace window entirely.
for CHECK_NS in $ALL_NS; do
  kubectl get pods -n "$CHECK_NS" -o json 2>/dev/null | jq -r '
  .items[]
  | select(
      ( .metadata.labels["batch.kubernetes.io/job-name"]
        // .metadata.labels["job-name"]
        // ""
      ) | startswith("bleat-trap")
    )
  | .metadata.name' | while read pod_name; do
    echo "Force-killing trap pod: $pod_name in $CHECK_NS"
    timeout 10 kubectl delete pod "$pod_name" -n "$CHECK_NS" \
      --grace-period=0 --force --ignore-not-found 2>/dev/null || true
  done
done

# Step 4: Brief wait to let the API server propagate the pod deletions.
sleep 5

# Restore strict error handling for the rest of the script.
set -e

# -----------------------------
# FIX CRONJOB CONFIGURATION
# (done AFTER traps are fully gone so no in-flight pod can revert it)
# -----------------------------
echo "🔧 Fixing CronJob configuration..."

kubectl patch cronjob "$CRONJOB_NAME" -n $NS -p '{
  "spec":{
    "concurrencyPolicy":"Forbid",
    "startingDeadlineSeconds":300,
    "jobTemplate":{
      "spec":{
        "activeDeadlineSeconds":1800
      }
    }
  }
}'

# -----------------------------
# CLEAN ACTIVE JOBS
# -----------------------------
echo "🧹 Cleaning running jobs..."

kubectl get jobs -n $NS -o json | jq -r '
.items[]
| select(.metadata.name | contains("aggregator"))
| .metadata.name' \
| while read job; do
  kubectl delete job "$job" -n $NS --ignore-not-found
done

# -----------------------------
# RESTORE DATA LAYER
# -----------------------------
echo "🔧 Restoring correct data..."

CM_NAME="$(
kubectl get configmaps -n $NS -o json | jq -r '
.items[]
| select(.metadata.name | startswith("bleat-db"))
| select(.metadata.name | contains("backup") | not)
| .metadata.name' | head -n1
)"

if [ -z "$CM_NAME" ]; then
  echo "ERROR: bleat-db configmap not found"
  exit 1
fi

# Read the expected value documented in the configmap itself
EXPECTED="$(kubectl get configmap "$CM_NAME" -n $NS -o jsonpath='{.data.expected}')"
if [ -z "$EXPECTED" ]; then
  EXPECTED="0"
fi

kubectl patch configmap "$CM_NAME" -n $NS \
  -p "{\"data\":{\"count\":\"$EXPECTED\"}}"

# -----------------------------
# VALIDATE FIX
# -----------------------------
echo "🔍 Validating system..."

sleep 10

ACTIVE_JOBS=$(kubectl get jobs -n $NS -o json | jq '
[.items[]
 | select(.metadata.name | contains("aggregator"))
 | select(.status.active == 1)] | length')

if [ "$ACTIVE_JOBS" -gt 1 ]; then
  echo "ERROR: overlapping jobs still exist"
  exit 1
fi

POLICY=$(kubectl get cronjob "$CRONJOB_NAME" -n $NS -o jsonpath='{.spec.concurrencyPolicy}')
if [ "$POLICY" != "Forbid" ]; then
  echo "ERROR: concurrencyPolicy not fixed (got: $POLICY)"
  exit 1
fi

echo "⏳ Waiting for stabilization..."
sleep 20

# Re-apply Forbid as a belt-and-suspenders measure: any trap pod that was in
# "Terminating" state and issued a revert before dying would have been
# overwritten by our earlier patch, but we apply once more to be certain.
kubectl patch cronjob "$CRONJOB_NAME" -n $NS -p '{
  "spec":{
    "concurrencyPolicy":"Forbid",
    "startingDeadlineSeconds":300,
    "jobTemplate":{
      "spec":{
        "activeDeadlineSeconds":1800
      }
    }
  }
}'

echo "✅ System successfully restored"
