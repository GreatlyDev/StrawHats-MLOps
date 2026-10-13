# Local Iris monitoring

This is one local Prometheus/Grafana setup for the existing Docker Desktop Iris
operator. It adds no AWS deployment and uses no AI provider. The application's
dashboard and persisted workspace prediction history remain separate.

## Components and data

- Prometheus 3.15.0 discovers and scrapes each Iris Pod in
  `strawhats-operator-local`; it does not scrape a load-balanced counter stream.
- Grafana 13.2.4 provisions one read-only dashboard, UID `strawhats-local-iris`.
- metrics-server 0.9.0 supplies Kubernetes pod CPU/memory for `kubectl top`.
  It requires Kubernetes 1.34+.
- Model metrics include successful prediction counts, duration histogram,
  three predicted-class counters and Linux serving-process CPU/resident memory.
  The resource panels show **process** usage; `kubectl top` reports **pod** usage.
  These have different definitions.

The single `IrisPredictionDistributionSkew` warning uses counter increases over
two minutes. It requires one class to exceed 90% with at least 20 recent predictions
continuously for 30s, good scrapes and all three counters on every target. This is
a concentration indicator, not proof of accuracy loss or a reason to auto-scale.
Unavailable telemetry and insufficient observations are shown explicitly.

Prometheus retains 6h/256MB in ephemeral storage; Grafana configuration is
provisioned from source. Recreated monitoring Pods lose transient history, then
recover their datasource/dashboard/rules. Generate fresh traffic to warm charts.
Model counters reset if its serving process restarts.

## Local preparation and ownership

Use Python 3.12, Docker Desktop Kubernetes, kubectl and the repository's existing
dependencies. Every command must explicitly target `docker-desktop`. Inspect the
namespace, fixed resource names and owner labels before creating anything; stop
if another project already owns a target. Existing `kube-system` is not modified
as a namespace, and no existing foreign object may be overwritten.

Build `strawhats-iris:local-monitoring` with `backend/Dockerfile.model` and import
it into the verified local kind node using the binary image-loader workflow in
[the operator setup](../operator/README.md). Preserve the prior image. Upgrade the
existing `ModelDeployment/iris` through its CR, retaining its verified replicas,
CPU and memory; if mode is Rollback, start from its verified `lastReadySpec`.
Do not patch the operator-managed Deployment directly. Wait for observed readiness.

Render/inspect both configurations before installation:

```powershell
kubectl kustomize infra/kubernetes/monitoring
kubectl kustomize infra/kubernetes/monitoring/metrics-server
kubectl --context docker-desktop get pods -n strawhats-operator-local
```

Monitoring objects carry `app.kubernetes.io/managed-by=strawhats-local-monitoring`
and `app.kubernetes.io/part-of=strawhats-mlops`. Prometheus runtime permissions are
only get/list/watch Pods in the Iris namespace. Grafana mounts no Kubernetes token.
The metrics-server addon uses its upstream scoped/delegated resource permissions.

After inspecting for foreign collisions, create the dedicated monitoring namespace
from the rendered configuration, then create a locally generated Secret named
`strawhats-grafana-admin` there with keys `admin-user` and `admin-password` and a
random password. Keep its payload outside Git and do not print it. Validate remaining
manifests with server dry-run after namespace creation. Create approved new objects;
never use an install command to overwrite a raced foreign object. Reuse only
already-owned matching configuration.

The metrics-server base is the official v0.9.0 release `components.yaml`, preserved
with [its Apache license](metrics-server/LICENSE). Upstream SHA256:
`1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b`.
The local overlay bounds resources. If Docker Desktop's kubelet certificate needs
the testing-only `--kubelet-insecure-tls` exception, it belongs exclusively in this
local overlay and must be documented after verification; never apply it to AWS.

Wait for Prometheus/Grafana/metrics-server Deployments and metrics API readiness.
Verify real resource readings:

```powershell
kubectl --context docker-desktop top pods -n strawhats-operator-local
```

## View and verify

In separate local terminals, forward only to loopback:

```powershell
kubectl --context docker-desktop -n strawhats-monitoring-local port-forward service/prometheus 9090:9090 --address 127.0.0.1
kubectl --context docker-desktop -n strawhats-monitoring-local port-forward service/grafana 3000:3000 --address 127.0.0.1
```

Open [the dashboard](http://127.0.0.1:3000/d/strawhats-local-iris) and
[Prometheus alerts](http://127.0.0.1:9090/alerts). Grafana is an anonymous Viewer;
its provisioned dashboard cannot be saved from the UI. Neither Service is public.

Run the read-only observation check, then explicitly request bounded demo traffic:

```powershell
.venv/Scripts/python.exe -m scripts.monitoring.observations
.venv/Scripts/python.exe -m scripts.monitoring.traffic --run-demo
```

The demo uses only the fixed existing Iris Service: balanced baseline, Setosa-heavy
traffic until the warning fires, then balanced traffic until it recovers with
healthy telemetry and adequate samples. It sends at most 2 requests/s, one in flight,
1000 successful requests, and starts no new work after its 600s deadline. Existing
finite transport timeouts apply to an in-flight operation. A deployment revision
change, missing telemetry or transport error stops it. Evidence is saved under
ignored `.local/milestone-four/`; no synthetic metrics are sent to the live cluster.

The workload must remain ready, and application predictions must still report
Kubernetes provenance and leave workspace prediction totals unchanged. This local
monitoring warning is not supplied to the assistant or to an automatic policy.

## Offline configuration tests and cleanup

Use the pinned image's promtool with network disabled and read-only mounts of
`configs` at `/etc/prometheus` and `tests` at `/tests`:

```text
promtool check config --syntax-only /etc/prometheus/prometheus.yaml
promtool test rules /tests/iris-alerts.test.yaml
```

CI runs those checks without cluster credentials. Rule fixtures cover firing
delay, balanced recovery, low/zero samples, resets, two replicas, partial older
images and failed/missing targets. Synthetic series are used only in those tests.

Cleanup is explicit: verify ownership, then remove only the rendered monitoring
objects (including the Iris discovery Role/RoleBinding), its local admin Secret
and monitoring namespace. Remove the nine rendered metrics-server objects only
if this project created and still owns them. Stop owned port-forwards. Preserve
`kube-system`, Iris/operator resources, model images, source branches and all
unrelated objects; restore a saved model target only if the owner requests it.

The exact monitoring inventory is the namespace `strawhats-monitoring-local`,
ServiceAccount `prometheus`, Deployments/Services `prometheus` and `grafana`, four
generated ConfigMaps (`prometheus-config`, `grafana-datasource`, `grafana-provider`,
`grafana-dashboard` with Kustomize content suffixes), Secret `strawhats-grafana-admin`,
and Role/RoleBinding `strawhats-prometheus-discovery` in `strawhats-operator-local`.
Use rendered names for the ConfigMaps. The nine addon objects are ServiceAccount,
Service and Deployment `metrics-server` in `kube-system`, RoleBinding
`metrics-server-auth-reader` there, ClusterRoles `system:aggregated-metrics-reader`
and `system:metrics-server`, ClusterRoleBindings `metrics-server:system:auth-delegator`
and `system:metrics-server`, and APIService `v1beta1.metrics.k8s.io`.
