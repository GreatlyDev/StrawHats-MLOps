"""Render deterministic resources; the controller supplies cluster observations."""

from .spec import Target


def render_workload(resource: dict, target: Target) -> tuple[dict, dict]:
    metadata = resource["metadata"]
    labels = {"mlops.strawhats.dev/owner-uid": metadata["uid"]}
    owned = {
        "name": metadata["name"],
        "namespace": metadata["namespace"],
        "labels": labels,
        "ownerReferences": [
            {
                "apiVersion": resource["apiVersion"],
                "kind": "ModelDeployment",
                "name": metadata["name"],
                "uid": metadata["uid"],
                "controller": True,
            }
        ],
    }
    probe = {"httpGet": {"path": "/health", "port": "http"}}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": owned,
        "spec": {
            "replicas": target.replicas,
            "revisionHistoryLimit": 3,
            "progressDeadlineSeconds": 180,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {
                    "maxUnavailable": 0,
                    "maxSurge": 1,
                },
            },
            "selector": {"matchLabels": labels},
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
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "iris",
                            "image": target.image,
                            "imagePullPolicy": target.image_pull_policy,
                            "ports": [{"name": "http", "containerPort": 8000}],
                            "resources": {
                                "requests": {
                                    "cpu": f"{target.cpu_millicores}m",
                                    "memory": f"{target.memory_mi}Mi",
                                },
                                "limits": {
                                    "cpu": f"{target.cpu_millicores * 2}m",
                                    "memory": f"{target.memory_mi * 2}Mi",
                                },
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "startupProbe": {
                                **probe,
                                "periodSeconds": 2,
                                "failureThreshold": 60,
                            },
                            "readinessProbe": {**probe, "periodSeconds": 5},
                            "livenessProbe": {**probe, "periodSeconds": 10},
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "tmp",
                            "emptyDir": {
                                "medium": "Memory",
                                "sizeLimit": "64Mi",
                            },
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": owned,
        "spec": {
            "type": "ClusterIP",
            "selector": labels,
            "ports": [
                {
                    "name": "http",
                    "port": 8000,
                    "targetPort": "http",
                    "protocol": "TCP",
                }
            ],
        },
    }
    return deployment, service
