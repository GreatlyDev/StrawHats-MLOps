"""Publish one clean, CI-verified pair of model/operator images on owner request."""

import json
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import deploy
from .aws_commands import validate_identity
from .ci import require_green_ci

LOCAL = deploy.ROOT / ".local" / "owner-eks"
DIGEST = r"sha256:[0-9a-f]{64}"


@dataclass(frozen=True)
class ReleaseImage:
    repository: str
    image: str
    runtime_manifest_digest: str
    runtime_config_digest: str

    def __post_init__(self):
        if not re.fullmatch(
            re.escape(self.repository) + "@" + DIGEST, self.image
        ) or not all(
            re.fullmatch(DIGEST, value)
            for value in (self.runtime_manifest_digest, self.runtime_config_digest)
        ):
            raise ValueError("Invalid release image identity.")


@dataclass(frozen=True)
class ReleaseImages:
    source_sha: str
    account: str
    ci_run_id: int
    model: ReleaseImage
    operator: ReleaseImage

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-f0-9]{40}", self.source_sha)
            or not re.fullmatch(r"[0-9]{12}", self.account)
            or type(self.ci_run_id) is not int
            or self.ci_run_id <= 0
        ):
            raise ValueError("Invalid runtime source or CI identity.")
        registry = f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
        if (
            self.model.repository != registry + "iris"
            or self.operator.repository != registry + "operator"
        ):
            raise ValueError("Images must belong to the same project account.")


def save_record(name, value):
    LOCAL.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=LOCAL, encoding="utf-8", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False)
    try:
        temporary.replace(LOCAL / name)
    finally:
        temporary.unlink(missing_ok=True)


def source_sha(cloud):
    if cloud.run(["git", "status", "--porcelain"]):
        raise RuntimeError("A clean committed source tree is required.")
    sha = cloud.run(["git", "rev-parse", "HEAD"])
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("Invalid source SHA.")
    return sha


def _check(cloud, sha):
    if source_sha(cloud) != sha:
        raise RuntimeError("Source changed during release; publication stopped.")
    if (
        validate_identity(cloud.aws("sts", "get-caller-identity"), cloud.args.account)
        != cloud.principal
    ):
        raise RuntimeError("Owner identity changed during release.")
    cloud.stack()
    cloud.guard_window()


def inspect_local(image, run, *, operator=False):
    try:
        records = json.loads(run(["docker", "image", "inspect", image]))
        if len(records) != 1:
            raise ValueError
        record = records[0]
        if (
            record["Os"] != "linux"
            or record["Architecture"] != "amd64"
            or not re.fullmatch(DIGEST, record["Id"])
        ):
            raise ValueError
        if operator:
            config = record["Config"]
            if (
                config.get("User") != "10001"
                or config.get("Entrypoint")
                or config.get("Cmd") != ["python", "-m", "backend.model_operator"]
            ):
                raise ValueError
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    "import backend.model_operator.reconcile; import backend.model_operator.kube; import backend.model_operator.workload",
                ],
                timeout_seconds=60,
            )
        return record["Id"]
    except (KeyError, TypeError, ValueError, IndexError):
        raise RuntimeError(
            "The built runtime image identity or entrypoint is invalid."
        ) from None


