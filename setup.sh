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
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-concurrency
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
# TRAP TYPE 2 — Deadline remover (default namespace)
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
          serviceAccountName: trap-sa
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
# TRAP TYPE 3 — Job killer (default namespace)
# -----------------------------
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bleat-trap-killer
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
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              kubectl delete job -n bleater -l job-name=bleat-aggregator || true
EOF

# -----------------------------
# TRAP TYPE 4 — Data corruption (default namespace)
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
          serviceAccountName: trap-sa
          containers:
          - name: trap
            image: bitnami/kubectl:latest
            command:
            - sh
            - -c
            - |
              sleep $((RANDOM % 40))
              kubectl patch configmap bleat-db -n bleater \
              -p '{"data":{"count":"300"}}' || true
EOF

# -----------------------------
# TRAP TYPE 5 — Fake resource creator
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
            command: ["sh","-c","echo shadow"]
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
