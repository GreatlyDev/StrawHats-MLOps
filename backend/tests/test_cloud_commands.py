"""Release guards run locally without credentials or cloud side effects."""

import json
import subprocess
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest

from scripts.cloud import deploy
from scripts.cloud.aws_commands import (
    require_eks_context,
    run_checked,
    validate_creation_window,
    validate_identity,
)
from scripts.cloud.deploy import dispatch, parser

ACCOUNT = "123456789012"
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
IDENTITY = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/owner"}


def test_approved_identity_context_and_window():
    assert validate_identity(IDENTITY, ACCOUNT) == IDENTITY["Arn"]
    require_eks_context(
        f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/strawhats-midterm",
        ACCOUNT,
        "us-east-1",
        "strawhats-midterm",
    )
    validate_creation_window(12, NOW + timedelta(hours=72), NOW)


@pytest.mark.parametrize(
    "identity",
    [
        {"Account": "999999999999", "Arn": IDENTITY["Arn"]},
        {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:root"},
        {"Account": ACCOUNT, "Arn": "malformed"},
    ],
)
def test_wrong_account_or_root_stops_before_mutation(identity):
    calls = []

    def run(argv, **kwargs):
        import json

        calls.append(argv)
        return json.dumps(identity)

    args = parser().parse_args(
        ["publish", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError):
        dispatch(args, run=run)
    assert len(calls) == 1
    assert calls[0][1:3] == ["sts", "get-caller-identity"]
    assert "--profile" in calls[0] and "--region" in calls[0]


@pytest.mark.parametrize(
    "context",
    [
        "docker-desktop",
        f"arn:aws:eks:us-east-2:{ACCOUNT}:cluster/strawhats-midterm",
        "arn:aws:eks:us-east-1:999999999999:cluster/strawhats-midterm",
    ],
)
def test_unrelated_context_is_rejected(context):
    with pytest.raises(RuntimeError):
        require_eks_context(context, ACCOUNT, "us-east-1", "strawhats-midterm")


@pytest.mark.parametrize(
    "budget,end",
    [
        (None, NOW + timedelta(hours=1)),
        (0, NOW + timedelta(hours=1)),
        (-1, NOW + timedelta(hours=1)),
        (float("nan"), NOW + timedelta(hours=1)),
        (5, None),
        (5, NOW),
        (5, NOW + timedelta(hours=73)),
        (5, NOW.replace(tzinfo=None) + timedelta(hours=1)),
    ],
)
def test_invalid_runtime_intent_is_rejected(budget, end):
    with pytest.raises(RuntimeError):
        validate_creation_window(budget, end, NOW)


def test_process_failure_does_not_expose_secret_stdin_or_output(monkeypatch):
    def fail(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["input"] == "secret-password"
        return subprocess.CompletedProcess(argv, 1, "secret-output", "secret-error")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError) as exc:
        run_checked(["docker", "login", "--password-stdin"], stdin="secret-password")
    assert "secret" not in str(exc.value)


def test_failed_command_halts_publish_before_docker_or_cloud_mutation():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise RuntimeError("STS failed")

    args = parser().parse_args(
        ["publish", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError, match="STS failed"):
        dispatch(args, run=run)
    assert len(calls) == 1


def test_creation_requires_reviewed_change_set_confirmation(tmp_path):
    args = parser().parse_args(
        [
            "create",
            "--profile",
            "strawhats",
            "--account",
            ACCOUNT,
            "--receipt",
            str(tmp_path / "missing.json"),
        ]
    )
    with pytest.raises(RuntimeError):
        dispatch(args, run=lambda *a, **k: pytest.fail("Must reject before AWS calls"))


def stack_output(*, expired=False):
    end = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 24)
    return {
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
                    {"Key": "BudgetUsd", "Value": "12"},
                ],
                "Outputs": [
                    {
                        "OutputKey": "RepositoryUri",
                        "OutputValue": f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/iris",
                    },
                    {
                        "OutputKey": "RepositoryName",
                        "OutputValue": "strawhats-midterm/iris",
                    },
                ],
            }
        ]
    }


