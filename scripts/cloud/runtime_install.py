"""Create and remove only receipt-bound runtime objects; acceptance is separate."""

import secrets
import time
from copy import deepcopy
from dataclasses import replace

from kubernetes import client, dynamic
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError
from kubernetes.dynamic.resource import ResourceList
from kubernetes.utils.quantity import parse_quantity

from .runtime_contract import ObjectRecord, fingerprint
from .runtime_manifests import document_key, render_runtime


def _matches(actual, desired):
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches(actual[k], v) for k, v in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(desired)
            and all(_matches(a, d) for a, d in zip(actual, desired))
        )
    return actual == desired


def _configuration_matches(actual, desired):
    return all(
        _matches(actual.get("metadata", {}).get(field, {}), expected)
        for field, expected in desired.get("metadata", {}).items()
        if field in ("labels", "annotations")
    ) and all(
        _matches(actual.get(k), v)
        for k, v in desired.items()
        if k not in ("apiVersion", "kind", "metadata")
    )


def _shared_configuration(desired):
    desired = deepcopy(desired)
    labels = desired.get("metadata", {}).get("labels", {})
    for field in ("app.kubernetes.io/managed-by", "mlops.strawhats.dev/runtime-kind"):
        labels.pop(field, None)
    return desired


def _recorded_configuration_matches(actual, desired, *, borrowed=False):
    if borrowed:
        desired = _shared_configuration(desired)
    if desired["kind"] == "ModelDeployment":
        from backend.model_operator.spec import choose_target, parse_target

        try:
            for target in (parse_target(actual["spec"]), choose_target(actual)):
                if (
                    target.image != desired["spec"]["image"]
                    or target.image_pull_policy != desired["spec"]["imagePullPolicy"]
                ):
                    return False
        except (ValueError, KeyError, TypeError):
            return False
        desired = deepcopy(desired)
        for field in ("replicas", "cpuMillicores", "memoryMi", "mode"):
            desired["spec"].pop(field, None)
    return _configuration_matches(actual, desired)


def _record(doc, actual, ownership):
    version, kind, namespace, name = document_key(doc)
    return ObjectRecord(
        version,
        kind,
        namespace,
        name,
        actual["metadata"]["uid"],
        ownership,
        fingerprint(doc),
    )


def persist(connection, receipt):
    receipt.save(connection.receipt_path)
    connection.receipt = receipt
    return receipt


def _capture(connection, doc, actual, ownership):
    record = _record(doc, actual, ownership)
    return persist(
        connection,
        replace(
            connection.receipt,
            phase="partial",
            inventory=(*connection.receipt.inventory, record),
        ),
    )


def _validate_existing(connection, doc, actual, records, shared):
    identity = document_key(doc)
    record = records.get(identity)
    if record:
        if actual is None or actual["metadata"]["uid"] != record.uid:
            raise RuntimeError("Recorded runtime object UID changed.")
        if record.manifest_sha256 != fingerprint(doc):
            raise RuntimeError(
                "Installed source configuration differs from this receipt."
            )
        if not _recorded_configuration_matches(
            actual, doc, borrowed=record.ownership == "borrowed"
        ):
            raise RuntimeError("Recorded runtime configuration drift detected.")
        return record.ownership
    if actual:
        if identity in shared and _configuration_matches(
            actual, _shared_configuration(doc)
        ):
            if doc["kind"] == "Deployment" and not deployment_ready(actual):
                raise RuntimeError("Existing shared addon is not ready.")
            return "borrowed"
        raise RuntimeError(
            "A foreign or incompatible object blocks runtime installation."
        )
    return None


def deployment_ready(doc):
    status = doc.get("status", {})
    count = doc["spec"].get("replicas", 1)
    return status.get("observedGeneration", 0) >= doc["metadata"].get(
        "generation", 1
    ) and all(
        status.get(k, 0) == count
        for k in ("readyReplicas", "availableReplicas", "updatedReplicas")
    )


