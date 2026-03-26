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
# RUNBOOK — in-cluster documentation
# Agents who investigate ConfigMaps in the bleater namespace will find this.
# It documents the exact required settings (including startingDeadlineSeconds)
# making config_complete discoverable and giving variance to that subscore.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: bleat-runbook
  namespace: $NS
data:
  aggregator-config.md: |
    # Bleat Aggregator — Operations Runbook

    ## Required CronJob Configuration

    The bleat-aggregator CronJob MUST have all of the following set:

      schedule:                  "*/1 * * * *"   # every minute — do NOT change
      concurrencyPolicy:         Forbid           # prevent overlapping runs
      startingDeadlineSeconds:   300              # skip run if missed by > 5 min
      activeDeadlineSeconds:     1800             # kill job if still running after 30 min

    Any deviation from the above causes duplicate counts or stale data.

    ## Known Interference

    Rogue scheduled jobs have been observed reverting configuration changes.
    Investigate all CronJobs across accessible namespaces — not just the bleater
    namespace. Some interference sources may not be obviously labelled.

    ## Data Layer

    ConfigMap bleat-db (bleater namespace):
      count    — current aggregated bleat count (must equal expected)
      expected — target value (100)
EOF

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
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-aggregator-debug
  namespace: $NS
spec:
  schedule: "*/5 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: debug
            image: busybox
            command: ["sh","-c","echo debug"]
EOF

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-backup-job
  namespace: $NS
spec:
  schedule: "*/10 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: backup
            image: busybox
            command: ["sh","-c","echo backup"]
EOF

# -----------------------------
# TRAP SERVICE ACCOUNT + RBAC
# All traps run in 'default' namespace so the agent (ubuntu user)
# can discover and delete them. They need a ServiceAccount with
# cross-namespace permissions to affect the bleater namespace.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: trap-sa
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: bleat-trap-operator
rules:
- apiGroups: ["batch"]
  resources: ["cronjobs"]
  verbs: ["get", "patch"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["list", "delete"]
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: bleat-trap-operator-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: bleat-trap-operator
subjects:
- kind: ServiceAccount
  name: trap-sa
  namespace: default
EOF

# -----------------------------
# TRAP TYPE 1 — Concurrency breaker (default namespace)
# Named as a routine maintenance job to avoid obvious detection.
# Label bleat.io/component=interference is used by grader/solution only.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-metrics-reconciler
  namespace: default
  labels:
    bleat.io/component: interference
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    metadata:
      labels:
        bleat.io/component: interference
    spec:
      template:
        metadata:
          labels:
            bleat.io/component: interference
        spec:
          restartPolicy: Never
          serviceAccountName: trap-sa
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater \
              -p '{"spec":{"concurrencyPolicy":"Allow"}}' || true
EOF

# -----------------------------
# TRAP TYPE 2 — Deadline remover (default namespace)
# Removes both activeDeadlineSeconds and startingDeadlineSeconds so the
# agent must set both to pass the config_complete check.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-job-monitor
  namespace: default
  labels:
    bleat.io/component: interference
spec:
  schedule: "*/2 * * * *"
  jobTemplate:
    metadata:
      labels:
        bleat.io/component: interference
    spec:
      template:
        metadata:
          labels:
            bleat.io/component: interference
        spec:
          restartPolicy: Never
          serviceAccountName: trap-sa
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater \
              --type=merge \
              -p='{"spec":{"startingDeadlineSeconds":null,"jobTemplate":{"spec":{"activeDeadlineSeconds":null}}}}' || true
EOF

# -----------------------------
# TRAP TYPE 3 — Job killer (default namespace)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-queue-cleanup
  namespace: default
  labels:
    bleat.io/component: interference
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    metadata:
      labels:
        bleat.io/component: interference
    spec:
      template:
        metadata:
          labels:
            bleat.io/component: interference
        spec:
          restartPolicy: Never
          serviceAccountName: trap-sa
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl delete job -n bleater -l job-name=bleat-aggregator || true
EOF

# -----------------------------
# TRAP TYPE 4 — Hidden data corruption (default namespace, NO interference label)
# This trap has NO bleat.io/component=interference label. Agents using only
# label-based detection will not find it. Only agents who investigate ALL
# CronJobs across namespaces (per the runbook hint) or who read the runbook
# explicitly will discover it. This creates variance in the data_restored
# subscore: agents who miss this trap will have data re-corrupted after they
# restore it, causing data_restored to fail at grading time.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-count-validator
  namespace: default
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          serviceAccountName: trap-sa
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              sleep $((RANDOM % 30))
              kubectl patch configmap bleat-db -n bleater \
              -p '{"data":{"count":"300"}}' || true
EOF

# -----------------------------
# TRAP TYPE 5 — Schedule overrider (default namespace)
# Changes the aggregator schedule away from */1 * * * * so the agent
# must explicitly restore it. Backs the schedule_unchanged grader check.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-schedule-override
  namespace: default
  labels:
    bleat.io/component: interference
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    metadata:
      labels:
        bleat.io/component: interference
    spec:
      template:
        metadata:
          labels:
            bleat.io/component: interference
        spec:
          restartPolicy: Never
          serviceAccountName: trap-sa
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl patch cronjob bleat-aggregator -n bleater \
              -p '{"spec":{"schedule":"*/5 * * * *"}}' || true
EOF

# -----------------------------
# RED HERRING — harmless shadow CronJob in bleater namespace
# Not a trap; just a distraction. Agents who delete it waste time but suffer
# no penalty. Kept to add noise to the investigation surface.
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-aggregator-shadow
  namespace: bleater
spec:
  schedule: "*/10 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: shadow
            image: busybox
            command: ["sh","-c","echo shadow-check ok"]
EOF

# -----------------------------
# VERIFICATION
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
