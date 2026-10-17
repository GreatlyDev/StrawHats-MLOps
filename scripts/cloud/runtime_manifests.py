"""Render the existing fixed operator and monitoring sources for one runtime."""

import json
from dataclasses import dataclass

import yaml

from backend.app.iris_target import RuntimeTarget

from .deploy import ROOT

BASE = ROOT / "infra" / "kubernetes"
MANAGED = "strawhats-owner-runtime"


def document_key(doc):
    meta = doc["metadata"]
    return doc["apiVersion"], doc["kind"], meta.get("namespace", ""), meta["name"]


@dataclass(frozen=True)
class RenderedRuntime:
    target: RuntimeTarget
    documents: tuple[dict, ...]
    metrics_documents: tuple[dict, ...]
    managed_labels: dict[str, str]
    secret_references: tuple[dict, ...]


def render_runtime(target: RuntimeTarget) -> RenderedRuntime:
    if target.kind not in ("eks", "rehearsal"):
        raise ValueError("Owner releases require an explicit EKS or rehearsal target.")
    labels = {
        "app.kubernetes.io/managed-by": MANAGED,
        "mlops.strawhats.dev/runtime-kind": target.kind,
    }
    namespace_map = {
        "strawhats-operator-local": target.namespace,
        "strawhats-monitoring-local": target.monitoring_namespace,
    }
    docs = []
    for directory, files in (
        (
            "operator",
            ("namespace.yaml", "crd.yaml", "rbac.yaml", "controller.yaml", "iris.yaml"),
        ),
        (
            "monitoring",
            (
                "namespace.yaml",
                "rbac.yaml",
                "prometheus-workload.yaml",
                "grafana-workload.yaml",
            ),
        ),
    ):
        for filename in files:
            docs.extend(
                d
                for d in yaml.safe_load_all(
                    (BASE / directory / filename).read_text(encoding="utf-8")
                )
                if d
            )
    for doc in docs:
        meta = doc["metadata"]
        if doc["kind"] == "Namespace":
            meta["name"] = namespace_map[meta["name"]]
        elif meta.get("namespace") in namespace_map:
            meta["namespace"] = namespace_map[meta["namespace"]]
        meta.setdefault("labels", {}).update(labels)
        for subject in doc.get("subjects", []):
            if subject.get("namespace") in namespace_map:
                subject["namespace"] = namespace_map[subject["namespace"]]
        if doc["kind"] == "ModelDeployment":
            doc["spec"].update(
                image=target.image, imagePullPolicy=target.image_pull_policy
            )
        if doc["kind"] == "Deployment":
            template = doc["spec"]["template"]
            template["metadata"].setdefault("labels", {}).update(labels)
            if meta["name"] == target.operator_name:
                template["spec"]["containers"][0].update(
                    image=target.operator_image,
                    imagePullPolicy=target.image_pull_policy,
                )
    configs = BASE / "monitoring" / "configs"
    prometheus = yaml.safe_load((configs / "prometheus.yaml").read_text())
    prometheus["scrape_configs"][0]["kubernetes_sd_configs"][0]["namespaces"][
        "names"
    ] = [target.namespace]
    datasources = yaml.safe_load((configs / "datasources.yaml").read_text())
    next(d for d in datasources["datasources"] if d["uid"] == "local-prometheus")[
        "url"
    ] = f"http://prometheus.{target.monitoring_namespace}.svc:9090"
    dashboard = json.loads(
        (configs / "iris-dashboard.json").read_text(encoding="utf-8")
    )
    dashboard.update(
        uid="strawhats-owner-iris",
        title=f"StrawHats — {'AWS EKS' if target.kind == 'eks' else 'Local rehearsal'} Iris monitoring",
        tags=["strawhats", target.kind, "iris"],
    )
    # Queries in this dashboard use the iris-model job; verify no hidden local namespace remains.
    if "strawhats-operator-local" in json.dumps(dashboard):
        raise ValueError("Dashboard contains an unexpected fixed namespace.")
    mappings = {
        "prometheus-config": {
            "prometheus.yaml": yaml.safe_dump(prometheus, sort_keys=False),
            "iris-alerts.yaml": (configs / "iris-alerts.yaml").read_text(),
        },
        "grafana-datasource": {
            "datasources.yaml": yaml.safe_dump(datasources, sort_keys=False)
        },
        "grafana-provider": {
            "dashboards.yaml": (configs / "dashboards.yaml").read_text()
        },
        "grafana-dashboard": {"iris-dashboard.json": json.dumps(dashboard, indent=2)},
    }
    # ConfigMaps precede Deployments without relying on generated name hashes.
    config_docs = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": target.monitoring_namespace,
                "labels": dict(labels),
            },
            "data": data,
        }
        for name, data in mappings.items()
    ]
    docs = (
        [d for d in docs if d["kind"] == "Namespace"]
        + config_docs
        + [d for d in docs if d["kind"] != "Namespace"]
    )
    metrics = list(
        yaml.safe_load_all(
            (BASE / "monitoring" / "metrics-server" / "components.yaml").read_text()
        )
    )
    if target.kind == "rehearsal":
        patch = yaml.safe_load(
            (BASE / "monitoring" / "metrics-server" / "local-patch.yaml").read_text()
        )
        deployment = next(d for d in metrics if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        container.update(patch["spec"]["template"]["spec"]["containers"][0])
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "strawhats-grafana-admin",
            "namespace": target.monitoring_namespace,
        },
    }
    return RenderedRuntime(target, tuple(docs), tuple(metrics), labels, (secret,))
