"""Owner-only release entry point; help/render never contact AWS."""

import argparse
import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from backend.app.iris_target import RuntimeTarget

from . import deploy
from .aws_commands import run_checked, validate_creation_window
from .images import LOCAL, ReleaseImage, ReleaseImages, publish_runtime
from .runtime_contract import RuntimeReceipt
from .runtime_manifests import render_runtime


def prepare_receipt(cloud, images: ReleaseImages) -> RuntimeReceipt:
    if images.account != cloud.args.account:
        raise RuntimeError("Images belong to another AWS account.")
    stack, outputs = cloud.stack()
    if outputs.get("ClusterArn") != cloud.context:
        raise RuntimeError("The owned stack does not identify this EKS cluster.")
    cluster = cloud.aws("eks", "describe-cluster", "--name", deploy.CLUSTER)["cluster"]
    endpoint = cluster["endpoint"]
    address = urlsplit(endpoint)
    if (
        cluster.get("arn") != cloud.context
        or cluster.get("status") != "ACTIVE"
        or address.scheme != "https"
        or not address.hostname
        or not address.hostname.endswith(".us-east-1.eks.amazonaws.com")
        or address.username
        or address.password
        or address.port not in (None, 443)
        or address.path not in ("", "/")
        or address.query
        or address.fragment
    ):
        raise RuntimeError("The active EKS endpoint does not match this release.")
    ca = base64.b64decode(cluster["certificateAuthority"]["data"], validate=True)
    if not ca or len(ca) > 65536:
        raise RuntimeError("A bounded EKS certificate authority is required.")
    for image in (images.model, images.operator):
        detail = cloud.aws(
            "ecr",
            "describe-images",
            "--repository-name",
            image.repository.split(".com/", 1)[1],
            "--image-ids",
            "imageDigest=" + image.image.split("@")[1],
        )["imageDetails"]
        if (
            len(detail) != 1
            or detail[0].get("imageDigest") != image.image.split("@")[1]
            or images.source_sha not in detail[0].get("imageTags", [])
        ):
            raise RuntimeError(
                "The published source-SHA image pair could not be verified."
            )
    owner = next(
        p["ParameterValue"]
        for p in stack["Parameters"]
        if p["ParameterKey"] == "OwnerPrincipalArn"
    )
    cloud.owner(owner)
    budget, end = cloud.window
    validate_creation_window(budget, end, deploy.utc_now())
    target = RuntimeTarget(
        kind="eks",
        context=cloud.context,
        namespace="strawhats",
        monitoring_namespace="strawhats-monitoring",
        name="iris",
        image=images.model.image,
        image_pull_policy="IfNotPresent",
        operator_name="strawhats-model-operator",
        operator_image=images.operator.image,
    )
    release_id = str(uuid4())
    folder = LOCAL / "releases" / release_id
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "kubeconfig.yaml"
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": cloud.context,
        "clusters": [
            {
                "name": cloud.context,
                "cluster": {
                    "server": endpoint,
                    "certificate-authority-data": cluster["certificateAuthority"][
                        "data"
                    ],
                },
            }
        ],
        "contexts": [
            {
                "name": cloud.context,
                "context": {"cluster": cloud.context, "user": cloud.context},
            }
        ],
        "users": [
            {
                "name": cloud.context,
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
                            cloud.args.profile,
                        ],
                    }
                },
            }
        ],
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    receipt = RuntimeReceipt(
        kind="eks",
        phase="prepared",
        release_id=release_id,
        source_sha=images.source_sha,
        target=target,
        images=images,
        kubeconfig=str(path.resolve()),
        endpoint=endpoint,
        ca_sha256=hashlib.sha256(ca).hexdigest(),
        account=images.account,
        profile=cloud.args.profile,
        owner_arn=owner,
        stack_arn=stack["StackId"],
        budget_usd=budget,
        end_at=end.isoformat(),
    )
    receipt.save(folder / "runtime.json")
    return receipt


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="Render a validated target offline")
    render.add_argument("--target-json", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    for name in ("publish", "install", "verify", "remove"):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--account", required=True)
        command.add_argument("--receipt", type=Path, default=LOCAL / "runtime.json")
        if name == "install":
            command.add_argument("--images", type=Path, default=LOCAL / "images.json")
    return result


def main(argv=None, *, run=run_checked):
    args = parser().parse_args(argv)
    try:
        if args.command == "render":
            target = RuntimeTarget(
                **json.loads(args.target_json.read_text(encoding="utf-8-sig"))
            )
            bundle = render_runtime(target)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                yaml.safe_dump_all(
                    bundle.documents, sort_keys=False, allow_unicode=True
                ),
                encoding="utf-8",
            )
            return 0
        if args.command == "publish":
            result = asdict(publish_runtime(deploy.Cloud(args, run)))
        else:
            from backend.app.eks_cluster import connect_eks

            from .runtime_install import install_runtime, remove_runtime, verify_runtime

            if args.command == "install" and not args.receipt.exists():
                values = deploy.read_json(args.images)
                values["model"], values["operator"] = (
                    ReleaseImage(**values["model"]),
                    ReleaseImage(**values["operator"]),
                )
                receipt = prepare_receipt(
                    deploy.Cloud(args, run), ReleaseImages(**values)
                )
                receipt.save(args.receipt)
            purpose = {"install": "install", "verify": "operate", "remove": "cleanup"}[
                args.command
            ]
            with connect_eks(
                args.receipt,
                profile=args.profile,
                expected_account=args.account,
                purpose=purpose,
            ) as connection:
                if args.command == "install":
                    result = asdict(
                        install_runtime(
                            connection,
                            render_runtime(connection.target),
                            evidence_dir=LOCAL,
                        )
                    )
                elif args.command == "verify":
                    result = verify_runtime(connection, connection.receipt)
                else:
                    result = remove_runtime(connection, connection.receipt)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, StopIteration):
        print(
            "Owner release stopped. Check the approved window, binding and local receipt; no automatic retry was made."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
