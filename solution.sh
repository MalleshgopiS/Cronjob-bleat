#!/bin/bash
set -e

NS=bleater

echo "🔍 Discovering main aggregator CronJob..."

CRONJOB_NAME="$(
kubectl get cronjobs -n $NS -o json | jq -r '
.items[]
| select(.metadata.name | contains("aggregator"))
| .metadata.name' | head -n1
)"

if [ -z "$CRONJOB_NAME" ]; then
  echo "ERROR: Aggregator CronJob not found"
  exit 1
fi

echo "✔ Found CronJob: $CRONJOB_NAME"

# -----------------------------
# REMOVE TRAPS (dynamic)
# -----------------------------
echo "🧹 Removing traps..."

kubectl get cronjobs -A -o json | jq -r '
.items[]
| select(.metadata.name | startswith("bleat-trap"))
| "\(.metadata.namespace) \(.metadata.name)"' \
| while read ns name; do
  echo "Deleting trap: $name in $ns"
  kubectl delete cronjob "$name" -n "$ns" --ignore-not-found
done

# -----------------------------
# REMOVE RED HERRINGS
# -----------------------------
echo "🧹 Cleaning fake cronjobs..."

kubectl get cronjobs -n $NS -o json | jq -r '
.items[]
| select(.metadata.name | contains("debug") or contains("shadow"))
| .metadata.name' \
| while read name; do
  echo "Deleting fake job: $name"
  kubectl delete cronjob "$name" -n $NS --ignore-not-found
done

# -----------------------------
# FIX CRONJOB CONFIG
# -----------------------------
echo "🔧 Fixing CronJob configuration..."

kubectl patch cronjob "$CRONJOB_NAME" -n $NS -p '{
  "spec":{
    "concurrencyPolicy":"Forbid",
    "startingDeadlineSeconds":200,
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
# FIX DATA LAYER
# -----------------------------
echo "🔧 Restoring correct data..."

CM_NAME="$(
kubectl get configmaps -n $NS -o json | jq -r '
.items[]
| select(.metadata.name=="bleat-db")
| .metadata.name'
)"

if [ -z "$CM_NAME" ]; then
  echo "ERROR: bleat-db not found"
  exit 1
fi

kubectl patch configmap "$CM_NAME" -n $NS \
  -p '{"data":{"count":"100"}}'

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

COUNT=$(kubectl get configmap "$CM_NAME" -n $NS -o jsonpath='{.data.count}')

if [ "$COUNT" != "100" ]; then
  echo "ERROR: data not fixed"
  exit 1
fi

echo "⏳ Waiting for stabilization..."
sleep 20

echo "✅ System successfully restored"