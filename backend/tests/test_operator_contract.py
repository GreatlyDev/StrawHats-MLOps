from pathlib import Path

import pytest
import yaml

from backend.model_operator.spec import choose_target, parse_target
from backend.model_operator.workload import render_workload

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "infra/kubernetes/operator"


@pytest.fixture
def resource():
    return {
        "apiVersion": "mlops.strawhats.dev/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "name": "iris",
            "namespace": "strawhats-operator-local",
            "uid": "test-iris-owner",
            "generation": 1,
            "resourceVersion": "1",
        },
        "spec": {"image": "strawhats-iris:milestone-one"},
    }


def test_target_defaults(resource):
    assert parse_target(resource["spec"]).to_spec() == {
        "image": "strawhats-iris:milestone-one",
        "imagePullPolicy": "Never",
        "replicas": 1,
        "cpuMillicores": 250,
        "memoryMi": 256,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("replicas", 0),
        ("replicas", 3),
        ("replicas", True),
        ("replicas", 1.5),
        ("cpuMillicores", 99),
        ("cpuMillicores", 501),
        ("memoryMi", 127),
        ("memoryMi", 513),
        ("memoryMi", None),
        ("image", ""),
        ("image", "  "),
        ("image", "iris image"),
        ("image", 2),
        ("imagePullPolicy", "Always"),
    ],
)
def test_rejects_invalid_target(resource, field, value):
    resource["spec"][field] = value
    with pytest.raises(ValueError):
        parse_target(resource["spec"])


def test_requires_image():
    with pytest.raises(ValueError):
        parse_target({})


@pytest.mark.parametrize("replicas,cpu,memory", [(1, 100, 128), (2, 500, 512)])
def test_accepts_resource_boundaries(resource, replicas, cpu, memory):
    resource["spec"].update(replicas=replicas, cpuMillicores=cpu, memoryMi=memory)
    assert parse_target(resource["spec"]).replicas == replicas


def test_rollback_uses_persisted_configuration(resource):
    resource["spec"].update(mode="Rollback", image="broken:local", replicas=1)
    resource["status"] = {
        "lastReadySpec": {
            "image": "iris:good",
            "imagePullPolicy": "Never",
            "replicas": 2,
            "cpuMillicores": 100,
            "memoryMi": 128,
        }
    }
    assert choose_target(resource).to_spec() == resource["status"]["lastReadySpec"]


@pytest.mark.parametrize("mode", ["Rollback", "SomethingElse"])
def test_rejects_invalid_mode_or_missing_rollback(resource, mode):
    resource["spec"]["mode"] = mode
    with pytest.raises(ValueError):
        choose_target(resource)


def test_renders_owned_bounded_workload(resource):
    deployment, service = render_workload(resource, parse_target(resource["spec"]))
    owner = deployment["metadata"]["ownerReferences"][0]
    assert owner["uid"] == "test-iris-owner" and owner["controller"] is True
    assert not owner.get("blockOwnerDeletion", False)
    assert service["metadata"]["ownerReferences"] == [owner]
    template = deployment["spec"]["template"]
    assert service["spec"]["selector"] == template["metadata"]["labels"]
    pod = template["spec"]
    container = pod["containers"][0]
    assert container["imagePullPolicy"] == "Never"
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": "http"}
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8000
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxUnavailable": 0,
        "maxSurge": 1,
    }


def test_scale_does_not_change_pod_template(resource):
    first, _ = render_workload(resource, parse_target(resource["spec"]))
    resource["spec"]["replicas"] = 2
    resource["metadata"]["generation"] = 2
    second, _ = render_workload(resource, parse_target(resource["spec"]))
    assert first["spec"]["template"] == second["spec"]["template"]
    assert second["spec"]["replicas"] == 2


def test_crd_has_validated_scale_and_status_contract():
    crd = yaml.safe_load((MANIFESTS / "crd.yaml").read_text())
    version = crd["spec"]["versions"][0]
    assert crd["spec"]["scope"] == "Namespaced"
    assert version["subresources"]["scale"] == {
        "specReplicasPath": ".spec.replicas",
        "statusReplicasPath": ".status.replicas",
        "labelSelectorPath": ".status.selector",
    }
    fields = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    for field, low, high in [
        ("replicas", 1, 2),
        ("cpuMillicores", 100, 500),
        ("memoryMi", 128, 512),
    ]:
        assert fields[field]["type"] == "integer"
        assert (fields[field]["minimum"], fields[field]["maximum"]) == (low, high)
    assert fields["image"]["pattern"] == r"^\S+$"
