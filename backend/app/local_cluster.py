"""Explicit Docker Desktop transport, limited to one existing namespace/resource."""

from contextlib import contextmanager
from urllib.parse import urlsplit

from fastapi import HTTPException

CONTEXT = "docker-desktop"
NAMESPACE = "strawhats-operator-local"
NAME = "iris"
IMAGE = "strawhats-iris:local-monitoring"
GROUP, VERSION, PLURAL = "mlops.strawhats.dev", "v1alpha1", "modeldeployments"
TIMEOUT = (3, 5)


class LocalCluster:
    def __init__(self, sdk, custom, apps):
        self.sdk, self.custom, self.apps = sdk, custom, apps

    def resource(self, *, request_timeout=TIMEOUT):
        from kubernetes.client.exceptions import ApiException

        try:
            return self.custom.get_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                PLURAL,
                NAME,
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
                    "strawhats-model-operator",
                    NAMESPACE,
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
        if resource is None:
            return self.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                PLURAL,
                body={
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ModelDeployment",
                    "metadata": {"name": NAME, "namespace": NAMESPACE},
                    "spec": spec,
                },
                _request_timeout=TIMEOUT,
            )
        if resource["metadata"].get("deletionTimestamp"):
            raise HTTPException(409, "Local Iris is being deleted. Refresh and retry.")
        if all(resource["spec"].get(key) == value for key, value in spec.items()):
            return resource
        return self.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            PLURAL,
            NAME,
            body={
                "metadata": {
                    "resourceVersion": resource["metadata"]["resourceVersion"]
                },
                "spec": spec,
            },
            _request_timeout=TIMEOUT,
        )


@contextmanager
def connect_local_cluster():
    # Disabled API instances do not need to import the optional Kubernetes SDK.
    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException
        from kubernetes.config.config_exception import ConfigException
        from urllib3.exceptions import HTTPError
        from urllib3.util.retry import Retry
    except ImportError:
        raise HTTPException(
            503, "Install backend/requirements-operator.txt to use local Iris controls."
        ) from None
    sdk = None
    try:
        configuration = client.Configuration()
        configuration.proxy = None  # Local cluster traffic must never use a proxy.
        # Refuse redirects before any connection pools cache a retry policy.
        configuration.retries = Retry(total=3, redirect=0, raise_on_redirect=False)
        sdk = config.new_client_from_config(
            context=CONTEXT,
            client_configuration=configuration,
            persist_config=False,
        )
        address = urlsplit(sdk.configuration.host)
        if (
            address.scheme != "https"
            or address.hostname
            not in ("localhost", "127.0.0.1", "::1", "kubernetes.docker.internal")
            or address.username
            or address.password
            or not sdk.configuration.verify_ssl
            or sdk.configuration.proxy
        ):
            raise HTTPException(
                503,
                "The docker-desktop context must use a verified local HTTPS endpoint without a proxy.",
            )
        yield LocalCluster(sdk, client.CustomObjectsApi(sdk), client.AppsV1Api(sdk))
    except ApiException as error:
        if error.status in (401, 403):
            raise HTTPException(403, "Local Kubernetes access was denied.") from None
        if error.status == 409:
            raise HTTPException(
                409, "Iris changed while this request was running. Refresh and retry."
            ) from None
        if error.status in (400, 422):
            raise HTTPException(
                422, "Kubernetes rejected the local Iris configuration."
            ) from None
        raise HTTPException(
            503,
            "Local Kubernetes is unavailable. Check Docker Desktop and the operator setup.",
        ) from None
    except (ConfigException, HTTPError, OSError):
        raise HTTPException(
            503,
            "Cannot reach docker-desktop. Start local Kubernetes and check its context.",
        ) from None
    finally:
        if sdk is not None:
            sdk.close()