def publish_runtime(cloud) -> ReleaseImages:
    _, outputs = cloud.stack()
    registry = (
        f"{cloud.args.account}.dkr.ecr.us-east-1.amazonaws.com/strawhats-midterm/"
    )
    repositories = {
        "model": outputs.get("RepositoryUri"),
        "operator": outputs.get("OperatorRepositoryUri"),
    }
    if repositories != {"model": registry + "iris", "operator": registry + "operator"}:
        raise RuntimeError("Both matching project ECR repositories are required.")
    sha = source_sha(cloud)
    ci = require_green_ci(sha, cloud.run)
    LOCAL.mkdir(parents=True, exist_ok=True)
    # An earlier complete receipt cannot describe this in-progress attempt.
    (LOCAL / "images.json").unlink(missing_ok=True)
    progress = {
        "source_sha": sha,
        "account": cloud.args.account,
        "ci_run_id": ci["run_id"],
        "complete": False,
    }
    save_record("publication-progress.json", progress)
    identities = {}
    for name, dockerfile in (
        ("model", "Dockerfile.model"),
        ("operator", "Dockerfile.operator"),
    ):
        _check(cloud, sha)
        image = repositories[name] + ":" + sha
        cloud.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                str(deploy.ROOT / "backend" / dockerfile),
                "-t",
                image,
                str(deploy.ROOT),
            ],
            timeout_seconds=900,
        )
        if name == "model":
            deploy.verify_local_image(image, cloud.run)
        identities[name] = inspect_local(image, cloud.run, operator=name == "operator")
        _check(cloud, sha)
    accepted = {}
    # TemporaryDirectory removes credentials even if publication or verification fails.
    with tempfile.TemporaryDirectory(prefix="docker-auth-", dir=LOCAL) as folder:
        _check(cloud, sha)
        password = cloud.aws("ecr", "get-login-password", text=True)
        _check(cloud, sha)
        cloud.run(
            [
                "docker",
                "--config",
                folder,
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                registry.split("/")[0],
            ],
            stdin=password,
        )
        for name, repository in repositories.items():
            _check(cloud, sha)
            cloud.run(
                ["docker", "--config", folder, "push", repository + ":" + sha],
                timeout_seconds=900,
            )
            progress[name] = {
                "repository": repository,
                "tag": sha,
                "uploaded": True,
                "verified": False,
            }
            save_record("publication-progress.json", progress)
            detail = cloud.aws(
                "ecr",
                "describe-images",
                "--repository-name",
                repository.split(".com/", 1)[1],
                "--image-ids",
                f"imageTag={sha}",
            )["imageDetails"][0]
            digest = detail["imageDigest"]
            if not re.fullmatch(DIGEST, digest):
                raise RuntimeError("Registry returned an invalid runtime digest.")
            # Record the accepted upload even if deeper manifest verification fails.
            progress[name] = {
                "repository": repository,
                "image": repository + "@" + digest,
                "verified": False,
            }
            save_record("publication-progress.json", progress)
            manifest, config = deploy.resolve_runtime_digests(
                repository,
                digest,
                lambda ref: cloud.run(
                    [
                        "docker",
                        "--config",
                        folder,
                        "buildx",
                        "imagetools",
                        "inspect",
                        "--raw",
                        ref,
                    ]
                ),
            )
            # Classic Docker reports the config ID. Its containerd store may
            # report the platform manifest or OCI index ID instead. All three
            # are content-addressed members of this exact published closure.
            if identities[name] not in (digest, manifest, config):
                raise RuntimeError(
                    "Registry runtime does not match the locally verified image."
                )
            accepted[name] = ReleaseImage(
                repository, repository + "@" + digest, manifest, config
            )
            progress[name] = asdict(accepted[name])
            save_record("publication-progress.json", progress)
    _check(cloud, sha)
    result = ReleaseImages(
        sha, cloud.args.account, ci["run_id"], accepted["model"], accepted["operator"]
    )
    save_record("images.json", asdict(result))
    deploy.save(
        "image.json",
        {"account": result.account, "source_sha": sha, **asdict(result.model)},
    )
    return result


def empty_repositories(cloud, outputs, resources):
    repositories = []
    for output, logical, expected in (
        ("RepositoryName", "ImageRepository", "strawhats-midterm/iris"),
        (
            "OperatorRepositoryName",
            "OperatorImageRepository",
            "strawhats-midterm/operator",
        ),
    ):
        physical = [
            r["PhysicalResourceId"]
            for r in resources
            if r.get("LogicalResourceId") == logical and r.get("PhysicalResourceId")
        ]
        actual = outputs.get(output) or (physical[0] if len(physical) == 1 else None)
        if len(physical) > 1 or (
            actual and (actual != expected or any(p != actual for p in physical))
        ):
            raise RuntimeError("Refusing unrelated ECR repository deletion.")
        if actual:
            repositories.append(actual)
    for repository in repositories:
        images = cloud.aws("ecr", "list-images", "--repository-name", repository)[
            "imageIds"
        ]
        for start in range(0, len(images), 100):
            result = cloud.aws(
                "ecr",
                "batch-delete-image",
                "--repository-name",
                repository,
                "--image-ids",
                json.dumps(images[start : start + 100]),
            )
            if result.get("failures"):
                raise RuntimeError(
                    "Image cleanup partially failed; stack deletion stopped."
                )
