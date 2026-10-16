"""Explicit owner-bound EKS transport; kubeconfig plugins are never executed."""

import base64
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import yaml
from fastapi import HTTPException
from kubernetes import client
from urllib3.util.retry import Retry

from scripts.cloud.aws_commands import run_checked, validate_creation_window
from scripts.cloud.cli import executable
from scripts.cloud.deploy import Cloud
from scripts.cloud.runtime_contract import RuntimeReceipt
from scripts.cloud.runtime_install import RuntimeObjects

from .iris_cluster import IrisCluster
from .owner_target import (
    UNAVAILABLE,
    OwnerConnection,
    check_binding,
    check_inventory,
    check_phase,
    receipt_binding,
)

_run = run_checked


def _aws_cli():
    return executable("aws")


def _validate_environment(profile):
    denied = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CA_BUNDLE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
    }
    if any(
        value
        and (
            key.upper() in denied
            or key.upper().startswith(("AWS_ENDPOINT_URL", "AWS_CONTAINER_"))
        )
        for key, value in os.environ.items()
    ):
        raise ValueError(
            "Use the named profile without credential or transport overrides."
        )
    if any(
        os.environ.get(key) not in (None, "", profile)
        for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")
    ):
        raise ValueError("Environment profile does not match the owner profile.")


def _runner(profile):
    def run(argv, **kwargs):
        _validate_environment(profile)
        if argv[0] != "aws":
            raise ValueError("Only the controlled AWS CLI boundary is permitted.")
        return _run([_aws_cli(), *argv[1:]], **kwargs)

    return run


def kube_metadata(receipt):
    address = urlsplit(receipt.endpoint)
    if (
        address.scheme != "https"
        or not address.hostname
        or not address.hostname.endswith(".us-east-1.eks.amazonaws.com")
        or address.port not in (None, 443)
        or address.username
        or address.password
        or address.path not in ("", "/")
        or address.query
        or address.fragment
    ):
        raise ValueError("A verified AWS HTTPS endpoint is required.")
    value = yaml.safe_load(Path(receipt.kubeconfig).read_text(encoding="utf-8-sig"))
    expected = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": receipt.target.context,
        "clusters": [
            {
                "name": receipt.target.context,
                "cluster": {
                    "server": receipt.endpoint,
                    "certificate-authority-data": None,
                },
            }
        ],
        "contexts": [
            {
                "name": receipt.target.context,
                "context": {
                    "cluster": receipt.target.context,
                    "user": receipt.target.context,
                },
            }
        ],
        "users": [
            {
                "name": receipt.target.context,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "aws",
                        "args": [
                            "--region",
                            "us-east-1",
                            "eks",
                            "get-token",
                            "--cluster-name",
                            "strawhats-midterm",
                            "--profile",
                            receipt.profile,
                        ],
                    }
                },
            }
        ],
    }
    helper = value["users"][0]["user"]["exec"]["command"]
    if helper not in ("aws", "aws.exe"):
        raise ValueError("Only the controlled AWS CLI helper is permitted.")
    expected["users"][0]["user"]["exec"]["command"] = helper
    encoded = value["clusters"][0]["cluster"]["certificate-authority-data"]
    expected["clusters"][0]["cluster"]["certificate-authority-data"] = encoded
    if value != expected:
        raise ValueError("Unexpected kubeconfig transport or credential plugin.")
    ca = base64.b64decode(encoded, validate=True)
    if not ca or len(ca) > 65536 or hashlib.sha256(ca).hexdigest() != receipt.ca_sha256:
        raise ValueError("Kubeconfig CA does not match the release.")
    return ca


def _verify_cloud(receipt, profile, account, purpose):
    _validate_environment(profile)
    if (
        receipt.kind != "eks"
        or receipt.profile != profile
        or receipt.account != account
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile)
    ):
        raise ValueError("The account/profile does not match the EKS receipt.")
    cloud = Cloud(SimpleNamespace(profile=profile, account=account), _runner(profile))
    stack, outputs = cloud.stack(active=purpose != "cleanup")
    tags = {t["Key"]: t["Value"] for t in stack["Tags"]}
    owner = next(
        p["ParameterValue"]
        for p in stack["Parameters"]
        if p["ParameterKey"] == "OwnerPrincipalArn"
    )
    if (
        stack["StackId"] != receipt.stack_arn
        or outputs.get("ClusterArn") != receipt.target.context
        or owner != receipt.owner_arn
        or float(tags["BudgetUsd"]) != receipt.budget_usd
        or datetime.fromisoformat(tags["RuntimeEndAt"].replace("Z", "+00:00"))
        != datetime.fromisoformat(receipt.end_at.replace("Z", "+00:00"))
    ):
        raise ValueError("Owned stack or runtime approval changed.")
    cluster = cloud.aws("eks", "describe-cluster", "--name", "strawhats-midterm")[
        "cluster"
    ]
    ca = base64.b64decode(cluster["certificateAuthority"]["data"], validate=True)
    if (
        cluster.get("arn") != receipt.target.context
        or cluster.get("name") != "strawhats-midterm"
        or cluster.get("status") != "ACTIVE"
        or cluster.get("endpoint") != receipt.endpoint
        or hashlib.sha256(ca).hexdigest() != receipt.ca_sha256
    ):
        raise ValueError("EKS endpoint or CA differs from the approved release.")
    return cloud


