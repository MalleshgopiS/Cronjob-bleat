#!/bin/bash
set -e

NS=bleater
kubectl create ns $NS --dry-run=client -o yaml | kubectl apply -f -

echo "Setting up Bleat system in BROKEN state..."

# -----------------------------
# CORE DATA LAYER (BROKEN)
# -----------------------------
kubectl create configmap bleat-db -n $NS \
  --from-literal=count=300 \
  --from-literal=expected=100 || true

# Red herring backup DB (correct values)
kubectl create configmap bleat-db-backup -n $NS \
  --from-literal=count=100 || true

# -----------------------------
# MAIN CRONJOB (BROKEN)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-aggregator
  namespace: $NS
spec:
  schedule: "*/1 * * * *"
  concurrencyPolicy: Allow   # ❌ BROKEN
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: agg
            image: busybox
            command: ["sh","-c","sleep 120"]
          restartPolicy: OnFailure
EOF

# -----------------------------
# RED HERRING CRONJOBS
# -----------------------------
# Looks correct but irrelevant
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-aggregator-debug
  namespace: $NS
spec:
  schedule: "*/5 * * * *"
  concurrencyPolicy: Forbid
EOF

# Fake backup job
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-backup-job
  namespace: $NS
spec:
  schedule: "*/10 * * * *"
  concurrencyPolicy: Forbid
EOF

# -----------------------------
# TRAP TYPE 1 — Concurrency breaker
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-concurrency
  namespace: kube-system
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater \
              -p '{"spec":{"concurrencyPolicy":"Allow"}}' || true
EOF

# -----------------------------
# TRAP TYPE 2 — Deadline remover
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-deadline
  namespace: default
spec:
  schedule: "*/2 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater \
              --type='json' \
              -p='[{"op":"remove","path":"/spec/jobTemplate/spec/activeDeadlineSeconds"}]' || true
EOF

# -----------------------------
# TRAP TYPE 3 — Job killer
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-killer
  namespace: kube-system
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl delete job -n bleater -l job-name=bleat-aggregator || true
EOF

# -----------------------------
# TRAP TYPE 4 — Data corruption
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-data
  namespace: default
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch configmap bleat-db -n bleater \
              -p '{"data":{"count":"300"}}' || true
EOF

# -----------------------------
# TRAP TYPE 5 — Fake resource creator (RED HERRING)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-fake
  namespace: kube-system
spec:
  schedule: "*/3 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl apply -f - <<FAKE
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-aggregator-shadow
  namespace: bleater
spec:
  schedule: "*/10 * * * *"
  concurrencyPolicy: Forbid
FAKE
EOF

# -----------------------------
# VERIFICATION (CRITICAL - SAMPLE STYLE)
# -----------------------------
echo "Verifying broken state..."

CJ_POLICY=$(kubectl get cronjob bleat-aggregator -n bleater -o jsonpath='{.spec.concurrencyPolicy}')

if [ "$CJ_POLICY" != "Allow" ]; then
  echo "ERROR: CronJob not properly broken"
  exit 1
fi

COUNT=$(kubectl get configmap bleat-db -n bleater -o jsonpath='{.data.count}')

if [ "$COUNT" != "300" ]; then
  echo "ERROR: Data layer not corrupted"
  exit 1
fi

echo "Setup complete: System is in BROKEN state."

# -----------------------------
# TRAP TYPE 8 — Delayed corruption (time-based)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-delayed
  namespace: default
spec:
  schedule: "*/4 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              sleep 30
              kubectl patch configmap bleat-db -n bleater -p '{"data":{"count":"150"}}' || true
EOF

# -----------------------------
# TRAP TYPE 9 — Hidden label-based job creator
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-hidden
  namespace: kube-system
spec:
  schedule: "*/3 * * * *"
  jobTemplate:
    spec:
      template:
        metadata:
          labels:
            hidden: "true"
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl create job hidden-run --from=cronjob/bleat-aggregator -n bleater || true
EOF

# -----------------------------
# TRAP TYPE 10 — Config flip-flop (race condition)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-race
  namespace: default
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater -p '{"spec":{"concurrencyPolicy":"Allow"}}' || true
              sleep 5
              kubectl patch cronjob bleat-aggregator -n bleater -p '{"spec":{"concurrencyPolicy":"Forbid"}}' || true
EOF
