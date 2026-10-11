"""Official-client boundary; all requests stay within the supplied namespace."""

from kubernetes import client
from kubernetes.client.exceptions import ApiException

GROUP, VERSION, PLURAL = "mlops.strawhats.dev", "v1alpha1", "modeldeployments"
TIMEOUT = (3, 10)


class KubernetesAPI:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.serializer = client.ApiClient()
        self.apps = client.AppsV1Api(self.serializer)
        self.core = client.CoreV1Api(self.serializer)
        self.discovery = client.DiscoveryV1Api(self.serializer)
        self.custom = client.CustomObjectsApi(self.serializer)

    def _get(self, method, name):
        try:
            return self.serializer.sanitize_for_serialization(
                method(name, self.namespace, _request_timeout=TIMEOUT)
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def get_deployment(self, name: str) -> dict | None:
        return self._get(self.apps.read_namespaced_deployment, name)

    def get_service(self, name: str) -> dict | None:
        return self._get(self.core.read_namespaced_service, name)

    def _list(self, method, selector):
        result = method(
            self.namespace, label_selector=selector, _request_timeout=TIMEOUT
        )
        return self.serializer.sanitize_for_serialization(result)["items"]

    def list_replica_sets(self, selector: str) -> list[dict]:
        return self._list(self.apps.list_namespaced_replica_set, selector)

    def list_pods(self, selector: str) -> list[dict]:
        return self._list(self.core.list_namespaced_pod, selector)

    def list_endpoint_slices(self, service: str) -> list[dict]:
        return self._list(
            self.discovery.list_namespaced_endpoint_slice,
            f"kubernetes.io/service-name={service}",
        )

    def create_deployment(self, body: dict) -> None:
        self.apps.create_namespaced_deployment(
            self.namespace, body, _request_timeout=TIMEOUT
        )

    def patch_deployment(self, name: str, body: dict) -> None:
        self.apps.patch_namespaced_deployment(
            name, self.namespace, body, _request_timeout=TIMEOUT
        )

    def create_service(self, body: dict) -> None:
        self.core.create_namespaced_service(
            self.namespace, body, _request_timeout=TIMEOUT
        )

    def patch_service(self, name: str, body: dict) -> None:
        self.core.patch_namespaced_service(
            name, self.namespace, body, _request_timeout=TIMEOUT
        )

    def list_resources(self) -> list[dict]:
        return self.custom.list_namespaced_custom_object(
            GROUP, VERSION, self.namespace, PLURAL, _request_timeout=TIMEOUT
        )["items"]

    def patch_status(self, name: str, resource_version: str, status: dict) -> None:
        self.custom.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            self.namespace,
            PLURAL,
            name,
            body={"metadata": {"resourceVersion": resource_version}, "status": status},
            _request_timeout=TIMEOUT,
        )