def _token(cloud):
    value = cloud.aws("eks", "get-token", "--cluster-name", "strawhats-midterm")
    token = value["status"]["token"]
    end = datetime.fromisoformat(
        value["status"]["expirationTimestamp"].replace("Z", "+00:00")
    )
    if (
        value.get("apiVersion") != "client.authentication.k8s.io/v1beta1"
        or value.get("kind") != "ExecCredential"
        or not re.fullmatch(r"k8s-aws-v1\.[A-Za-z0-9_-]{1,8192}", token)
        or not timedelta(seconds=15)
        < end - datetime.now(timezone.utc)
        <= timedelta(minutes=16)
    ):
        raise ValueError("A current bounded EKS token is required.")
    return token, end


def _window(receipt, purpose):
    if purpose != "cleanup":
        validate_creation_window(
            receipt.budget_usd,
            datetime.fromisoformat(receipt.end_at.replace("Z", "+00:00")),
            datetime.now(timezone.utc),
        )


@contextmanager
def connect_eks(
    receipt_path: Path, *, profile: str, expected_account: str, purpose="operate"
):
    sdk = None
    try:
        receipt_path = Path(receipt_path)
        receipt = RuntimeReceipt.load(receipt_path)
        check_phase(receipt, purpose)
        if receipt.kind != "eks":
            raise ValueError("Rehearsal receipts cannot open EKS.")
        ca = kube_metadata(receipt)
        binding = receipt_binding(receipt)
        cloud = _verify_cloud(receipt, profile, expected_account, purpose)
        token, token_end = _token(cloud)
        with tempfile.TemporaryDirectory(
            prefix="verified-ca-", dir=receipt_path.parent
        ) as folder:
            certificate = Path(folder) / "ca.pem"
            certificate.write_bytes(ca)
            configuration = client.Configuration()
            configuration.host = receipt.endpoint
            configuration.verify_ssl = True
            configuration.ssl_ca_cert = str(certificate)
            configuration.proxy = None
            configuration.retries = Retry(total=0, redirect=0, raise_on_redirect=False)
            configuration.api_key = {"authorization": token}
            configuration.api_key_prefix = {"authorization": "Bearer"}
            configuration.refresh_api_key_hook = None
            sdk = client.ApiClient(configuration=configuration)
            objects = RuntimeObjects(
                sdk, before_mutation=lambda: connection.check_mutation()
            )
            connection = OwnerConnection(
                receipt.target, None, receipt, receipt_path, purpose, objects, None
            )

            def guard():
                current = check_binding(connection, binding)
                kube_metadata(current)
                current_cloud = _verify_cloud(
                    current, profile, expected_account, purpose
                )
                refreshed, expires = _token(current_cloud)
                sdk.configuration.api_key["authorization"] = refreshed
                check_inventory(connection)
                # Credential helpers and slow reads all finish before the last check.
                _verify_cloud(current, profile, expected_account, purpose)
                check_binding(connection, binding)
                kube_metadata(current)
                if datetime.now(timezone.utc) + timedelta(seconds=10) >= expires:
                    raise ValueError("Token expired during binding checks.")
                _window(current, purpose)

            connection.guard = guard
            connection.cluster = IrisCluster(
                sdk,
                client.CustomObjectsApi(sdk),
                client.AppsV1Api(sdk),
                target=receipt.target,
                before_mutation=connection.check_mutation,
            )
            check_inventory(connection)
            check_binding(connection, binding)
            # Token acquisition may have waited or changed the active profile.
            _verify_cloud(receipt, profile, expected_account, purpose)
            kube_metadata(receipt)
            if datetime.now(timezone.utc) + timedelta(seconds=10) >= token_end:
                raise ValueError("Token expired during initial binding checks.")
            _window(receipt, purpose)
            yield connection
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, UNAVAILABLE) from None
    finally:
        if sdk is not None:
            sdk.close()
