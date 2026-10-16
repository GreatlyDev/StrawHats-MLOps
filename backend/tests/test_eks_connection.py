"""Owner bindings use isolated SDK configuration and controlled AWS responses."""

import base64
import hashlib
import importlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from fastapi import HTTPException
from kubernetes import client

from backend.app.iris_target import RuntimeTarget
from backend.tests.test_cloud_runtime import connection as connection
from backend.tests.test_cloud_runtime import rehearsal as rehearsal
from backend.tests.test_cloud_runtime import runtime as runtime
from backend.tests.test_cloud_runtime import targets as targets
from scripts.cloud.images import ReleaseImage, ReleaseImages
from scripts.cloud.runtime_contract import ObjectRecord, RuntimeReceipt, fingerprint
from scripts.cloud.runtime_manifests import document_key, render_runtime

ACCOUNT = "123456789012"
OWNER = f"arn:aws:iam::{ACCOUNT}:user/owner"
CONTEXT = f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/strawhats-midterm"
ENDPOINT = "https://example.gr7.us-east-1.eks.amazonaws.com"
CA = base64.b64encode(b"public-test-ca").decode()
PROFILE = "strawhats"
TOKEN = "k8s-aws-v1.TEST_ONLY"


@pytest.fixture
def local_release(connection):
    return connection


@pytest.fixture
def eks():
    try:
        return importlib.import_module("backend.app.eks_cluster")
    except ModuleNotFoundError:
        pytest.fail("Verified owner EKS connection is not implemented")


