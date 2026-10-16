"""Explicit Docker Desktop transport, limited to one existing namespace/resource."""

from contextlib import contextmanager
from urllib.parse import urlsplit

from fastapi import HTTPException

from .iris_cluster import GROUP, PLURAL, TIMEOUT, VERSION, IrisCluster  # noqa: F401
from .iris_target import LOCAL_TARGET

CONTEXT = LOCAL_TARGET.context
NAMESPACE = LOCAL_TARGET.namespace
NAME = LOCAL_TARGET.name
IMAGE = LOCAL_TARGET.image


class LocalCluster(IrisCluster):
    def __init__(self, sdk, custom, apps):
        super().__init__(
            sdk, custom, apps, target=LOCAL_TARGET, before_mutation=lambda: None
        )


@contextmanager
def connect_local_cluster(*, kubeconfig=None, kubeconfig_data=None, zero_retries=False):
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
        configuration.retries = Retry(
            total=0 if zero_retries else 3, redirect=0, raise_on_redirect=False
        )
        if kubeconfig is not None and kubeconfig_data is not None:
            raise ValueError("Select one Kubernetes configuration source.")
        loader = (
            config.new_client_from_config_dict
            if kubeconfig_data is not None
            else config.new_client_from_config
        )
        sdk = loader(
            context=CONTEXT,
            client_configuration=configuration,
            persist_config=False,
            **(
                {"config_dict": kubeconfig_data}
                if kubeconfig_data is not None
                else {"config_file": kubeconfig}
                if kubeconfig is not None
                else {}
            ),
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
