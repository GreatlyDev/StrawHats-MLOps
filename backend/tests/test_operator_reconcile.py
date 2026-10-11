from copy import deepcopy

import pytest

from backend.model_operator.reconcile import reconcile
from backend.model_operator.spec import parse_target
from backend.model_operator.workload import render_workload


def merge(destination, patch):
    for key, value in patch.items():
        if value is None:
            destination.pop(key, None)
        elif isinstance(value, dict):
            merge(destination.setdefault(key, {}), value)
        else:
            destination[key] = deepcopy(value)


class Cluster:
    """In-memory Kubernetes boundary; all lifecycle decisions remain real."""

    def __init__(self, resource):
        self.resource = resource
        self.deployment = self.service = None
        self.pods = []
        self.replica_sets = []
        self.slices = []
        self.child_writes = []
        self.status_writes = []
        self.status_error = None

    def get_deployment(self, name):
        return self.deployment

    def get_service(self, name):
        return self.service

    def create_deployment(self, body):
        self.child_writes.append("create deployment")
        self.deployment = deepcopy(body)
        self.deployment["metadata"].update(uid="deployment-uid", generation=1)

    def patch_deployment(self, name, body):
        self.child_writes.append("patch deployment")
        merge(self.deployment, body)
        self.deployment["metadata"]["generation"] += 1

    def create_service(self, body):
        self.child_writes.append("create service")
        self.service = deepcopy(body)
        self.service["spec"]["clusterIP"] = "10.96.1.2"

    def patch_service(self, name, body):
        self.child_writes.append("patch service")
        merge(self.service, body)

    def patch_status(self, name, resource_version, status):
        if self.status_error:
            raise self.status_error
        self.status_writes.append(deepcopy(status))
        self.resource["status"] = deepcopy(status)

    def list_replica_sets(self, selector):
        return self.replica_sets

    def list_pods(self, selector):
        return self.pods

    def list_endpoint_slices(self, service):
        return self.slices

    def ready(self):
        spec = self.deployment["spec"]
        n = spec["replicas"]
        self.deployment["status"] = {
            "observedGeneration": self.deployment["metadata"]["generation"],
            "replicas": n,
            "updatedReplicas": n,
            "readyReplicas": n,
            "availableReplicas": n,
        }
        self.replica_sets = [
            {
                "metadata": {
                    "uid": "rs-ready",
                    "ownerReferences": [
                        {
                            "uid": "deployment-uid",
                            "kind": "Deployment",
                            "controller": True,
                        }
                    ],
                },
                "spec": {"template": deepcopy(spec["template"])},
            }
        ]
        self.pods = [
            {
                "metadata": {
                    "uid": f"pod-{i}",
                    "ownerReferences": [
                        {"uid": "rs-ready", "kind": "ReplicaSet", "controller": True}
                    ],
                },
                "spec": deepcopy(spec["template"]["spec"]),
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
            for i in range(n)
        ]
        self.slices = [
            {
                "ports": [{"name": "http", "port": 8000, "protocol": "TCP"}],
                "endpoints": [
                    {
                        "conditions": {"ready": True},
                        "targetRef": {"kind": "Pod", "uid": f"pod-{i}"},
                    }
                    for i in range(n)
                ],
            }
        ]


@pytest.fixture
def resource():
    return {
        "apiVersion": "mlops.strawhats.dev/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "name": "iris",
            "namespace": "strawhats-operator-local",
            "uid": "iris-uid",
            "generation": 1,
            "resourceVersion": "1",
        },
        "spec": {"image": "iris:good"},
    }


@pytest.fixture
def cluster(resource):
    return Cluster(resource)


def verified(resource, cluster):
    reconcile(resource, cluster)
    cluster.ready()
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "Ready"


def test_creates_owned_resources_and_reports_pending(resource, cluster):
    reconcile(resource, cluster)
    assert cluster.child_writes == ["create deployment", "create service"]
    assert resource["status"]["phase"] == "Pending"
    assert "lastReadySpec" not in resource["status"]


def test_ready_snapshot_and_no_op_repeat(resource, cluster):
    verified(resource, cluster)
    assert resource["status"]["lastReadySpec"]["image"] == "iris:good"
    assert resource["status"]["readyReplicas"] == 1
    before = (len(cluster.child_writes), len(cluster.status_writes))
    reconcile(resource, cluster)
    assert (len(cluster.child_writes), len(cluster.status_writes)) == before


def test_scale_changes_replicas_without_restarting_template(resource, cluster):
    verified(resource, cluster)
    template = deepcopy(cluster.deployment["spec"]["template"])
    resource["spec"]["replicas"] = 2
    resource["metadata"]["generation"] = 2
    reconcile(resource, cluster)
    assert cluster.deployment["spec"]["replicas"] == 2
    assert cluster.deployment["spec"]["template"] == template
    assert resource["status"]["phase"] == "Pending"
    cluster.ready()
    reconcile(resource, cluster)
    assert resource["status"]["lastReadySpec"]["replicas"] == 2


def test_failed_image_does_not_replace_snapshot_and_rollback_restores_it(
    resource, cluster
):
    resource["spec"].update(replicas=2, cpuMillicores=100, memoryMi=128)
    verified(resource, cluster)
    snapshot = deepcopy(resource["status"]["lastReadySpec"])
    resource["spec"].update(image="iris:broken", replicas=1, cpuMillicores=500)
    resource["metadata"]["generation"] += 1
    reconcile(resource, cluster)
    cluster.ready()
    cluster.pods[0]["status"] = {
        "containerStatuses": [{"state": {"waiting": {"reason": "ErrImageNeverPull"}}}]
    }
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "Failed"
    assert resource["status"]["lastReadySpec"] == snapshot
    resource["spec"]["mode"] = "Rollback"
    resource["metadata"]["generation"] += 1
    reconcile(resource, cluster)
    cluster.ready()
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "RolledBack"
    assert cluster.deployment["spec"]["replicas"] == 2
    assert (
        cluster.deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        == "iris:good"
    )
    assert resource["status"]["lastReadySpec"] == snapshot
    assert resource["spec"]["image"] == "iris:broken"


