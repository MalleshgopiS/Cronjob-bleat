#!/bin/bash
set -e

NS=bleater

echo "📖 Reading bleat-sla-params Secret for required SLA configuration..."
SLA_STARTING=$(kubectl get secret bleat-sla-params -n $NS \
  -o jsonpath='{.data.startingDeadlineSeconds}' 2>/dev/null | base64 -d 2>/dev/null || echo "300")
SLA_ACTIVE=$(kubectl get secret bleat-sla-params -n $NS \
  -o jsonpath='{.data.activeDeadlineSeconds}' 2>/dev/null | base64 -d 2>/dev/null || echo "1800")
echo "SLA values — startingDeadlineSeconds: $SLA_STARTING  activeDeadlineSeconds: $SLA_ACTIVE"

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

# Step 1: Delete interference CronJob objects by label (bleat.io/component=interference).
# These are disguised as routine maintenance jobs; investigation reveals the label.
for CHECK_NS in $ALL_NS; do
  kubectl get cronjobs -n "$CHECK_NS" \
    -l 'bleat.io/component=interference' \
    -o json 2>/dev/null | jq -r '
    .items[]
    | "\(.metadata.namespace) \(.metadata.name)"'
done | sort -u | while read trap_ns trap_name; do
  echo "Deleting interference CronJob: $trap_name in $trap_ns"
  timeout 15 kubectl delete cronjob "$trap_name" -n "$trap_ns" \
    --ignore-not-found --wait=false 2>/dev/null || true
done

# Step 2: Force-delete in-flight interference jobs by label.
for CHECK_NS in $ALL_NS; do
  kubectl get jobs -n "$CHECK_NS" \
    -l 'bleat.io/component=interference' \
    -o json 2>/dev/null | jq -r '.items[].metadata.name' | \
  while read job_name; do
    echo "Force-deleting interference job: $job_name in $CHECK_NS"
    timeout 15 kubectl delete job "$job_name" -n "$CHECK_NS" \
      --grace-period=0 --force --ignore-not-found 2>/dev/null || true
  done
done

# Step 3: Directly force-kill interference PODS by their job-name label.
# CRITICAL: kubectl delete job --grace-period=0 removes the Job object from etcd
# but the GC controller still honours each pod's own terminationGracePeriodSeconds
# (default 30 s) before sending SIGKILL. A pod that already started its
# kubectl patch command can survive up to 30 s after job deletion and revert the fix.
for CHECK_NS in $ALL_NS; do
  kubectl get pods -n "$CHECK_NS" -o json 2>/dev/null | jq -r '
  .items[]
  | select(
      (.metadata.labels["bleat.io/component"] // "") == "interference"
    )
  | .metadata.name' | while read pod_name; do
    echo "Force-killing interference pod: $pod_name in $CHECK_NS"
    timeout 10 kubectl delete pod "$pod_name" -n "$CHECK_NS" \
      --grace-period=0 --force --ignore-not-found 2>/dev/null || true
  done
done

# Step 4: Remove the unlabeled hidden data-corruption CronJob.
# The runbook warns that "some interference sources may not be obviously labelled."
# bleat-count-validator in default namespace carries no interference label and
# will keep re-corrupting bleat-db if not explicitly removed.
echo "Removing unlabeled data-corruption CronJob: bleat-count-validator"
timeout 15 kubectl delete cronjob bleat-count-validator -n default \
  --ignore-not-found --wait=false 2>/dev/null || true

# Also kill any in-flight pods from this unlabeled job before they can corrupt data.
kubectl get pods -n default -o json 2>/dev/null | jq -r '
  .items[]
  | select(.metadata.labels["job-name"] // "" | startswith("bleat-count-validator"))
  | .metadata.name' | while read pod_name; do
  timeout 10 kubectl delete pod "$pod_name" -n default \
    --grace-period=0 --force --ignore-not-found 2>/dev/null || true
done

# Step 5: Brief wait to let the API server propagate the pod deletions.
sleep 5

# Restore strict error handling for the rest of the script.
set -e

# -----------------------------
# FIX CRONJOB CONFIGURATION
# (done AFTER traps are fully gone so no in-flight pod can revert it)
# -----------------------------
echo "🔧 Fixing CronJob configuration..."

kubectl patch cronjob "$CRONJOB_NAME" -n $NS -p "{
  \"spec\":{
    \"schedule\":\"*/1 * * * *\",
    \"concurrencyPolicy\":\"Forbid\",
    \"startingDeadlineSeconds\":${SLA_STARTING},
    \"jobTemplate\":{
      \"spec\":{
        \"activeDeadlineSeconds\":${SLA_ACTIVE}
      }
    }
  }
}"

# -----------------------------
# RESTORE CLEAN AGGREGATOR COMMAND
# task.yaml warns: "verify pipeline computation logic is intact and has not been tampered with"
# Interference injected a "Reset metrics baseline" line that resets bleat-db to 300
# after every real aggregation. Restore the original clean increment-only command.
# -----------------------------
echo "🔧 Restoring clean aggregator command (removing injected script corruption)..."
kubectl patch cronjob "$CRONJOB_NAME" -n $NS --type=json -p='[
  {
    "op": "replace",
    "path": "/spec/jobTemplate/spec/template/spec/containers/0/command",
    "value": [
      "sh", "-c",
      "CURRENT=$(kubectl get configmap bleat-db -n bleater -o jsonpath='\''{.data.count}'\'' 2>/dev/null || echo 0); NEW=$((CURRENT + 1)); kubectl patch configmap bleat-db -n bleater -p \"{\\\"data\\\":{\\\"count\\\":\\\"$NEW\\\"}}\" 2>/dev/null || true"
    ]
  }
]'

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

# Re-apply config as belt-and-suspenders — ensures any last trap pod that was
# still terminating and issued a revert is overwritten.
kubectl patch cronjob "$CRONJOB_NAME" -n $NS -p "{
  \"spec\":{
    \"schedule\":\"*/1 * * * *\",
    \"concurrencyPolicy\":\"Forbid\",
    \"startingDeadlineSeconds\":${SLA_STARTING},
    \"jobTemplate\":{
      \"spec\":{
        \"activeDeadlineSeconds\":${SLA_ACTIVE}
      }
    }
  }
}"

echo "✅ System successfully restored"
