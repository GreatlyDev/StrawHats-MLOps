import os
import shutil
import subprocess
from uuid import uuid4

import yaml

from .schemas import DeploymentInput
from .store import now


def preview(config: DeploymentInput) -> dict:
    labels = {
        "app.kubernetes.io/name": config.name,
        "app.kubernetes.io/managed-by": "strawhats",
    }
    resources = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": config.namespace},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": config.name,
                "namespace": config.namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": config.replicas,
                "revisionHistoryLimit": 3,
                "selector": {"matchLabels": {"app.kubernetes.io/name": config.name}},
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {
                            "prometheus.io/scrape": "true",
                            "prometheus.io/path": "/metrics",
                            "prometheus.io/port": "8000",
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "model",
                                "image": config.image,
                                "ports": [{"containerPort": 8000}],
                                "resources": {
                                    "requests": {
                                        "cpu": f"{config.cpu_millicores}m",
                                        "memory": f"{config.memory_mebibytes}Mi",
                                    },
                                    "limits": {
                                        "cpu": f"{config.cpu_millicores * 2}m",
                                        "memory": f"{config.memory_mebibytes * 2}Mi",
                                    },
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/health", "port": 8000},
                                    "initialDelaySeconds": 10,
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/health", "port": 8000},
                                    "initialDelaySeconds": 30,
                                    "periodSeconds": 10,
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": config.name, "namespace": config.namespace},
            "spec": {
                "selector": {"app.kubernetes.io/name": config.name},
                "ports": [{"port": 80, "targetPort": 8000}],
                "type": "ClusterIP",
            },
        },
    ]
    return {
        "id": f"plan-{uuid4().hex[:12]}",
        **config.model_dump(),
        "created_at": now(),
        "status": "preview",
        "manifest": yaml.safe_dump_all(resources, sort_keys=False),
    }


def cluster_status(enabled: bool = True) -> dict:
    result = {
        "connected": False,
        "context": None,
        "message": "No cluster is reachable. Deployment previews are available.",
    }
    executable = shutil.which("kubectl") if enabled else None
    if executable is None:
        return result
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": 4,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    try:
        context = subprocess.run(
            [executable, "config", "current-context"], check=False, **kwargs
        )
        if context.returncode == 0:
            result["context"] = context.stdout.strip()
        query = subprocess.run(
            [executable, "--request-timeout=2s", "get", "namespaces", "-o", "name"],
            check=False,
            **kwargs,
        )
        if query.returncode == 0:
            result.update(
                connected=True,
                message="Cluster reachable. This release creates previews only.",
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result
