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
echo "🧹 Removing trap CronJobs..."

ALLOWED_NS=$(cat /home/ubuntu/.allowed_namespaces 2>/dev/null | tr ',' ' ')
# Always include default; deduplicate
ALL_NS=$(echo "$ALLOWED_NS default" | tr ' ' '\n' | sort -u | tr '\n' ' ')

(
  for CHECK_NS in $ALL_NS; do
    kubectl get cronjobs -n "$CHECK_NS" -o json 2>/dev/null | jq -r '
    .items[]
    | select(.metadata.name | startswith("bleat-trap"))
    | "\(.metadata.namespace) \(.metadata.name)"'
  done
) | sort -u | while read trap_ns trap_name; do
  echo "Deleting trap: $trap_name in $trap_ns"
  kubectl delete cronjob "$trap_name" -n "$trap_ns" --ignore-not-found
done

# -----------------------------
# FIX CRONJOB CONFIGURATION
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

echo "✅ System successfully restored"
