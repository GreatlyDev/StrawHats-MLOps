"""Single-replica in-cluster polling controller."""

import logging
import os
import signal
from threading import Event

from kubernetes import config

from .kube import KubernetesAPI
from .reconcile import reconcile

LOG = logging.getLogger(__name__)


def run_once(api: KubernetesAPI) -> None:
    for resource in api.list_resources():
        try:
            reconcile(resource, api)
        except Exception:
            LOG.exception(
                "Reconciliation deferred for %s", resource["metadata"]["name"]
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    namespace = os.environ["OPERATOR_NAMESPACE"]
    config.load_incluster_config()
    api = KubernetesAPI(namespace)
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    LOG.info("Reconciling ModelDeployments in namespace %s", namespace)
    while not stop.is_set():
        try:
            run_once(api)
        except Exception:
            LOG.exception("Cluster read failed; retrying")
        stop.wait(2)


if __name__ == "__main__":
    main()
