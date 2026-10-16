"""Verified Docker Desktop rehearsal, separate from both local defaults and EKS."""

import base64
import hashlib
import shutil
from contextlib import contextmanager
from pathlib import Path

import yaml
from fastapi import HTTPException

from scripts.cloud.aws_commands import run_checked
from scripts.cloud.images import inspect_local
from scripts.cloud.runtime_contract import RuntimeReceipt
from scripts.cloud.runtime_install import RuntimeObjects

from .iris_cluster import IrisCluster
from .local_cluster import connect_local_cluster
from .owner_target import (
    UNAVAILABLE,
    OwnerConnection,
    check_binding,
    check_inventory,
    check_phase,
    receipt_binding,
)

_run = run_checked


def _metadata(receipt):
    config = yaml.safe_load(Path(receipt.kubeconfig).read_text(encoding="utf-8-sig"))
    contexts = [
        c["context"] for c in config["contexts"] if c["name"] == "docker-desktop"
    ]
    if len(contexts) != 1:
        raise ValueError("One explicit Docker Desktop context is required.")
    context = contexts[0]
    clusters = [
        c["cluster"] for c in config["clusters"] if c["name"] == context["cluster"]
    ]
    users = [u["user"] for u in config["users"] if u["name"] == context["user"]]
    if (
        len(clusters) != 1
        or len(users) != 1
        or set(clusters[0]) != {"server", "certificate-authority-data"}
        or set(users[0]) != {"client-certificate-data", "client-key-data"}
        or clusters[0]["server"] != receipt.endpoint
    ):
        raise ValueError(
            "Rehearsal requires existing verified local certificate authentication."
        )
    ca = base64.b64decode(clusters[0]["certificate-authority-data"], validate=True)
    if hashlib.sha256(ca).hexdigest() != receipt.ca_sha256:
        raise ValueError("The local CA differs from this rehearsal.")


def _images(receipt):
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if not executable:
        raise ValueError("Local Docker CLI is required for rehearsal identity checks.")

    def run(argv, **kwargs):
        return _run([executable, *argv[1:]], **kwargs)

    for reference, identity in (
        (receipt.images.model_reference, receipt.images.model_image_id),
        (receipt.images.operator_reference, receipt.images.operator_image_id),
    ):
        if inspect_local(reference, run) != identity:
            raise ValueError("The locally built runtime image changed.")


@contextmanager
def connect_rehearsal(receipt_path: Path, *, purpose="operate"):
    try:
        receipt_path = Path(receipt_path)
        receipt = RuntimeReceipt.load(receipt_path)
        check_phase(receipt, purpose)
        if receipt.kind != "rehearsal":
            raise ValueError("Only local rehearsal receipts are supported.")
        _metadata(receipt)
        binding = receipt_binding(receipt)
        _images(receipt)
        with connect_local_cluster(
            kubeconfig=receipt.kubeconfig, zero_retries=True
        ) as local:
            sdk = local.sdk
            if (
                sdk.configuration.host != receipt.endpoint
                or not sdk.configuration.verify_ssl
                or sdk.configuration.proxy
                or hashlib.sha256(
                    Path(sdk.configuration.ssl_ca_cert).read_bytes()
                ).hexdigest()
                != receipt.ca_sha256
            ):
                raise ValueError(
                    "Rehearsal transport differs from the verified local endpoint."
                )
            connection = OwnerConnection(
                receipt.target,
                None,
                receipt,
                receipt_path,
                purpose,
                RuntimeObjects(
                    sdk, before_mutation=lambda: connection.check_mutation()
                ),
                None,
            )

            def guard():
                current = check_binding(connection, binding)
                _metadata(current)
                _images(current)
                check_inventory(connection)
                check_binding(connection, binding)

            connection.guard = guard
            connection.cluster = IrisCluster(
                sdk,
                local.custom,
                local.apps,
                target=receipt.target,
                before_mutation=connection.check_mutation,
            )
            check_inventory(connection)
            check_binding(connection, binding)
            yield connection
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, UNAVAILABLE) from None