@pytest.fixture
def release(eks, monkeypatch, tmp_path):
    registry = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"

    def image(name, letter):
        return ReleaseImage(
            registry + name,
            registry + name + "@sha256:" + letter * 64,
            "sha256:" + letter * 64,
            "sha256:" + "c" * 64,
        )

    images = ReleaseImages(
        "a" * 40, ACCOUNT, 50, image("iris", "a"), image("operator", "b")
    )
    target = RuntimeTarget(
        "eks",
        CONTEXT,
        "strawhats",
        "strawhats-monitoring",
        "iris",
        images.model.image,
        "IfNotPresent",
        "strawhats-model-operator",
        images.operator.image,
    )
    bundle = render_runtime(target)
    docs = (*bundle.documents, *bundle.metrics_documents, *bundle.secret_references)
    inventory = tuple(
        ObjectRecord(*document_key(d), f"uid-{index}", "created", fingerprint(d))
        for index, d in enumerate(docs)
    )
    kubeconfig = tmp_path / "kubeconfig"
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": CONTEXT,
        "clusters": [
            {
                "name": CONTEXT,
                "cluster": {"server": ENDPOINT, "certificate-authority-data": CA},
            }
        ],
        "contexts": [
            {"name": CONTEXT, "context": {"cluster": CONTEXT, "user": CONTEXT}}
        ],
        "users": [
            {
                "name": CONTEXT,
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
                            PROFILE,
                        ],
                    }
                },
            }
        ],
    }
    kubeconfig.write_text(yaml.safe_dump(config))
    receipt = RuntimeReceipt(
        kind="eks",
        phase="installed",
        release_id=str(uuid4()),
        source_sha="a" * 40,
        target=target,
        images=images,
        kubeconfig=str(kubeconfig),
        endpoint=ENDPOINT,
        ca_sha256=hashlib.sha256(base64.b64decode(CA)).hexdigest(),
        inventory=inventory,
        account=ACCOUNT,
        profile=PROFILE,
        owner_arn=OWNER,
        stack_arn=f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/strawhats-midterm/id",
        budget_usd=12,
        end_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    )
    path = tmp_path / "receipt.json"
    receipt.save(path)
    state = SimpleNamespace(
        receipt=receipt,
        path=path,
        config=config,
        calls=[],
        tokens=0,
        writes=[],
        fault=None,
        sdk=None,
        after_token=None,
    )
    data = {
        document_key(d): {
            **deepcopy(d),
            "metadata": {**d["metadata"], "uid": record.uid, "resourceVersion": "1"},
        }
        for d, record in zip(docs, inventory)
    }

    class Objects:
        def __init__(self, sdk, **kwargs):
            state.sdk = sdk

        def get(self, doc):
            value = deepcopy(data.get(document_key(doc)))
            if value and (
                (
                    state.fault == "namespace_uid"
                    and doc["kind"] == "Namespace"
                    and doc["metadata"]["name"] == "strawhats"
                )
                or (
                    state.fault == "operator_uid"
                    and doc["kind"] == "Deployment"
                    and doc["metadata"]["name"] == "strawhats-model-operator"
                )
                or (state.fault == "model_uid" and doc["kind"] == "ModelDeployment")
            ):
                value["metadata"]["uid"] = "recreated"
            return value

    def run(argv, **kwargs):
        state.calls.append(argv)
        assert argv[0] == "aws"
        assert argv[argv.index("--profile") + 1] == PROFILE
        operation = argv[1:3]
        if operation == ["sts", "get-caller-identity"]:
            return json.dumps(
                {
                    "Account": "999999999999" if state.fault == "account" else ACCOUNT,
                    "Arn": f"arn:aws:iam::{ACCOUNT}:root"
                    if state.fault == "root"
                    else OWNER.replace("/owner", "/other")
                    if state.fault == "owner"
                    else OWNER,
                }
            )
        if operation == ["cloudformation", "describe-stacks"]:
            return json.dumps(
                {
                    "Stacks": [
                        {
                            "StackId": receipt.stack_arn
                            + ("-other" if state.fault == "stack_arn" else ""),
                            "StackStatus": "CREATE_COMPLETE",
                            "Tags": [
                                {
                                    "Key": "Project",
                                    "Value": "foreign"
                                    if state.fault == "tags"
                                    else "StrawHats-MLOps",
                                },
                                {"Key": "Milestone", "Value": "one"},
                                {"Key": "BudgetUsd", "Value": "12"},
                                {"Key": "RuntimeEndAt", "Value": state.receipt.end_at},
                            ],
                            "Parameters": [
                                {
                                    "ParameterKey": "OwnerPrincipalArn",
                                    "ParameterValue": OWNER,
                                }
                            ],
                            "Outputs": [
                                {"OutputKey": "ClusterArn", "OutputValue": CONTEXT}
                            ],
                        }
                    ]
                }
            )
        if operation == ["eks", "describe-cluster"]:
            return json.dumps(
                {
                    "cluster": {
                        "arn": CONTEXT
                        + ("-other" if state.fault == "cluster_arn" else ""),
                        "name": "strawhats-midterm",
                        "status": "ACTIVE",
                        "endpoint": state.receipt.endpoint
                        + (".foreign" if state.fault == "endpoint" else ""),
                        "certificateAuthority": {
                            "data": base64.b64encode(b"wrong-ca").decode()
                            if state.fault == "ca"
                            else CA
                        },
                    }
                }
            )
        if operation == ["eks", "get-token"]:
            assert "--endpoint-url" not in argv
            assert argv[argv.index("--cluster-name") + 1] == "strawhats-midterm"
            state.tokens += 1
            if state.after_token:
                state.after_token()
            return json.dumps(
                {
                    "apiVersion": "client.authentication.k8s.io/v1beta1",
                    "kind": "ExecCredential",
                    "status": {
                        "token": TOKEN,
                        "expirationTimestamp": (
                            datetime.now(timezone.utc) + timedelta(minutes=14)
                        ).isoformat(),
                    },
                }
            )
        pytest.fail(f"Unexpected AWS operation {operation}")

    monkeypatch.setattr(eks, "_run", run)
    monkeypatch.setattr(eks, "_aws_cli", lambda: "aws")
    monkeypatch.setattr(eks, "RuntimeObjects", Objects)
    monkeypatch.setattr(
        client,
        "CustomObjectsApi",
        lambda sdk: SimpleNamespace(
            patch_namespaced_custom_object=lambda *a, **k: state.writes.append(
                k["body"]
            )
        ),
    )
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ENDPOINT_URL",
        "AWS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    state.data = data
    return state


def open_connection(eks, release, **kwargs):
    return eks.connect_eks(
        release.path, profile=PROFILE, expected_account=ACCOUNT, **kwargs
    )


def test_exact_windows_aws_helper_is_accepted_as_transport_metadata(eks, release):
    release.config["users"][0]["user"]["exec"]["command"] = "aws.exe"
    from pathlib import Path

    Path(release.receipt.kubeconfig).write_text(yaml.safe_dump(release.config))
    with open_connection(eks, release) as connection:
        assert connection.target.kind == "eks"
    assert release.tokens == 1


