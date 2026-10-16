"""Exercise owner publication/bootstrap through executable and path resolution."""

import json
from types import SimpleNamespace

import pytest

from scripts.cloud import deploy, owner_release


@pytest.mark.parametrize("operation", ["publish", "install"])
def test_owner_entrypoint_uses_installed_windows_cli_bridges(
    monkeypatch, tmp_path, operation
):
    import shutil

    from scripts.cloud import aws_commands

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: {
            "aws.exe": "/mnt/c/tools/aws.exe",
            "docker.exe": "/mnt/c/tools/docker.exe",
            "docker": "/bad/wrapper",
        }.get(name),
    )
    calls = []

    def boundary(argv, **kwargs):
        calls.append(argv)
        if argv[0].endswith("aws.exe"):
            return json.dumps(
                {
                    "Account": "123456789012",
                    "Arn": "arn:aws:iam::123456789012:user/owner",
                }
            )
        assert argv[0] == "/mnt/c/tools/docker.exe"
        assert "C:/work/backend/Dockerfile.model" in argv
        assert "C:/work/auth" in argv and argv[-1] == "C:/work"
        return ""

    monkeypatch.setattr(aws_commands, "run_checked", boundary)

    def publish(cloud):
        cloud.run(
            [
                "docker",
                "--config",
                "/mnt/c/work/auth",
                "build",
                "-f",
                "/mnt/c/work/backend/Dockerfile.model",
                "/mnt/c/work",
            ]
        )
        return SimpleNamespace()

    if operation == "publish":
        monkeypatch.setattr(
            owner_release, "publish_runtime", lambda cloud: publish(cloud)
        )
        monkeypatch.setattr(owner_release, "asdict", lambda value: {})
        expected = 0
    else:
        monkeypatch.setattr(
            deploy, "read_json", lambda path: {"model": {}, "operator": {}}
        )
        monkeypatch.setattr(owner_release, "ReleaseImage", lambda **v: None)
        monkeypatch.setattr(owner_release, "ReleaseImages", lambda **v: None)

        def prepare(cloud, images):
            publish(cloud)
            raise RuntimeError("stop before connection")

        monkeypatch.setattr(owner_release, "prepare_receipt", prepare)
        expected = 1
    assert (
        owner_release.main(
            [
                operation,
                "--profile",
                "strawhats",
                "--account",
                "123456789012",
                "--receipt",
                str(tmp_path / "runtime.json"),
            ]
        )
        == expected
    )
    assert len(calls) == 2
