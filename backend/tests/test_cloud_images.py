"""Exercise image publication and cleanup through controlled process responses."""

import importlib
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.tests.test_cloud_ci import SHA, boundary, ci_run
from backend.tests.test_cloud_commands import ACCOUNT, IDENTITY, stack_output
from scripts.cloud import deploy


@pytest.fixture
def images():
    try:
        return importlib.import_module("scripts.cloud.images")
    except ModuleNotFoundError:
        pytest.fail("Two-image runtime release is not implemented")


@pytest.fixture
def release(images, monkeypatch, tmp_path):
    state = SimpleNamespace(
        calls=[],
        pushes=[],
        changed=False,
        expired=False,
        build_count=0,
        change_after_build=False,
        expire_after_build=False,
        fail_second=False,
        index_identity=False,
    )
    repository = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    stack_payload = stack_output()

    def run(argv, **kwargs):
        state.calls.append(argv)
        if argv[:2] == ["gh", "api"]:
            return boundary(getattr(state, "runs", [ci_run()]))(argv, **kwargs)
        if argv[:3] == ["aws", "sts", "get-caller-identity"]:
            return json.dumps(IDENTITY)
        if argv[:3] == ["aws", "cloudformation", "describe-stacks"]:
            stack = (
                stack_output(expired=True) if state.expired else deepcopy(stack_payload)
            )
            stack["Stacks"][0]["Outputs"] += [
                {
                    "OutputKey": "OperatorRepositoryUri",
                    "OutputValue": repository + "operator",
                },
                {
                    "OutputKey": "OperatorRepositoryName",
                    "OutputValue": "strawhats-midterm/operator",
                },
            ]
            return json.dumps(stack)
        if argv[:3] == ["git", "status", "--porcelain"]:
            return ""
        if argv[:2] == ["git", "rev-parse"]:
            return "b" * 40 if state.changed else SHA
        if argv[:2] == ["docker", "build"]:
            state.build_count += 1
            state.changed = state.change_after_build
            state.expired = state.expire_after_build
            return ""
        if argv[:3] == ["docker", "image", "inspect"]:
            operator = "/operator:" in argv[-1]
            return json.dumps(
                [
                    {
                        "Id": "sha256:"
                        + (
                            ("b" if operator else "a")
                            if state.index_identity
                            else ("2" if operator else "1")
                        )
                        * 64,
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": {
                            "User": "10001",
                            "Cmd": ["python", "-m", "backend.model_operator"],
                            "Entrypoint": None,
                        },
                    }
                ]
            )
        if argv[:2] == ["docker", "run"]:
            assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
            exec(argv[-1], {})
            return ""
        if argv[:3] == ["aws", "ecr", "get-login-password"]:
            return "test-secret"
        if "login" in argv:
            assert kwargs["stdin"] == "test-secret"
            return ""
        if "push" in argv:
            if state.fail_second and state.pushes:
                raise RuntimeError("second push failed")
            state.pushes.append(argv[-1])
            return ""
        if argv[:3] == ["aws", "ecr", "describe-images"]:
            operator = argv[argv.index("--repository-name") + 1].endswith("operator")
            return json.dumps(
                {
                    "imageDetails": [
                        {"imageDigest": "sha256:" + ("b" if operator else "a") * 64}
                    ]
                }
            )
        if "imagetools" in argv:
            operator = "/operator@" in argv[-1]
            return json.dumps(
                {
                    "schemaVersion": 2,
                    "config": {"digest": "sha256:" + ("2" if operator else "1") * 64},
                }
            )
        pytest.fail(f"Unexpected release process boundary: {argv[:4]}")

    monkeypatch.setattr(images, "LOCAL", tmp_path)
    monkeypatch.setattr(
        deploy, "verify_local_image", lambda image, run: {"verified": True}
    )
    cloud = deploy.Cloud(SimpleNamespace(profile="strawhats", account=ACCOUNT), run)
    return state, cloud, tmp_path