def test_verified_connection_is_isolated_and_uses_zero_write_retries(eks, release):
    before = fingerprint(
        {
            k: v
            for k, v in client.Configuration.get_default_copy().__dict__.items()
            if k
            in (
                "host",
                "api_key",
                "api_key_prefix",
                "verify_ssl",
                "ssl_ca_cert",
                "proxy",
            )
        }
    )
    original_config = release.receipt.kubeconfig
    with open_connection(eks, release) as connection:
        assert connection.target.context == CONTEXT
        assert connection.cluster.sdk.configuration.host == ENDPOINT
        assert connection.cluster.sdk.configuration.verify_ssl is True
        assert connection.cluster.sdk.configuration.proxy is None
        assert connection.cluster.sdk.configuration.retries.total == 0
        assert connection.cluster.sdk.configuration.retries.redirect == 0
        model = next(d for d in release.data.values() if d["kind"] == "ModelDeployment")
        connection.cluster.apply(model, {"replicas": 2})
    after = fingerprint(
        {
            k: v
            for k, v in client.Configuration.get_default_copy().__dict__.items()
            if k
            in (
                "host",
                "api_key",
                "api_key_prefix",
                "verify_ssl",
                "ssl_ca_cert",
                "proxy",
            )
        }
    )
    assert before == after and original_config == release.receipt.kubeconfig
    assert len(release.writes) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "root",
        "account",
        "owner",
        "tags",
        "stack_arn",
        "cluster_arn",
        "endpoint",
        "ca",
        "namespace_uid",
        "operator_uid",
        "model_uid",
    ],
)
def test_unverified_bindings_never_allow_a_write(eks, release, fault):
    release.fault = fault
    with pytest.raises(Exception) as error:
        with open_connection(eks, release):
            pytest.fail("An unverified connection was opened")
    assert TOKEN not in str(error.value) and release.writes == []


@pytest.mark.parametrize(
    "fault", ["command", "args", "env", "token_file", "tls", "proxy"]
)
def test_kubeconfig_plugins_and_transport_overrides_are_not_executed(
    eks, release, fault
):
    config = deepcopy(release.config)
    user = config["users"][0]["user"]
    if fault == "command":
        user["exec"]["command"] = "powershell"
    elif fault == "args":
        user["exec"]["args"] += ["--endpoint-url", "https://foreign"]
    elif fault == "env":
        user["exec"]["env"] = [{"name": "AWS_PROFILE", "value": "other"}]
    elif fault == "token_file":
        user["tokenFile"] = "/foreign"
    elif fault == "tls":
        config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    else:
        config["clusters"][0]["cluster"]["proxy-url"] = "https://foreign"
    from pathlib import Path

    Path(release.receipt.kubeconfig).write_text(yaml.safe_dump(config))
    with pytest.raises(Exception):
        with open_connection(eks, release):
            pytest.fail("Tampered kubeconfig was accepted")
    assert release.tokens == 0 and release.writes == []


def test_owner_change_after_token_wait_stops_next_write(eks, release):
    with open_connection(eks, release) as connection:
        release.after_token = lambda: setattr(release, "fault", "owner")
        with pytest.raises(Exception):
            connection.check_mutation()
    assert release.writes == []


