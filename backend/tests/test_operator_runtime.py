from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from backend.model_operator.__main__ import run_once
from backend.model_operator.kube import KubernetesAPI


def test_adapter_preserves_wire_fields_and_namespaces():
    api = KubernetesAPI("strawhats-operator-local")
    api.apps = Mock()
    api.apps.read_namespaced_deployment.return_value = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="iris", resource_version="12"),
        status=client.V1DeploymentStatus(observed_generation=3, ready_replicas=1),
    )
    observed = api.get_deployment("iris")
    assert observed["metadata"]["resourceVersion"] == "12"
    assert observed["status"]["observedGeneration"] == 3
    api.apps.read_namespaced_deployment.assert_called_once_with(
        "iris", "strawhats-operator-local", _request_timeout=(3, 10)
    )


@pytest.mark.parametrize("code", [403, 409, 500])
def test_only_not_found_means_absent(code):
    api = KubernetesAPI("strawhats-operator-local")
    api.core = Mock()
    api.core.read_namespaced_service.side_effect = ApiException(status=code)
    with pytest.raises(ApiException):
        api.get_service("iris")
    api.core.read_namespaced_service.side_effect = ApiException(status=404)
    assert api.get_service("iris") is None


def test_status_patch_has_optimistic_version_precondition():
    api = KubernetesAPI("strawhats-operator-local")
    api.custom = Mock()
    api.patch_status("iris", "12", {"phase": "Pending"})
    body = api.custom.patch_namespaced_custom_object_status.call_args.kwargs["body"]
    assert body == {
        "metadata": {"resourceVersion": "12"},
        "status": {"phase": "Pending"},
    }


def test_one_failed_resource_does_not_stop_other_resources(monkeypatch):
    from backend.model_operator import __main__ as runtime

    calls = []

    def attempt(resource, api):
        calls.append(resource["metadata"]["name"])
        if len(calls) == 1:
            raise ApiException(status=409)

    monkeypatch.setattr(runtime, "reconcile", attempt)
    api = Mock()
    api.list_resources.return_value = [
        {"metadata": {"name": "first"}},
        {"metadata": {"name": "second"}},
    ]
    run_once(api)
    assert calls == ["first", "second"]
