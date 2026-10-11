# Local Iris lifecycle operator

This operator implements one `ModelDeployment` CRD for Iris deployment, scaling,
and explicit rollback. It runs in `strawhats-operator-local` on Docker Desktop's
`docker-desktop` context. The opt-in Local Iris dashboard panel drives this
operator; manifest previews remain available separately. AWS remains deferred.

## Build and install

Use Docker Desktop with Kubernetes running, kubectl, and the project's Python
3.12 environment. Run commands from the repository root. First check the context,
namespace and CRD; stop if their existing resources belong to another project.
The CRD installation needs local administrator access; the controller's runtime
permissions remain limited to its namespace.

```powershell
kubectl --context docker-desktop get nodes
kubectl --context docker-desktop get namespace strawhats-operator-local --ignore-not-found
kubectl --context docker-desktop get crd modeldeployments.mlops.strawhats.dev --ignore-not-found
docker build --platform linux/amd64 -f backend/Dockerfile.model -t strawhats-iris:milestone-one .
docker build --platform linux/amd64 -f backend/Dockerfile.operator -t strawhats-model-operator:local .
```

Docker Desktop's kind provisioner has a separate node image store. For the
verified single-node installation named `desktop-control-plane`, import only
these two local images before installing the workloads. This uses a binary
subprocess stream because PowerShell text pipes can corrupt image archives.
Confirm that `docker inspect desktop-control-plane` identifies your local node.
For another local provisioner/node, use its corresponding local image loader.

```powershell
New-Item -ItemType Directory -Force .local/operator | Out-Null
docker save --platform linux/amd64 --output .local/operator/images.tar strawhats-model-operator:local strawhats-iris:milestone-one
@'
import subprocess
with open('.local/operator/images.tar', 'rb') as stream:
    subprocess.run([
        'docker', 'exec', '-i', 'desktop-control-plane', 'ctr', '-n', 'k8s.io',
        'images', 'import', '--platform', 'linux/amd64', '-'
    ], stdin=stream, check=True)
'@ | .\.venv\Scripts\python.exe -
```

Create the dedicated namespace first: server dry-run does not persist it for
validation of the subsequent namespaced objects. Validate before the full install.
The example custom resource is applied separately from the controller resources.

```powershell
kubectl --context docker-desktop apply -f infra/kubernetes/operator/namespace.yaml
kubectl --context docker-desktop apply --dry-run=server -k infra/kubernetes/operator
kubectl --context docker-desktop apply -k infra/kubernetes/operator
kubectl --context docker-desktop -n strawhats-operator-local rollout status deployment/strawhats-model-operator --timeout=60s
kubectl --context docker-desktop apply -f infra/kubernetes/operator/iris.yaml
kubectl --context docker-desktop -n strawhats-operator-local wait modeldeployment/iris --for=jsonpath='{.status.phase}'=Ready --timeout=180s
kubectl --context docker-desktop -n strawhats-operator-local get modeldeployment/iris -o yaml
```

## Predict, scale, and roll back

In one terminal, forward the model Service only to localhost:

```powershell
kubectl --context docker-desktop -n strawhats-operator-local port-forward service/iris 18082:8000 --address 127.0.0.1
```

In another terminal, verify actual health, prediction, and metric increments:

```powershell
.\.venv\Scripts\python.exe -m scripts.cloud.smoke --base-url http://127.0.0.1:18082
kubectl --context docker-desktop -n strawhats-operator-local scale modeldeployment/iris --replicas=2
kubectl --context docker-desktop -n strawhats-operator-local wait modeldeployment/iris --for=jsonpath='{.status.readyReplicas}'=2 --timeout=180s
```

The smoke module makes HTTP calls to the supplied local URL; it performs no AWS
operations. The controller saves `status.lastReadySpec` only after the requested
revision's owned Pods and Service endpoints are ready. A failed update preserves
that snapshot and the rolling strategy keeps prior working Pods available.

To demonstrate a failed update and rollback, use ignored patch files:

```powershell
New-Item -ItemType Directory -Force .local/operator | Out-Null
'{"spec":{"image":"strawhats-iris:deliberately-missing-local"}}' | Set-Content .local/operator/fail.json
kubectl --context docker-desktop -n strawhats-operator-local patch modeldeployment/iris --type=merge --patch-file=.local/operator/fail.json
kubectl --context docker-desktop -n strawhats-operator-local wait modeldeployment/iris --for=jsonpath='{.status.phase}'=Failed --timeout=180s
'{"spec":{"mode":"Rollback"}}' | Set-Content .local/operator/rollback.json
kubectl --context docker-desktop -n strawhats-operator-local patch modeldeployment/iris --type=merge --patch-file=.local/operator/rollback.json
kubectl --context docker-desktop -n strawhats-operator-local wait modeldeployment/iris --for=jsonpath='{.status.phase}'=RolledBack --timeout=180s
```

Rollback restores the entire saved image, pull policy, replica and resource
configuration. While `mode: Rollback` remains set, other desired fields do not
override that snapshot. To resume deployment, correct the desired fields and set
`mode: Deploy` (or reapply the example to restore its one-replica configuration).
Rollback before any verified working deployment reports failure without creating
children. The snapshot survives controller restarts.

Only 1–2 replicas, 100–500 CPU millicores, and 128–512Mi requested memory are
allowed; limits are twice requests. Local examples use `imagePullPolicy: Never`.
Image references use lowercase repository/registry names, an optional tag, and/or
a full lowercase SHA256 digest. URLs, whitespace, and malformed references are
rejected before child resources change.
The controller refuses foreign/unowned same-name children and repairs missing
owned resources. It receives no Secret permissions, cluster-wide role, or AWS
credentials. Operator updates use Recreate to keep one active reconciler.

## Verified scope and cleanup

On October 11, 2026, Docker Desktop Kubernetes 1.34.3 verified real Iris inference,
one-to-two scaling, failed-image reporting, explicit rollback, operator restart,
owned Service repair, admission bounds, and namespace-scoped RBAC. This is local
evidence; cloud deployment, public access, UI integration and the remaining
monitoring/AI assignment requirements still need separate verification.

To stop this local demo, first inspect the namespace and ensure every resource
there belongs to this installation. Then delete only its namespace:

```powershell
kubectl --context docker-desktop -n strawhats-operator-local get modeldeployments,deployments,services,pods
kubectl --context docker-desktop delete namespace strawhats-operator-local
```

The shared CRD can remain installed. Delete it only after checking that no
ModelDeployment in any namespace belongs to another user or task. Never reset
Docker Desktop's cluster to clean up this project.
## Dashboard integration

After this local installation is ready, enable `LOCAL_OPERATOR_ENABLED=1` in the
backend's `.env.local` and restart it. The existing **Deployments → Local Iris**
panel can deploy the fixed Iris image, observe status, scale between one and two
replicas, and request snapshot rollback. See the root README for the local caller
restrictions and the distinction between accepted requests and observed readiness.
