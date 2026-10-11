"""Regression cases for final release-safety review findings."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from scripts.cloud import aws_commands, deploy
from scripts.cloud.deploy import Cloud, dispatch, parser

ACCOUNT = "123456789012"
IDENTITY = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/owner"}
REPOSITORY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/iris"
IMAGE = REPOSITORY + "@sha256:" + "a" * 64


def test_window_expiring_during_build_prevents_registry_push(monkeypatch, tmp_path):
    clock = [datetime.now(timezone.utc)]
    end = clock[0] + timedelta(hours=1)
    monkeypatch.setattr(deploy, "utc_now", lambda: clock[0], raising=False)
    monkeypatch.setattr(deploy, "LOCAL", tmp_path)
    monkeypatch.setattr(deploy, "verify_local_image", lambda *a: {"verified": True})
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if argv[1:3] == ["cloudformation", "describe-stacks"]:
            return json.dumps(
                {
                    "Stacks": [
                        {
                            "StackId": f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/strawhats-midterm/id",
                            "StackStatus": "CREATE_COMPLETE",
                            "Parameters": [
                                {
                                    "ParameterKey": "OwnerPrincipalArn",
                                    "ParameterValue": IDENTITY["Arn"],
                                }
                            ],
                            "Tags": [
                                {"Key": "Project", "Value": "StrawHats-MLOps"},
                                {"Key": "Milestone", "Value": "one"},
                                {"Key": "RuntimeEndAt", "Value": end.isoformat()},
                                {"Key": "BudgetUsd", "Value": "10"},
                            ],
                            "Outputs": [
                                {
                                    "OutputKey": "RepositoryUri",
                                    "OutputValue": REPOSITORY,
                                },
                                {
                                    "OutputKey": "RepositoryName",
                                    "OutputValue": "strawhats-midterm/iris",
                                },
                            ],
                        }
                    ]
                }
            )
        if argv[:3] == ["git", "status", "--porcelain"]:
            return ""
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        if argv[:2] == ["docker", "build"]:
            clock[0] = end + timedelta(seconds=1)
            return ""
        if argv[1:3] == ["ecr", "get-login-password"] or "login" in argv:
            return ""
        pytest.fail("Expired build must never reach push")

    args = parser().parse_args(
        ["publish", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError, match="future UTC end"):
        dispatch(args, run=run)
    assert not any("push" in argv for argv in calls)


@pytest.mark.parametrize("target", ["aws", "kubectl"])
def test_expiry_before_write_blocks_create_and_restart(monkeypatch, target):
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(deploy, "utc_now", lambda: clock[0], raising=False)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1:3] == ["sts", "get-caller-identity"]
        return json.dumps(IDENTITY)

    cloud = Cloud(
        parser().parse_args(
            ["preflight", "--profile", "strawhats", "--account", ACCOUNT]
        ),
        run,
    )
    cloud.set_window(10, clock[0] + timedelta(seconds=1))
    clock[0] += timedelta(seconds=2)
    with pytest.raises(RuntimeError, match="future UTC end"):
        if target == "aws":
            cloud.aws(
                "cloudformation", "execute-change-set", "--change-set-name", "known"
            )
        else:
            cloud.kubectl("-n", "strawhats", "rollout", "restart", "deployment/iris")
    assert len(calls) == 1


def test_failed_command_preserves_allowlisted_error_and_stack_identity(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        aws_commands, "DIAGNOSTICS", tmp_path / "failures.jsonl", raising=False
    )
    stack = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/strawhats-midterm/id"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv,
            255,
            "ignored output",
            "An error occurred (AccessDenied) when calling the ExecuteChangeSet operation: cannot execute",
        ),
    )
    with pytest.raises(RuntimeError):
        aws_commands.run_checked(
            ["aws", "cloudformation", "execute-change-set", "--stack-name", stack]
        )
    record = json.loads((tmp_path / "failures.jsonl").read_text())
    assert record["exit_code"] == 255
    assert record["aws_error_code"] == "AccessDenied"
    assert stack in record["resource_ids"]
    assert record["operation"] == "aws cloudformation execute-change-set"
    assert record["at_utc"]


def test_credential_failure_keeps_status_but_excludes_all_output(monkeypatch, tmp_path):
    destination = tmp_path / "failures.jsonl"
    monkeypatch.setattr(aws_commands, "DIAGNOSTICS", destination, raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "secret-output", "secret-error"
        ),
    )
    with pytest.raises(RuntimeError):
        aws_commands.run_checked(
            ["docker", "login", "--password-stdin"], stdin="secret-password"
        )
    data = destination.read_text()
    assert "secret" not in data
    assert json.loads(data)["exit_code"] == 1


def runtime_objects(*, image=IMAGE, ready=True, runtime_digest=None):
    runtime_digest = runtime_digest or "sha256:" + "b" * 64
    deployment = {
        "metadata": {"uid": "deployment-uid"},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "iris", "image": image}]}},
        },
    }
    replicas = {
        "items": [
            {
                "metadata": {
                    "uid": "rs-uid",
                    "ownerReferences": [
                        {
                            "uid": "deployment-uid",
                            "kind": "Deployment",
                            "controller": True,
                        }
                    ],
                }
            }
        ]
    }
    pods = {
        "items": [
            {
                "metadata": {
                    "uid": "pod-uid",
                    "name": "iris-abc",
                    "ownerReferences": [
                        {"uid": "rs-uid", "kind": "ReplicaSet", "controller": True}
                    ],
                },
                "spec": {"containers": [{"name": "iris", "image": image}]},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": "iris",
                            "ready": True,
                            "imageID": REPOSITORY + "@" + runtime_digest,
                        }
                    ],
                },
            }
        ]
    }
    endpoints = {
        "items": [
            {
                "endpoints": [
                    {
                        "conditions": {"ready": ready},
                        "targetRef": {"kind": "Pod", "uid": "pod-uid"},
                        "addresses": ["10.72.0.8"],
                    }
                ]
            }
        ]
    }
    return deployment, replicas, pods, endpoints


@pytest.mark.parametrize(
    "fault", ["old-image", "unready-endpoint", "no-endpoint", "wrong-runtime-id"]
)
def test_cloud_acceptance_rejects_wrong_release_or_endpoints(fault):
    receipt = {
        "image": IMAGE,
        "runtime_manifest_digest": "sha256:" + "b" * 64,
        "runtime_config_digest": "sha256:" + "c" * 64,
    }
    deployment, replicas, pods, endpoints = runtime_objects(
        image=REPOSITORY + "@sha256:" + "d" * 64 if fault == "old-image" else IMAGE,
        ready=fault != "unready-endpoint",
        runtime_digest="sha256:" + "d" * 64 if fault == "wrong-runtime-id" else None,
    )
    if fault == "no-endpoint":
        endpoints = {"items": []}
    with pytest.raises(RuntimeError):
        deploy.validate_runtime(deployment, replicas, pods, endpoints, receipt)


def test_cloud_acceptance_handles_index_and_platform_digest():
    receipt = {
        "image": IMAGE,
        "runtime_manifest_digest": "sha256:" + "b" * 64,
        "runtime_config_digest": "sha256:" + "c" * 64,
    }
    result = deploy.validate_runtime(*runtime_objects(), receipt)
    assert result["pod_uid"] == "pod-uid"
    assert result["ready_service_endpoint"] is True
    assert result["image_id"].endswith("b" * 64)


def test_registry_index_resolves_runtime_and_ignores_attestation():
    manifest = "sha256:" + "b" * 64
    config = "sha256:" + "c" * 64

    def inspect(reference):
        if reference == IMAGE:
            return json.dumps(
                {
                    "manifests": [
                        {
                            "digest": manifest,
                            "platform": {"os": "linux", "architecture": "amd64"},
                        },
                        {
                            "digest": "sha256:" + "d" * 64,
                            "platform": {"os": "unknown", "architecture": "unknown"},
                        },
                    ]
                }
            )
        assert reference == REPOSITORY + "@" + manifest
        return json.dumps({"config": {"digest": config}})

    assert deploy.resolve_runtime_digests(REPOSITORY, IMAGE.split("@")[1], inspect) == (
        manifest,
        config,
    )


def test_port_forward_failure_records_safe_kubectl_reason(monkeypatch, tmp_path):
    destination = tmp_path / "failures.jsonl"
    monkeypatch.setattr(aws_commands, "DIAGNOSTICS", destination)

    class FailedProcess:
        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, **kwargs):
            return 1

    def start(argv, **kwargs):
        assert kwargs["shell"] is False
        kwargs["stdout"].write("Error from server (Forbidden): cannot port-forward")
        kwargs["stdout"].flush()
        return FailedProcess()

    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(RuntimeError):
        with deploy.port_forward(
            [
                "kubectl",
                "--context",
                "expected",
                "port-forward",
                "pod/iris",
                "8001:8000",
            ]
        ):
            raise RuntimeError("Port-forward exited before verification")
    assert json.loads(destination.read_text())["reason"] == "Forbidden"