def test_expired_window_blocks_image_publication():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if argv[1:3] == ["cloudformation", "describe-stacks"]:
            return json.dumps(stack_output(expired=True))
        pytest.fail("Expired release must not build or publish")

    args = parser().parse_args(
        ["publish", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError, match="budget|end"):
        dispatch(args, run=run)
    assert len(calls) == 2


def test_rebuilt_image_is_smoke_checked_before_ecr_login(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if argv[1:3] == ["cloudformation", "describe-stacks"]:
            return json.dumps(stack_output())
        if argv[:3] == ["git", "status", "--porcelain"]:
            return ""
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        if argv[:2] == ["docker", "build"]:
            return ""
        pytest.fail("An unverified rebuilt image must never reach ECR login/push")

    def reject(*a, **k):
        raise RuntimeError("Container smoke failed")

    monkeypatch.setattr(deploy, "verify_local_image", reject, raising=False)
    args = parser().parse_args(
        ["publish", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError, match="Container smoke failed"):
        dispatch(args, run=run)
    assert calls[-1][:2] == ["docker", "build"]


def test_failed_apply_never_awaits_or_claims_rollout(tmp_path):
    image = (
        f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/iris@sha256:"
        + "a" * 64
    )
    receipt = tmp_path / "image.json"
    receipt.write_text(json.dumps({"account": ACCOUNT, "image": image}))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if argv[1:3] == ["cloudformation", "describe-stacks"]:
            return json.dumps(stack_output())
        if argv[1:3] == ["ecr", "describe-images"]:
            return json.dumps({"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]})
        if argv[0] == "kubectl":
            assert argv[1:3] == [
                "--context",
                f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/strawhats-midterm",
            ]
            assert argv[3] == "apply"
            raise RuntimeError("Apply failed")
        pytest.fail("Unexpected command")

    args = parser().parse_args(
        [
            "apply",
            "--profile",
            "strawhats",
            "--account",
            ACCOUNT,
            "--image-receipt",
            str(receipt),
        ]
    )
    with pytest.raises(RuntimeError, match="Apply failed"):
        dispatch(args, run=run)
    assert calls[-1][3] == "apply"


def test_container_waits_for_http_after_port_opens(monkeypatch):
    calls = []
    http_attempts = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            return "b" * 64
        if argv[1] == "port":
            return "127.0.0.1:18001"
        assert argv == ["docker", "rm", "--force", "b" * 64]
        return ""

    class Response:
        status = 200

    def http(*a, **k):
        http_attempts.append(1)
        if len(http_attempts) == 1:
            raise URLError("Container's forwarding port opened before HTTP server")
        return nullcontext(Response())

    def smoke(url):
        assert len(http_attempts) == 2
        return {"verified": True}

    monkeypatch.setattr(deploy, "urlopen", http, raising=False)
    monkeypatch.setattr(
        deploy.socket, "create_connection", lambda *a, **k: nullcontext()
    )
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    monkeypatch.setattr(deploy, "verify_iris", smoke)
    assert deploy.verify_local_image("image:local", run) == {"verified": True}
    assert calls[-1] == ["docker", "rm", "--force", "b" * 64]


def test_free_plan_ineligible_worker_stops_preflight():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        operation = argv[1:3]
        if operation == ["sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if operation == ["eks", "describe-cluster-versions"]:
            return json.dumps(
                {"clusterVersions": [{"versionStatus": "STANDARD_SUPPORT"}]}
            )
        if operation == ["freetier", "get-account-plan-state"]:
            return json.dumps(
                {
                    "accountId": ACCOUNT,
                    "accountPlanStatus": "ACTIVE",
                    "accountPlanType": "FREE",
                }
            )
        if operation == ["ec2", "describe-instance-types"]:
            return json.dumps({"InstanceTypes": [{"FreeTierEligible": False}]})
        pytest.fail("No later operation or billing upgrade is allowed")

    args = parser().parse_args(
        ["preflight", "--profile", "strawhats", "--account", ACCOUNT]
    )
    with pytest.raises(RuntimeError, match="Free plan"):
        dispatch(args, run=run)
    assert len(calls) == 4


def test_cli_reports_safe_guard_reason(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["deploy", "create", "--profile", "strawhats", "--account", ACCOUNT]
    )
    monkeypatch.setattr(
        deploy,
        "dispatch",
        lambda _: (_ for _ in ()).throw(
            RuntimeError(
                "Review the actual change set and confirm its exact ARN before execution."
            )
        ),
    )
    with pytest.raises(SystemExit) as exc:
        deploy.main()
    assert exc.value.code == 1
    assert "confirm its exact ARN" in capsys.readouterr().err
