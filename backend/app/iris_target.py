"""Immutable internal bindings for the three supported Iris runtimes."""

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RuntimeTarget:
    kind: Literal["local", "eks", "rehearsal"]
    context: str
    namespace: str
    monitoring_namespace: str
    name: str
    image: str
    image_pull_policy: str
    operator_name: str
    operator_image: str

    def __post_init__(self):
        if self.name != "iris" or self.operator_name != "strawhats-model-operator":
            raise ValueError("Unsupported Iris resource identity.")
        if self.kind == "local":
            valid = (
                self.context == "docker-desktop"
                and self.namespace == "strawhats-operator-local"
                and self.monitoring_namespace == "strawhats-monitoring-local"
                and self.image == "strawhats-iris:local-monitoring"
                and self.operator_image == "strawhats-model-operator:local"
                and self.image_pull_policy == "Never"
            )
        elif self.kind == "rehearsal":
            model = re.fullmatch(
                r"strawhats-iris:rehearsal-([a-f0-9]{7,40})", self.image
            )
            valid = (
                self.context == "docker-desktop"
                and self.namespace == "strawhats"
                and self.monitoring_namespace == "strawhats-monitoring"
                and self.image_pull_policy == "Never"
                and model is not None
                and self.operator_image
                == f"strawhats-model-operator:rehearsal-{model[1]}"
            )
        elif self.kind == "eks":
            account = re.fullmatch(
                r"arn:aws:eks:us-east-1:([0-9]{12}):cluster/strawhats-midterm",
                self.context,
            )
            registry = (
                f"{account[1]}.dkr.ecr.us-east-1.amazonaws.com" if account else ""
            )
            valid = (
                account is not None
                and self.namespace == "strawhats"
                and self.monitoring_namespace == "strawhats-monitoring"
                and self.image_pull_policy == "IfNotPresent"
                and re.fullmatch(
                    re.escape(registry)
                    + r"/strawhats-midterm/iris@sha256:[a-f0-9]{64}",
                    self.image,
                )
                is not None
                and re.fullmatch(
                    re.escape(registry)
                    + r"/strawhats-midterm/operator@sha256:[a-f0-9]{64}",
                    self.operator_image,
                )
                is not None
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Runtime fields do not match a supported Iris binding.")


LOCAL_TARGET = RuntimeTarget(
    kind="local",
    context="docker-desktop",
    namespace="strawhats-operator-local",
    monitoring_namespace="strawhats-monitoring-local",
    name="iris",
    image="strawhats-iris:local-monitoring",
    image_pull_policy="Never",
    operator_name="strawhats-model-operator",
    operator_image="strawhats-model-operator:local",
)