def test_rollback_without_snapshot_does_not_write_children(resource, cluster):
    resource["spec"]["mode"] = "Rollback"
    reconcile(resource, cluster)
    assert cluster.child_writes == []
    assert resource["status"]["phase"] == "Failed"


@pytest.mark.parametrize("kind", ["deployment", "service"])
def test_foreign_child_blocks_all_child_writes(resource, cluster, kind):
    deployment, service = render_workload(resource, parse_target(resource["spec"]))
    body = deployment if kind == "deployment" else service
    body["metadata"]["ownerReferences"] = []
    setattr(cluster, kind, body)
    reconcile(resource, cluster)
    assert cluster.child_writes == []
    assert resource["status"]["phase"] == "Conflict"


def test_terminating_resource_is_not_recreated(resource, cluster):
    resource["metadata"]["deletionTimestamp"] = "2026-10-11T15:00:00Z"
    reconcile(resource, cluster)
    assert cluster.child_writes == cluster.status_writes == []


@pytest.mark.parametrize("kind", ["deployment", "service"])
def test_repairs_missing_owned_child(resource, cluster, kind):
    verified(resource, cluster)
    setattr(cluster, kind, None)
    reconcile(resource, cluster)
    assert getattr(cluster, kind) is not None


@pytest.mark.parametrize(
    "fault",
    [
        "endpoints",
        "old-image",
        "old-resources",
        "stale-generation",
        "foreign-rs",
        "terminating-pod",
    ],
)
def test_incomplete_current_revision_never_certifies_ready(resource, cluster, fault):
    reconcile(resource, cluster)
    cluster.ready()
    if fault == "endpoints":
        cluster.slices[0]["endpoints"][0]["targetRef"]["uid"] = "foreign-pod"
    elif fault == "old-image":
        cluster.pods[0]["spec"]["containers"][0]["image"] = "iris:old"
    elif fault == "old-resources":
        cluster.pods[0]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = (
            "100m"
        )
    elif fault == "stale-generation":
        cluster.deployment["status"]["observedGeneration"] = 0
    elif fault == "foreign-rs":
        cluster.replica_sets[0]["metadata"]["ownerReferences"][0]["uid"] = (
            "foreign-deployment"
        )
    else:
        cluster.pods[0]["metadata"]["deletionTimestamp"] = "2026-10-11T15:00:00Z"
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "Pending"
    assert "lastReadySpec" not in resource["status"]


def test_old_ready_pods_cannot_certify_failed_new_image(resource, cluster):
    verified(resource, cluster)
    snapshot = deepcopy(resource["status"]["lastReadySpec"])
    resource["spec"]["image"] = "iris:broken"
    resource["metadata"]["generation"] += 1
    reconcile(resource, cluster)
    cluster.deployment["status"]["observedGeneration"] = 2
    reconcile(resource, cluster)
    assert resource["status"]["phase"] != "Ready"
    assert resource["status"]["lastReadySpec"] == snapshot


def test_status_conflict_propagates_without_certifying_snapshot(resource, cluster):
    reconcile(resource, cluster)
    cluster.ready()
    cluster.status_error = RuntimeError("409 stale resourceVersion")
    with pytest.raises(RuntimeError, match="409"):
        reconcile(resource, cluster)
    assert "lastReadySpec" not in resource["status"]


def test_kubernetes_quantity_normalization_does_not_trigger_writes(resource, cluster):
    resource["spec"]["cpuMillicores"] = 500
    verified(resource, cluster)
    cluster.deployment["spec"]["template"]["spec"]["containers"][0]["resources"][
        "limits"
    ]["cpu"] = "1"
    cluster.pods[0]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1"
    before = len(cluster.child_writes)
    reconcile(resource, cluster)
    assert len(cluster.child_writes) == before
    assert resource["status"]["phase"] == "Ready"


def test_wrong_endpoint_port_name_cannot_certify_readiness(resource, cluster):
    reconcile(resource, cluster)
    cluster.ready()
    cluster.slices[0]["ports"][0]["name"] = "wrong-port-name"
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "Pending"
    assert "lastReadySpec" not in resource["status"]


def test_extra_service_selector_is_removed_and_repair_converges(resource, cluster):
    verified(resource, cluster)
    cluster.service["spec"]["selector"]["unmatched"] = "extra"
    cluster.slices = []
    reconcile(resource, cluster)
    assert cluster.service["spec"]["selector"] == {
        "mlops.strawhats.dev/owner-uid": "iris-uid",
    }
    assert cluster.service["spec"]["clusterIP"] == "10.96.1.2"
    cluster.ready()
    reconcile(resource, cluster)
    assert resource["status"]["phase"] == "Ready"
    writes = len(cluster.child_writes)
    reconcile(resource, cluster)
    assert len(cluster.child_writes) == writes


@pytest.mark.parametrize(
    "image", ["https://example.com/iris:latest", "iris::latest", "!!!"]
)
def test_malformed_image_does_not_write_children(resource, cluster, image):
    resource["spec"]["image"] = image
    reconcile(resource, cluster)
    assert cluster.child_writes == []
    assert resource["status"]["phase"] == "Failed"