def _capacity(documents):
    cpu = memory = 0
    for doc in documents:
        if doc["kind"] == "ModelDeployment":
            cpu += doc["spec"]["cpuMillicores"] * doc["spec"]["replicas"]
            memory += doc["spec"]["memoryMi"] * doc["spec"]["replicas"]
        if doc["kind"] == "Deployment":
            replicas = doc["spec"].get("replicas", 1)
            for container in doc["spec"]["template"]["spec"]["containers"]:
                req = container.get("resources", {}).get("requests", {})
                cpu += float(parse_quantity(req.get("cpu", "0"))) * 1000 * replicas
                memory += (
                    float(parse_quantity(req.get("memory", "0"))) / 2**20 * replicas
                )
    return cpu, memory


def install_runtime(connection, bundle, *, evidence_dir):
    if (
        connection.purpose != "install"
        or connection.target != bundle.target
        or connection.receipt.phase not in ("prepared", "partial", "installed")
    ):
        raise RuntimeError("A matching installation-purpose connection is required.")
    receipt, kube = connection.receipt, connection.kube
    records = {document_key(r.document()): r for r in receipt.inventory}
    shared = {document_key(d) for d in bundle.metrics_documents} | {
        document_key(d)
        for d in bundle.documents
        if d["kind"] == "CustomResourceDefinition"
    }
    # Namespaces/CRD precede the objects whose server-side validation needs them.
    ordered = [
        d
        for d in bundle.documents
        if d["kind"] in ("Namespace", "CustomResourceDefinition")
    ]
    ordered += list(bundle.metrics_documents)
    ordered += list(bundle.secret_references)
    ordered += [
        d
        for d in bundle.documents
        if d["kind"] not in ("Namespace", "CustomResourceDefinition")
    ]
    existing, ownership = {}, {}
    for doc in ordered:
        identity = document_key(doc)
        actual = kube.get(doc)
        existing[identity] = actual
        ownership[identity] = _validate_existing(
            connection, doc, actual, records, shared
        )
    # A direct legacy model cannot be adopted, even when target namespaces are free.
    model_record = records.get(
        (
            "mlops.strawhats.dev/v1alpha1",
            "ModelDeployment",
            bundle.target.namespace,
            "iris",
        )
    )
    for kind, version in (("Deployment", "apps/v1"), ("Service", "v1")):
        actual = kube.get(
            {
                "apiVersion": version,
                "kind": kind,
                "metadata": {"namespace": bundle.target.namespace, "name": "iris"},
            }
        )
        if actual and (
            model_record is None
            or not any(
                o.get("uid") == model_record.uid and o.get("controller") is True
                for o in actual["metadata"].get("ownerReferences", [])
            )
        ):
            raise RuntimeError("A foreign or legacy Iris workload cannot be adopted.")
    free = kube.available_capacity()
    required = _capacity([d for d in ordered if existing[document_key(d)] is None])
    if any(need > available for need, available in zip(required, free)):
        raise RuntimeError("Insufficient ready-worker capacity; no worker was added.")
    for doc in ordered:
        identity = document_key(doc)
        if identity in records:
            continue
        if ownership[identity] == "borrowed":
            _capture(connection, doc, existing[identity], "borrowed")
            continue
        rendered = deepcopy(doc)
        rendered["metadata"].setdefault("labels", {}).update(bundle.managed_labels)
        rendered["metadata"]["labels"]["mlops.strawhats.dev/release-id"] = (
            receipt.release_id
        )
        if doc["kind"] == "Secret":
            rendered.update(
                type="Opaque",
                stringData={
                    "admin-user": "admin",
                    "admin-password": secrets.token_urlsafe(32),
                },
            )
        # Dry-run and create are both create-only: a raced object returns 409.
        kube.create(rendered, dry_run=True)
        connection.check_mutation()
        actual = kube.create(rendered)
        _capture(connection, doc, actual, "created")
        if doc["kind"] == "CustomResourceDefinition" and hasattr(kube, "wait_crd"):
            kube.wait_crd(doc)
    connection.check_mutation()
    return persist(connection, replace(connection.receipt, phase="installed"))