@pytest.mark.parametrize("purpose", ["operate", "cleanup"])
def test_cleanup_remains_available_after_window_expiry(eks, release, purpose):
    expired = replace(
        release.receipt,
        end_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    expired.save(release.path)
    # The stack boundary must return the same expired approval record.
    release.receipt = expired
    if purpose == "operate":
        with pytest.raises(Exception):
            with open_connection(eks, release):
                pytest.fail("Expired runtime accepted")
    else:
        with open_connection(eks, release, purpose="cleanup") as connection:
            assert connection.purpose == "cleanup"
    assert release.writes == []


def test_changed_receipt_and_credential_environment_are_rejected(
    eks, release, monkeypatch
):
    with open_connection(eks, release) as connection:
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://foreign")
        with pytest.raises(Exception):
            connection.check_mutation()
    assert release.writes == []


def test_another_release_cannot_replace_the_open_binding(eks, release):
    with open_connection(eks, release) as connection:
        replace(release.receipt, release_id=str(uuid4())).save(release.path)
        with pytest.raises(Exception):
            connection.check_mutation()
    assert release.writes == []


@pytest.mark.parametrize("fault", [None, "image", "uid", "exec", "endpoint"])
def test_local_rehearsal_has_its_own_verified_binding(
    runtime, local_release, monkeypatch, tmp_path, fault
):
    try:
        module = importlib.import_module("backend.app.rehearsal_cluster")
    except ModuleNotFoundError:
        pytest.fail("Separate local rehearsal connection is not implemented")
    receipt = runtime.install.install_runtime(
        local_release, render_runtime(local_release.target), evidence_dir=tmp_path
    )
    certificate = tmp_path / "local-ca.pem"
    certificate.write_bytes(b"public-rehearsal-ca")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "docker-desktop",
        "clusters": [
            {
                "name": "docker-desktop",
                "cluster": {
                    "server": receipt.endpoint,
                    "certificate-authority-data": base64.b64encode(
                        certificate.read_bytes()
                    ).decode(),
                },
            }
        ],
        "contexts": [
            {
                "name": "docker-desktop",
                "context": {"cluster": "docker-desktop", "user": "docker-desktop"},
            }
        ],
        "users": [
            {
                "name": "docker-desktop",
                "user": {
                    "client-certificate-data": "Y2VydA==",
                    "client-key-data": "a2V5",
                },
            }
        ],
    }
    if fault == "exec":
        config["users"][0]["user"] = {"exec": {"command": "foreign"}}
    from pathlib import Path

    Path(receipt.kubeconfig).write_text(yaml.safe_dump(config))
    receipt = replace(
        receipt, ca_sha256=hashlib.sha256(certificate.read_bytes()).hexdigest()
    )
    receipt.save(local_release.receipt_path)
    sdk = SimpleNamespace(
        configuration=SimpleNamespace(
            host=receipt.endpoint + ("/foreign" if fault == "endpoint" else ""),
            verify_ssl=True,
            proxy=None,
            ssl_ca_cert=str(certificate),
        )
    )

    @contextmanager
    def connect(**kwargs):
        assert (
            kwargs["kubeconfig"] == receipt.kubeconfig
            and kwargs["zero_retries"] is True
        )
        yield SimpleNamespace(sdk=sdk, custom=None, apps=None)

    monkeypatch.setattr(module, "connect_local_cluster", connect)
    monkeypatch.setattr(
        module, "RuntimeObjects", lambda sdk, **kwargs: local_release.kube
    )

    def run(argv, **kwargs):
        assert "aws" not in argv[0]
        operator = "operator:" in argv[-1]
        identity = "3" if fault == "image" else "2" if operator else "1"
        return json.dumps(
            [{"Os": "linux", "Architecture": "amd64", "Id": "sha256:" + identity * 64}]
        )

    monkeypatch.setattr(module, "_run", run)
    if fault == "uid":
        doc = next(
            d for d in local_release.kube.data.values() if d["kind"] == "Namespace"
        )
        doc["metadata"]["uid"] = "recreated"
    if fault:
        with pytest.raises(HTTPException):
            with module.connect_rehearsal(local_release.receipt_path):
                pytest.fail("Unverified local rehearsal accepted")
    else:
        with module.connect_rehearsal(local_release.receipt_path) as connection:
            assert (
                connection.target.kind == "rehearsal"
                and connection.target.namespace == "strawhats"
            )
            connection.check_mutation()


def test_rehearsal_receipt_cannot_contact_aws(eks, local_release, monkeypatch):
    monkeypatch.setattr(
        eks, "_run", lambda *a, **k: pytest.fail("Rehearsal must never contact AWS")
    )
    local_release.receipt.save(local_release.receipt_path)
    with pytest.raises(HTTPException):
        with eks.connect_eks(
            local_release.receipt_path, profile=PROFILE, expected_account=ACCOUNT
        ):
            pytest.fail("A rehearsal receipt opened EKS")


def test_insecure_endpoint_is_refused_even_if_copied_into_both_records(eks, release):
    from pathlib import Path

    release.receipt = replace(
        release.receipt, endpoint="http://example.gr7.us-east-1.eks.amazonaws.com"
    )
    release.receipt.save(release.path)
    release.config["clusters"][0]["cluster"]["server"] = release.receipt.endpoint
    Path(release.receipt.kubeconfig).write_text(yaml.safe_dump(release.config))
    with pytest.raises(HTTPException):
        with open_connection(eks, release):
            pytest.fail("Insecure endpoint accepted")
    assert release.tokens == 0 and release.writes == []