def test_both_verified_digests_are_saved_as_one_release(images, release):
    state, cloud, folder = release
    result = images.publish_runtime(cloud)
    assert result.source_sha == SHA and result.ci_run_id == 50
    assert result.model.image.endswith("@sha256:" + "a" * 64)
    assert result.operator.runtime_config_digest == "sha256:" + "2" * 64
    assert len(state.pushes) == 2
    assert json.loads((folder / "images.json").read_text()) == asdict(result)
    assert "test-secret" not in "".join(p.read_text() for p in folder.rglob("*.json"))


def test_containerd_store_identity_can_be_the_published_manifest(images, release):
    state, cloud, folder = release
    state.index_identity = True
    result = images.publish_runtime(cloud)
    assert result.model.image.endswith("a" * 64)
    assert result.model.runtime_config_digest.endswith("1" * 64)


@pytest.mark.parametrize("condition", ["change_after_build", "expire_after_build"])
def test_source_or_window_change_during_build_blocks_publication(
    images, release, condition
):
    state, cloud, folder = release
    setattr(state, condition, True)
    with pytest.raises(RuntimeError):
        images.publish_runtime(cloud)
    assert state.pushes == []
    assert not any("login" in call for call in state.calls)
    assert not (folder / "images.json").exists()


def test_second_push_failure_preserves_only_confirmed_partial_outcome(images, release):
    state, cloud, folder = release
    state.fail_second = True
    with pytest.raises(RuntimeError):
        images.publish_runtime(cloud)
    assert len(state.pushes) == 1 and not (folder / "images.json").exists()
    partial = json.loads((folder / "publication-progress.json").read_text())
    assert partial["complete"] is False and partial["model"]["image"].endswith("a" * 64)
    assert "operator" not in partial


def test_failed_ci_never_builds_or_publishes(images, release):
    state, cloud, _ = release
    state.runs = [ci_run(conclusion="failure")]
    with pytest.raises(RuntimeError):
        images.publish_runtime(cloud)
    assert state.build_count == 0 and state.pushes == []


@pytest.mark.parametrize("fault", ["budget", "end", "stack", "source_during_stack"])
def test_publication_rejects_changed_binding_after_slow_checks(images, release, fault):
    from datetime import datetime, timedelta

    state, cloud, _ = release
    run = cloud.run

    def changed(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[:3] == ["aws", "ecr", "get-login-password"]:
            state.password_issued = True
        if state.build_count and argv[:3] == [
            "aws",
            "cloudformation",
            "describe-stacks",
        ]:
            payload = json.loads(result)
            stack = payload["Stacks"][0]
            if fault == "stack":
                stack["StackId"] += "-replacement"
            elif fault == "source_during_stack":
                state.changed = getattr(state, "password_issued", False)
            else:
                tag = next(
                    t
                    for t in stack["Tags"]
                    if t["Key"]
                    == ("BudgetUsd" if fault == "budget" else "RuntimeEndAt")
                )
                tag["Value"] = (
                    "24"
                    if fault == "budget"
                    else (
                        datetime.fromisoformat(tag["Value"]) + timedelta(hours=1)
                    ).isoformat()
                )
            return json.dumps(payload)
        return result

    cloud.run = changed
    with pytest.raises(RuntimeError):
        images.publish_runtime(cloud)
    assert not state.pushes
    assert not any("login" in argv for argv in state.calls)


@pytest.mark.parametrize("foreign", [False, True])
def test_cleanup_empties_only_the_two_matching_repository_ids(images, foreign):
    calls = []

    def aws(*argv):
        calls.append(argv)
        if argv[:2] == ("ecr", "list-images"):
            return {"imageIds": [{"imageDigest": "sha256:" + "a" * 64}]}
        if argv[:2] == ("ecr", "batch-delete-image"):
            return {"imageIds": [], "failures": []}
        pytest.fail("Unexpected cleanup operation")

    outputs = {
        "RepositoryName": "strawhats-midterm/iris",
        "OperatorRepositoryName": "foreign"
        if foreign
        else "strawhats-midterm/operator",
    }
    if foreign:
        with pytest.raises(RuntimeError):
            images.empty_repositories(SimpleNamespace(aws=aws), outputs, [])
        assert calls == []
    else:
        images.empty_repositories(SimpleNamespace(aws=aws), outputs, [])
        assert [a[3] for a in calls if a[:2] == ("ecr", "list-images")] == [
            "strawhats-midterm/iris",
            "strawhats-midterm/operator",
        ]