def _check_inventory(connection, receipt, *, require_present=False):
    for record in receipt.inventory:
        actual = connection.kube.get(record.document())
        if require_present and actual is None:
            raise RuntimeError("A recorded runtime object is missing.")
        if actual and actual["metadata"].get("uid") != record.uid:
            raise RuntimeError("Runtime object UID changed; no deletion attempted.")


def _automatic_endpoints(child, children, known):
    """The legacy endpoint controller omits Service ownerReferences."""
    meta = child["metadata"]
    if (
        child["kind"] != "Endpoints"
        or meta.get("labels", {}).get("endpoints.kubernetes.io/managed-by")
        != "endpoint-controller"
    ):
        return False
    service = next(
        (
            d
            for d in children
            if d["kind"] == "Service"
            and d["metadata"].get("uid") in known
            and d["metadata"].get("namespace") == meta.get("namespace")
            and d["metadata"]["name"] == meta["name"]
        ),
        None,
    )
    selector = (service or {}).get("spec", {}).get("selector", {})
    if not selector:
        return False
    pods = {
        d["metadata"]["uid"]: d
        for d in children
        if d["kind"] == "Pod"
        and d["metadata"].get("uid") in known
        and d["metadata"].get("namespace") == meta.get("namespace")
        and all(
            d["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()
        )
    }
    for subset in child.get("subsets", []):
        for address in (
            *subset.get("addresses", []),
            *subset.get("notReadyAddresses", []),
        ):
            ref = address.get("targetRef", {})
            pod = pods.get(ref.get("uid"))
            if (
                not pod
                or ref.get("kind") != "Pod"
                or ref.get("namespace") != meta.get("namespace")
                or ref.get("name") != pod["metadata"]["name"]
            ):
                return False
    return True


def _check_children(connection, receipt):
    known = {r.uid for r in receipt.inventory}
    for namespace in (
        r.name
        for r in receipt.inventory
        if r.kind == "Namespace" and r.ownership == "created"
    ):
        children = connection.kube.children(namespace)
        changed = True
        while changed:
            changed = False
            for child in children:
                meta = child["metadata"]
                if meta.get("uid") not in known and any(
                    o.get("uid") in known and o.get("controller") is True
                    for o in meta.get("ownerReferences", [])
                ):
                    known.add(meta["uid"])
                    changed = True
        for child in children:
            meta = child["metadata"]
            default = (
                child["kind"] == "ServiceAccount"
                and meta.get("name") == "default"
                and not child.get("secrets")
            )
            if child["kind"] == "ConfigMap" and meta.get("name") == "kube-root-ca.crt":
                import hashlib

                default = (
                    hashlib.sha256(
                        child.get("data", {}).get("ca.crt", "").encode()
                    ).hexdigest()
                    == receipt.ca_sha256
                )
            related = child.get("involvedObject") or child.get("regarding") or {}
            reporter = (
                child.get("reportingComponent")
                or child.get("reportingController")
                or child.get("source", {}).get("component")
            )
            # Kubernetes retains these short-lived records after their Pods/RS
            # disappear, including across a resumed cleanup. Durable foreign
            # objects still block namespace deletion independently.
            system_event = (
                related.get("namespace") == namespace
                and related.get("kind")
                in (
                    "Pod",
                    "ReplicaSet",
                    "Deployment",
                    "Service",
                    "Endpoints",
                    "EndpointSlice",
                )
                and reporter
                in (
                    "kubelet",
                    "default-scheduler",
                    "deployment-controller",
                    "replicaset-controller",
                    "endpoint-controller",
                    "endpointslice-controller",
                )
            )
            event = child["kind"] == "Event" and (
                related.get("uid") in known or system_event
            )
            automatic_endpoints = _automatic_endpoints(child, children, known)
            if (
                meta.get("uid") not in known
                and not default
                and not event
                and not automatic_endpoints
            ):
                raise RuntimeError("A foreign namespace child blocks cleanup.")


def _check_crd_instances(connection, receipt):
    allowed = {r.uid for r in receipt.inventory if r.kind == "ModelDeployment"}
    for record in receipt.inventory:
        if record.kind != "CustomResourceDefinition" or record.ownership != "created":
            continue
        crd = connection.kube.get(record.document())
        if crd is not None and any(
            d.get("metadata", {}).get("uid") not in allowed
            for d in connection.kube.instances(crd)
        ):
            raise RuntimeError("A foreign custom resource blocks CRD cleanup.")


def remove_runtime(connection, receipt):
    if (
        connection.purpose != "cleanup"
        or connection.target != receipt.target
        or connection.receipt.release_id != receipt.release_id
    ):
        raise RuntimeError("A matching cleanup-purpose connection is required.")
    _check_inventory(connection, receipt)
    _check_children(connection, receipt)
    _check_crd_instances(connection, receipt)
    for record in reversed(receipt.inventory):
        if record.ownership == "borrowed":
            continue
        connection.check_mutation()

        def before_delete():
            if record.kind == "Namespace":
                _check_children(connection, receipt)
            if record.kind == "CustomResourceDefinition":
                _check_crd_instances(connection, receipt)

        connection.kube.delete(record, before_delete=before_delete)
    persist(connection, replace(receipt, phase="removed"))
    return {"removed": True, "kind": receipt.kind, "release_id": receipt.release_id}


def verify_runtime(connection, receipt, *, monotonic=time.monotonic, wait=time.sleep):
    from fastapi import HTTPException

    from scripts.monitoring.observations import MonitoringError, require_snapshot
    from scripts.optimization.execution import (
        RolloutFailed,
        Superseded,
        verify_revision,
    )

    if connection.purpose != "operate" or receipt.phase != "installed":
        raise RuntimeError("Only an installed runtime can be verified.")
    _check_inventory(connection, receipt, require_present=True)
    bundle = render_runtime(receipt.target)
    for doc in (*bundle.documents, *bundle.metrics_documents):
        if doc["kind"] == "Deployment":
            current = connection.kube.get(doc)
            if (
                not current
                or not deployment_ready(current)
                or not _configuration_matches(current, doc)
            ):
                raise RuntimeError("The exact runtime deployments are not ready.")
    model = next(r for r in receipt.inventory if r.kind == "ModelDeployment")
    try:
        resource = require_snapshot(
            connection.cluster, expected_uid=model.uid, target=receipt.target
        )
        result = verify_revision(
            connection.cluster,
            resource,
            monotonic=monotonic,
            wait=wait,
            runtime=receipt.target,
        )
    except (MonitoringError, RolloutFailed, Superseded, HTTPException):
        raise RuntimeError(
            "The exact owned Iris revision, pods, endpoints and prediction did not verify."
        ) from None
    return {"kind": receipt.kind, "ready": True, **result}


class RuntimeObjects:
    """Bounded SDK CRUD; every deletion includes its captured UID precondition."""

    def __init__(self, sdk, *, before_mutation=lambda: None):
        self.sdk, self.dynamic = sdk, dynamic.DynamicClient(sdk)
        self.before_mutation = before_mutation

    def _resource(self, doc):
        return self.dynamic.resources.get(
            api_version=doc["apiVersion"], kind=doc["kind"]
        )

    def get(self, doc):
        try:
            api = self._resource(doc)
            meta = doc["metadata"]
            return api.get(
                name=meta["name"], namespace=meta.get("namespace"), _request_timeout=5
            ).to_dict()
        except ResourceNotFoundError:
            return None
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def create(self, doc, *, dry_run=False):
        resource = self._resource(doc)
        if not dry_run:
            self.before_mutation()
        return resource.create(
            body=doc,
            namespace=doc["metadata"].get("namespace"),
            dry_run="All" if dry_run else None,
            _request_timeout=5,
        ).to_dict()

    def wait_crd(self, doc):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            value = self.get(doc)
            if any(
                c.get("type") == "Established" and c.get("status") == "True"
                for c in value.get("status", {}).get("conditions", [])
            ):
                self.dynamic.resources.invalidate_cache()
                return
            time.sleep(1)
        raise RuntimeError("The new CRD did not become Established.")

    def delete(self, record, *, before_delete=lambda: None):
        doc = record.document()
        current = self.get(doc)
        if current is None:
            if record.kind == "CustomResourceDefinition":
                self.dynamic.resources.invalidate_cache()
            return
        if current["metadata"].get("uid") != record.uid:
            raise RuntimeError("Deletion UID changed.")
        resource = self._resource(doc)
        self.before_mutation()
        before_delete()
        resource.delete(
            name=record.name,
            namespace=record.namespace or None,
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": record.uid},
                "propagationPolicy": "Foreground",
            },
            _request_timeout=5,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = self.get(doc)
            if current is None:
                if record.kind == "CustomResourceDefinition":
                    self.dynamic.resources.invalidate_cache()
                return
            if current["metadata"].get("uid") != record.uid:
                raise RuntimeError("An object was recreated during cleanup.")
            time.sleep(1)
        raise RuntimeError("Owned object deletion did not complete.")

    def available_capacity(self):
        core = client.CoreV1Api(self.sdk)
        nodes = self.sdk.sanitize_for_serialization(core.list_node(_request_timeout=5))[
            "items"
        ]
        ready = [
            n
            for n in nodes
            if not n["spec"].get("unschedulable")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in n["status"].get("conditions", [])
            )
        ]
        if len(ready) != 1:
            raise RuntimeError("Exactly one ready schedulable worker is required.")
        name = ready[0]["metadata"]["name"]
        alloc = ready[0]["status"]["allocatable"]
        cpu, memory = (
            float(parse_quantity(alloc["cpu"])) * 1000,
            float(parse_quantity(alloc["memory"])) / 2**20,
        )
        pods = self.sdk.sanitize_for_serialization(
            core.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={name}", _request_timeout=5
            )
        )["items"]
        for pod in pods:
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            spec = pod["spec"]
            for field in ("cpu", "memory"):
                value = sum(
                    parse_quantity(
                        c.get("resources", {}).get("requests", {}).get(field, "0")
                    )
                    for c in spec["containers"]
                )
                value = max(
                    value,
                    max(
                        (
                            parse_quantity(
                                c.get("resources", {})
                                .get("requests", {})
                                .get(field, "0")
                            )
                            for c in spec.get("initContainers", [])
                        ),
                        default=0,
                    ),
                )
                value += parse_quantity(spec.get("overhead", {}).get(field, "0"))
                if field == "cpu":
                    cpu -= float(value) * 1000
                else:
                    memory -= float(value) / 2**20
        return cpu, memory

    def instances(self, crd):
        spec = crd["spec"]
        version = next(v["name"] for v in spec["versions"] if v["served"])
        resource = self.dynamic.resources.get(
            api_version=f"{spec['group']}/{version}", kind=spec["names"]["kind"]
        )
        return resource.get(_request_timeout=5).to_dict().get("items", [])

    def children(self, namespace):
        # The SDK persists discovery across processes, including removed CRDs.
        # Refresh before the initial guard as well as resumed namespace checks.
        self.dynamic.resources.invalidate_cache()
        children = []
        for api in self.dynamic.resources.search(namespaced=True):
            if (
                isinstance(api, ResourceList)
                or "/" in api.name
                or "list" not in api.verbs
                or api.group == "metrics.k8s.io"
            ):
                continue
            children.extend(
                api.get(namespace=namespace, _request_timeout=5)
                .to_dict()
                .get("items", [])
            )
        return children
