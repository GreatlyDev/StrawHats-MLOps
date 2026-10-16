"""Namespace-bound Iris SDK operations with a check before each mutation."""

from fastapi import HTTPException

from .iris_target import RuntimeTarget

GROUP, VERSION, PLURAL = "mlops.strawhats.dev", "v1alpha1", "modeldeployments"
TIMEOUT = (3, 5)


class IrisCluster:
    def __init__(self, sdk, custom, apps, *, target: RuntimeTarget, before_mutation):
        self.sdk, self.custom, self.apps = sdk, custom, apps
        self.target = target
        self.before_mutation = before_mutation

    def resource(self, *, request_timeout=TIMEOUT):
        from kubernetes.client.exceptions import ApiException

        try:
            return self.custom.get_namespaced_custom_object(
                GROUP,
                VERSION,
                self.target.namespace,
                PLURAL,
                self.target.name,
                _request_timeout=request_timeout,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def controller_ready(self, *, request_timeout=TIMEOUT) -> bool:
        from kubernetes.client.exceptions import ApiException

        try:
            deployment = self.sdk.sanitize_for_serialization(
                self.apps.read_namespaced_deployment(
                    self.target.operator_name,
                    self.target.namespace,
                    _request_timeout=request_timeout,
                )
            )
        except ApiException as error:
            if error.status == 404:
                return False
            raise
        status = deployment.get("status", {})
        return status.get("observedGeneration", 0) >= deployment["metadata"].get(
            "generation", 1
        ) and all(
            status.get(field, 0) >= 1
            for field in ("readyReplicas", "availableReplicas", "updatedReplicas")
        )

    def apply(self, resource, spec):
        if resource is not None and (
            resource.get("metadata", {}).get("namespace") != self.target.namespace
            or resource.get("metadata", {}).get("name") != self.target.name
        ):
            raise HTTPException(409, "Iris identity does not match this runtime.")
        if self.target.kind != "local" and (
            spec.get("image", self.target.image) != self.target.image
            or spec.get("imagePullPolicy", self.target.image_pull_policy)
            != self.target.image_pull_policy
        ):
            raise HTTPException(409, "Iris image does not match this runtime.")
        if resource is None:
            self.before_mutation()
            return self.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                self.target.namespace,
                PLURAL,
                body={
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ModelDeployment",
                    "metadata": {
                        "name": self.target.name,
                        "namespace": self.target.namespace,
                    },
                    "spec": spec,
                },
                _request_timeout=TIMEOUT,
            )
        if resource["metadata"].get("deletionTimestamp"):
            raise HTTPException(409, "Local Iris is being deleted. Refresh and retry.")
        if all(resource["spec"].get(key) == value for key, value in spec.items()):
            return resource
        self.before_mutation()
        return self.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.target.namespace,
            PLURAL,
            self.target.name,
            body={
                "metadata": {
                    "resourceVersion": resource["metadata"]["resourceVersion"],
                    "uid": resource["metadata"]["uid"],
                },
                "spec": spec,
            },
            _request_timeout=TIMEOUT,
        )
