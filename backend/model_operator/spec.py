"""The small, bounded ModelDeployment desired-state contract."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    image: str
    image_pull_policy: str
    replicas: int
    cpu_millicores: int
    memory_mi: int

    def to_spec(self) -> dict:
        return {
            "image": self.image,
            "imagePullPolicy": self.image_pull_policy,
            "replicas": self.replicas,
            "cpuMillicores": self.cpu_millicores,
            "memoryMi": self.memory_mi,
        }


def parse_target(spec: dict) -> Target:
    if not isinstance(spec, dict):
        raise ValueError("A model configuration is required")
    image = spec.get("image")
    if not isinstance(image, str) or not re.fullmatch(r"\S+", image):
        raise ValueError("image must be a nonempty image reference without whitespace")
    policy = spec.get("imagePullPolicy", "Never")
    if policy not in ("Never", "IfNotPresent"):
        raise ValueError("imagePullPolicy must be Never or IfNotPresent")
    values = {}
    for field, default, low, high in (
        ("replicas", 1, 1, 2),
        ("cpuMillicores", 250, 100, 500),
        ("memoryMi", 256, 128, 512),
    ):
        value = spec.get(field, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{field} must be an integer from {low} through {high}")
        values[field] = value
    return Target(image, policy, *values.values())


def choose_target(resource: dict) -> Target:
    mode = resource["spec"].get("mode", "Deploy")
    if mode == "Deploy":
        return parse_target(resource["spec"])
    if mode == "Rollback":
        snapshot = resource.get("status", {}).get("lastReadySpec")
        if not snapshot:
            raise ValueError("Rollback requires a previously verified ready deployment")
        return parse_target(snapshot)
    raise ValueError("mode must be Deploy or Rollback")
