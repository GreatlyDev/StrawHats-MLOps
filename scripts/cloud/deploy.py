"""Owner-operated EKS release. Cloud commands require an explicit named profile."""

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .aws_commands import (
    require_eks_context,
    run_checked,
    validate_creation_window,
    validate_identity,
)
from .smoke import verify_iris

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local" / "milestone-one"
TEMPLATE = ROOT / "infra" / "aws" / "cluster.yaml"
REGION = "us-east-1"
CLUSTER = "strawhats-midterm"
PROJECT = "StrawHats-MLOps"
KUBECONFIG = LOCAL / "kubeconfig"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("A valid local release receipt is required.") from exc


def save(name, data):
    LOCAL.mkdir(parents=True, exist_ok=True)
    path = LOCAL / name
    path.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
    return str(path)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "preflight",
        "preview",
        "create",
        "publish",
        "apply",
        "verify",
        "cleanup",
    ):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--account", required=True)
        if name == "preview":
            command.add_argument("--owner-arn", required=True)
            command.add_argument("--admin-cidr", required=True)
            command.add_argument("--az-a", required=True)
            command.add_argument("--az-b", required=True)
            command.add_argument("--budget-usd", required=True, type=float)
            command.add_argument("--end-at", required=True)
            command.add_argument("--eligibility-confirmed", action="store_true")
        if name == "create":
            command.add_argument("--receipt", default=str(LOCAL / "preview.json"))
            command.add_argument("--confirm-execute")
        if name == "apply":
            command.add_argument("--image-receipt", default=str(LOCAL / "image.json"))
        if name == "cleanup":
            command.add_argument("--confirm-delete")
    return result


class Cloud:
    def __init__(self, args, run):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.profile):
            raise RuntimeError("Use a named AWS profile.")
        self.args, self.run = args, run
        self.identity = self.aws("sts", "get-caller-identity")
        self.principal = validate_identity(self.identity, args.account)
        self.context = f"arn:aws:eks:{REGION}:{args.account}:cluster/{CLUSTER}"
        require_eks_context(self.context, args.account, REGION, CLUSTER)

    def aws(self, *arguments, text=False, timeout=60):
        output = self.run(
            [
                "aws",
                *arguments,
                "--profile",
                self.args.profile,
                "--region",
                REGION,
                "--no-cli-pager",
                "--no-cli-auto-prompt",
                "--output",
                "text" if text else "json",
            ],
            timeout_seconds=timeout,
        )
        return output if text else json.loads(output or "{}")

    def kubectl(self, *arguments, timeout=120):
        return self.run(
            [
                "kubectl",
                "--context",
                self.context,
                *arguments,
                "--kubeconfig",
                str(KUBECONFIG),
            ],
            timeout_seconds=timeout,
        )

    def owner(self, arn):
        if not re.fullmatch(
            rf"arn:aws:iam::{self.args.account}:(user|role)/[^\s]+", arn
        ):
            raise RuntimeError(
                "Owner must be an IAM user/role in the expected account."
            )
        if self.principal == arn:
            return
        if ":role/" in arn and ":assumed-role/" in self.principal:
            role = self.aws("iam", "get-role", "--role-name", arn.rsplit("/", 1)[1])[
                "Role"
            ]
            if (
                role["Arn"] == arn
                and self.identity.get("UserId", "").split(":")[0] == role["RoleId"]
            ):
                return
        raise RuntimeError("Release credentials must belong to the configured owner.")

    def preflight(self):
        versions = self.aws(
            "eks", "describe-cluster-versions", "--cluster-versions", "1.35"
        )
        if not any(
            v.get("versionStatus") == "STANDARD_SUPPORT"
            for v in versions.get("clusterVersions", [])
        ):
            raise RuntimeError(
                "EKS 1.35 standard support must be confirmed before release."
            )
        plan = self.aws("freetier", "get-account-plan-state")
        if (
            plan.get("accountId") != self.args.account
            or plan.get("accountPlanStatus") != "ACTIVE"
        ):
            raise RuntimeError("Active account plan could not be verified.")
        instance = self.aws(
            "ec2", "describe-instance-types", "--instance-types", "t3.medium"
        )["InstanceTypes"][0]
        if plan.get("accountPlanType") == "FREE" and not instance.get(
            "FreeTierEligible"
        ):
            raise RuntimeError(
                "This Free plan does not establish t3.medium eligibility. Stop; owner must review AWS eligibility. No plan upgrade performed."
            )
        if plan.get("accountPlanType") not in {"FREE", "PAID"}:
            raise RuntimeError("Unknown account plan; release stopped.")
        zones = self.aws(
            "ec2",
            "describe-availability-zones",
            "--filters",
            "Name=state,Values=available",
        )
        offerings = self.aws(
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "availability-zone",
            "--filters",
            "Name=instance-type,Values=t3.medium",
        )
        quota = self.aws(
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "ec2",
            "--quota-code",
            "L-1216C47A",
        )
        eks_quota = self.aws(
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "eks",
            "--quota-code",
            "L-1194D53C",
        )
        if quota["Quota"]["Value"] < 2 or eks_quota["Quota"]["Value"] < 1:
            raise RuntimeError(
                "Insufficient EC2/EKS quota; no quota increase requested."
            )
        return {
            "plan": plan,
            "zones": zones,
            "offerings": offerings,
            "ec2_quota": quota,
            "eks_quota": eks_quota,
        }

    def stack(self, *, active=True):
        stacks = self.aws("cloudformation", "describe-stacks", "--stack-name", CLUSTER)[
            "Stacks"
        ]
        stack = stacks[0]
        if stack["StackId"].split(":")[4] != self.args.account:
            raise RuntimeError("Stack account differs from the approved account.")
        tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
        if tags.get("Project") != PROJECT or tags.get("Milestone") != "one":
            raise RuntimeError(
                "Refusing a stack without this project's ownership tags."
            )
        parameters = {
            p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]
        }
        self.owner(parameters["OwnerPrincipalArn"])
        if active and stack["StackStatus"] != "CREATE_COMPLETE":
            raise RuntimeError("A completely created project stack is required.")
        if active:
            validate_creation_window(
                float(tags.get("BudgetUsd", "nan")),
                datetime.fromisoformat(tags.get("RuntimeEndAt", "")),
                datetime.now(timezone.utc),
            )
        return stack, {
            o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])
        }


def preview(cloud):
    args = cloud.args
    end = datetime.fromisoformat(args.end_at.replace("Z", "+00:00"))
    validate_creation_window(args.budget_usd, end, datetime.now(timezone.utc))
    cloud.owner(args.owner_arn)
    network = ipaddress.ip_network(args.admin_cidr, strict=True)
    if (
        network.version != 4
        or network.prefixlen != 32
        or not network.network_address.is_global
    ):
        raise RuntimeError("Use the owner's current external IPv4 /32.")
    if not args.eligibility_confirmed:
        raise RuntimeError(
            "Owner must confirm EKS, EC2, and credit eligibility before preparing a cloud change set."
        )
    checks = cloud.preflight()
    zones = {z["ZoneName"] for z in checks["zones"]["AvailabilityZones"]}
    offers = {o["Location"] for o in checks["offerings"]["InstanceTypeOfferings"]}
    if args.az_a == args.az_b or not {args.az_a, args.az_b} <= zones & offers:
        raise RuntimeError("Select two available AZs offering t3.medium.")
    clusters = cloud.aws("eks", "list-clusters")["clusters"]
    if CLUSTER in clusters:
        raise RuntimeError("Refusing to adopt a pre-existing cluster.")
    stacks = cloud.aws("cloudformation", "list-stacks")["StackSummaries"]
    if any(
        s["StackName"] == CLUSTER and s["StackStatus"] != "DELETE_COMPLETE"
        for s in stacks
    ):
        raise RuntimeError(
            "Existing stack requires inspection; do not replace or adopt it."
        )
    parameters = {
        "ClusterName": CLUSTER,
        "KubernetesVersion": "1.35",
        "OwnerPrincipalArn": args.owner_arn,
        "AdminCidr": str(network),
        "AvailabilityZoneA": args.az_a,
        "AvailabilityZoneB": args.az_b,
    }
    body = TEMPLATE.read_text(encoding="utf-8")
    cloud.aws(
        "cloudformation",
        "validate-template",
        "--template-body",
        "file://" + str(TEMPLATE),
    )
    name = "iris-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    created = cloud.aws(
        "cloudformation",
        "create-change-set",
        "--stack-name",
        CLUSTER,
        "--change-set-name",
        name,
        "--change-set-type",
        "CREATE",
        "--template-body",
        "file://" + str(TEMPLATE),
        "--capabilities",
        "CAPABILITY_NAMED_IAM",
        "--parameters",
        json.dumps(
            [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
        ),
        "--tags",
        f"Key=Project,Value={PROJECT}",
        "Key=Milestone,Value=one",
        f"Key=RuntimeEndAt,Value={end.isoformat()}",
        f"Key=BudgetUsd,Value={args.budget_usd}",
    )
    receipt = {
        "account": args.account,
        "profile": args.profile,
        "change_set": created["Id"],
        "stack_id": created["StackId"],
        "template_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "parameters": parameters,
        "budget_usd": args.budget_usd,
        "end_at": end.isoformat(),
        "eligibility_confirmed": True,
    }
    # Save recovery identity before waiting: an interrupted preview is not lost.
    save("preview.json", receipt)
    cloud.aws(
        "cloudformation",
        "wait",
        "change-set-create-complete",
        "--change-set-name",
        created["Id"],
        timeout=900,
    )
    changes = cloud.aws(
        "cloudformation", "describe-change-set", "--change-set-name", created["Id"]
    )
    save("change-set.json", changes)
    return {
        "receipt": str(LOCAL / "preview.json"),
        "review": str(LOCAL / "change-set.json"),
        "change_set": created["Id"],
        "executed": False,
    }


def create(cloud, receipt):
    args = cloud.args
    if receipt["account"] != args.account or receipt["profile"] != args.profile:
        raise RuntimeError("Receipt belongs to a different account/profile.")
    end = datetime.fromisoformat(receipt["end_at"])
    validate_creation_window(receipt["budget_usd"], end, datetime.now(timezone.utc))
    if not receipt.get("eligibility_confirmed"):
        raise RuntimeError("Eligibility was not confirmed.")
    change_set = receipt["change_set"]
    if not re.fullmatch(
        rf"arn:aws:cloudformation:{REGION}:{args.account}:changeSet/iris-[0-9]+/[^\s]+",
        change_set,
    ):
        raise RuntimeError("Unrelated change set.")
    cloud.owner(receipt["parameters"]["OwnerPrincipalArn"])
    cloud.preflight()
    actual = cloud.aws(
        "cloudformation", "describe-change-set", "--change-set-name", change_set
    )
    actual_parameters = {
        p["ParameterKey"]: p["ParameterValue"] for p in actual["Parameters"]
    }
    tags = {t["Key"]: t["Value"] for t in actual.get("Tags", [])}
    template = cloud.aws(
        "cloudformation", "get-template", "--change-set-name", change_set
    )["TemplateBody"]
    if (
        actual["StackId"] != receipt["stack_id"]
        or actual["StackName"] != CLUSTER
        or actual["Status"] != "CREATE_COMPLETE"
        or actual["ExecutionStatus"] != "AVAILABLE"
        or actual_parameters != receipt["parameters"]
        or tags.get("Project") != PROJECT
        or tags.get("Milestone") != "one"
        or tags.get("RuntimeEndAt") != receipt["end_at"]
        or float(tags.get("BudgetUsd", "nan")) != receipt["budget_usd"]
        or not isinstance(template, str)
        or hashlib.sha256(template.encode()).hexdigest() != receipt["template_sha256"]
        or hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        != receipt["template_sha256"]
    ):
        raise RuntimeError(
            "Cloud change set differs from the reviewed local template/inputs."
        )
    cloud.aws("cloudformation", "execute-change-set", "--change-set-name", change_set)
    cloud.aws(
        "cloudformation",
        "wait",
        "stack-create-complete",
        "--stack-name",
        receipt["stack_id"],
        timeout=3600,
    )
    stack, outputs = cloud.stack()
    cluster = cloud.aws("eks", "describe-cluster", "--name", CLUSTER)["cluster"]
    node = cloud.aws(
        "eks",
        "describe-nodegroup",
        "--cluster-name",
        CLUSTER,
        "--nodegroup-name",
        "iris-worker",
    )["nodegroup"]
    if (
        cluster["status"] != "ACTIVE"
        or node["status"] != "ACTIVE"
        or node["scalingConfig"] != {"minSize": 1, "maxSize": 1, "desiredSize": 1}
    ):
        raise RuntimeError(
            "Stack finished but EKS runtime does not match the approved size."
        )
    LOCAL.mkdir(parents=True, exist_ok=True)
    cloud.aws(
        "eks",
        "update-kubeconfig",
        "--name",
        CLUSTER,
        "--alias",
        cloud.context,
        "--kubeconfig",
        str(KUBECONFIG),
        text=True,
    )
    ready = json.loads(cloud.kubectl("get", "nodes", "-o", "json"))["items"]
    if len(ready) != 1 or not any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in ready[0]["status"]["conditions"]
    ):
        raise RuntimeError("Exactly one Ready worker is required.")
    return {
        "stack_id": stack["StackId"],
        "outputs": outputs,
        "end_at": receipt["end_at"],
        "cloud_inference_verified": False,
    }


def wait_for_http(base_url, timeout=60, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("Port-forward exited before verification.")
        try:
            with urlopen(base_url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Model HTTP endpoint did not become ready.")


def verify_local_image(image, run):
    """Check the actual rebuilt release image before any registry publication."""
    container = run(
        [
            "docker",
            "run",
            "--detach",
            "--read-only",
            "--user",
            "10001:10001",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "0.5",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--label",
            "strawhats.task=release-image-check",
            "-p",
            "127.0.0.1::8000",
            image,
        ]
    )
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise RuntimeError("Docker did not return an exact task-owned container ID.")
    try:
        address = run(["docker", "port", container, "8000/tcp"])
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
            raise RuntimeError(
                "Container must expose only a loopback verification port."
            )
        wait_for_http("http://" + address)
        return verify_iris("http://" + address)
    finally:
        run(["docker", "rm", "--force", container])


def publish(cloud):
    _, outputs = cloud.stack()
    repository = outputs["RepositoryUri"]
    expected = f"{cloud.args.account}.dkr.ecr.{REGION}.amazonaws.com/{CLUSTER}/iris"
    if repository != expected:
        raise RuntimeError("Unexpected ECR repository.")
    if cloud.run(["git", "status", "--porcelain"]):
        raise RuntimeError(
            "Commit tracked release changes before building a source-SHA image."
        )
    sha = cloud.run(["git", "rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Invalid source SHA.")
    image = f"{repository}:{sha}"
    cloud.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            str(ROOT / "backend/Dockerfile.model"),
            "-t",
            image,
            str(ROOT),
        ],
        timeout_seconds=900,
    )
    evidence = verify_local_image(image, cloud.run)
    save("release-container-evidence.json", evidence)
    password = cloud.aws("ecr", "get-login-password", text=True)
    registry = repository.split("/")[0]
    # Use an ignored task-only config, avoiding changes to the owner's Docker login.
    config = LOCAL / "docker-auth"
    config.mkdir(parents=True, exist_ok=True)
    try:
        cloud.run(
            [
                "docker",
                "--config",
                str(config),
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                registry,
            ],
            stdin=password,
        )
        cloud.run(
            ["docker", "--config", str(config), "push", image], timeout_seconds=900
        )
    finally:
        (config / "config.json").unlink(missing_ok=True)
    detail = cloud.aws(
        "ecr",
        "describe-images",
        "--repository-name",
        outputs["RepositoryName"],
        "--image-ids",
        f"imageTag={sha}",
    )["imageDetails"][0]
    digest = detail["imageDigest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("ECR returned an invalid digest.")
    result = {
        "account": cloud.args.account,
        "source_sha": sha,
        "image": f"{repository}@{digest}",
    }
    save("image.json", result)
    return result


def apply(cloud):
    _, outputs = cloud.stack()
    receipt = read_json(cloud.args.image_receipt)
    image = receipt.get("image", "")
    if receipt.get("account") != cloud.args.account or not re.fullmatch(
        re.escape(outputs["RepositoryUri"]) + r"@sha256:[0-9a-f]{64}", image
    ):
        raise RuntimeError("A digest from this project's private ECR is required.")
    cloud.aws(
        "ecr",
        "describe-images",
        "--repository-name",
        outputs["RepositoryName"],
        "--image-ids",
        "imageDigest=" + image.split("@")[1],
    )
    overlay = LOCAL / "iris-overlay"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources:\n  - ../../../infra/kubernetes/iris\nimages:\n"
        f"  - name: strawhats-iris\n    newName: {image.split('@')[0]}\n    digest: {image.split('@')[1]}\n",
        encoding="utf-8",
    )
    cloud.kubectl("apply", "-k", str(overlay))
    cloud.kubectl(
        "-n",
        "strawhats",
        "rollout",
        "status",
        "deployment/iris",
        "--timeout=180s",
        timeout=200,
    )
    return {"image": image, "rolled_out": True, "inference_verified": False}


def verify(cloud):
    cloud.stack()
    cloud.kubectl(
        "-n",
        "strawhats",
        "rollout",
        "status",
        "deployment/iris",
        "--timeout=180s",
        timeout=200,
    )
    deployment = json.loads(
        cloud.kubectl("-n", "strawhats", "get", "deployment", "iris", "-o", "json")
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if (
        deployment["spec"]["replicas"] != 1
        or pod.get("automountServiceAccountToken") is not False
        or pod["securityContext"].get("runAsUser") != 10001
        or container["securityContext"].get("readOnlyRootFilesystem") is not True
        or container["securityContext"].get("allowPrivilegeEscalation") is not False
        or container["securityContext"].get("capabilities", {}).get("drop") != ["ALL"]
        or container["resources"]
        != {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        }
        or not re.search(r"@sha256:[0-9a-f]{64}$", container["image"])
    ):
        raise RuntimeError("Cloud workload differs from the approved hardened runtime.")

    def forwarded_smoke():
        # Ask the OS for a free port; fail if another process wins the bind race.
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        process = subprocess.Popen(
            [
                "kubectl",
                "--context",
                cloud.context,
                "--kubeconfig",
                str(KUBECONFIG),
                "-n",
                "strawhats",
                "port-forward",
                "service/iris",
                f"{port}:80",
                "--address",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        try:
            wait_for_http(f"http://127.0.0.1:{port}", timeout=30, process=process)
            return verify_iris(f"http://127.0.0.1:{port}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    before = forwarded_smoke()
    cloud.kubectl("-n", "strawhats", "rollout", "restart", "deployment/iris")
    cloud.kubectl(
        "-n",
        "strawhats",
        "rollout",
        "status",
        "deployment/iris",
        "--timeout=180s",
        timeout=200,
    )
    after = forwarded_smoke()
    return {
        "context": cloud.context,
        "image": container["image"],
        "before_restart": before,
        "after_restart": after,
    }


def cleanup(cloud):
    stack, outputs = cloud.stack(active=False)
    if cloud.args.confirm_delete != stack["StackId"]:
        raise RuntimeError(
            "Confirm deletion using the exact inspected project stack ARN."
        )
    resources = cloud.aws(
        "cloudformation", "list-stack-resources", "--stack-name", stack["StackId"]
    )["StackResourceSummaries"]
    vpcs = [
        r["PhysicalResourceId"]
        for r in resources
        if r["ResourceType"] == "AWS::EC2::VPC"
    ]
    if len(vpcs) != 1:
        raise RuntimeError(
            "Could not establish the dedicated project VPC for cleanup verification."
        )
    repo = outputs.get("RepositoryName") or next(
        (
            r["PhysicalResourceId"]
            for r in resources
            if r["LogicalResourceId"] == "ImageRepository"
        ),
        None,
    )
    if repo:
        if repo != f"{CLUSTER}/iris":
            raise RuntimeError("Refusing unrelated ECR repository deletion.")
        images = cloud.aws("ecr", "list-images", "--repository-name", repo)["imageIds"]
        for start in range(0, len(images), 100):
            deleted = cloud.aws(
                "ecr",
                "batch-delete-image",
                "--repository-name",
                repo,
                "--image-ids",
                json.dumps(images[start : start + 100]),
            )
            if deleted.get("failures"):
                raise RuntimeError(
                    "Image cleanup partially failed; stack deletion stopped."
                )
    cloud.aws("cloudformation", "delete-stack", "--stack-name", stack["StackId"])
    cloud.aws(
        "cloudformation",
        "wait",
        "stack-delete-complete",
        "--stack-name",
        stack["StackId"],
        timeout=3600,
    )
    if CLUSTER in cloud.aws("eks", "list-clusters")["clusters"]:
        raise RuntimeError("Cluster remains after cleanup.")
    instances = cloud.aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=vpc-id,Values={vpcs[0]}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    )["Reservations"]
    volumes = cloud.aws(
        "ec2",
        "describe-volumes",
        "--filters",
        f"Name=tag:Project,Values={PROJECT}",
        "Name=tag:Milestone,Values=one",
    )["Volumes"]
    ips = cloud.aws(
        "ec2", "describe-addresses", "--filters", f"Name=tag:Project,Values={PROJECT}"
    )["Addresses"]
    networks = cloud.aws(
        "ec2",
        "describe-network-interfaces",
        "--filters",
        f"Name=vpc-id,Values={vpcs[0]}",
    )["NetworkInterfaces"]
    if instances or volumes or ips or networks:
        raise RuntimeError(
            "Owned compute/storage/network remains; inspect before declaring shutdown."
        )
    return {"deleted_stack": stack["StackId"], "verified_no_owned_runtime": True}


def dispatch(args, *, run=run_checked):
    receipt = None
    if args.command == "create":
        receipt = read_json(args.receipt)
        if not args.confirm_execute or args.confirm_execute != receipt.get(
            "change_set"
        ):
            raise RuntimeError(
                "Review the actual change set and confirm its exact ARN before execution."
            )
    cloud = Cloud(args, run)
    if args.command == "preflight":
        return cloud.preflight()
    if args.command == "create":
        return create(cloud, receipt)
    return {
        "preview": preview,
        "publish": publish,
        "apply": apply,
        "verify": verify,
        "cleanup": cleanup,
    }[args.command](cloud)


def main():
    arguments = parser().parse_args()
    try:
        result = dispatch(arguments)
        if arguments.command in {"create", "verify", "cleanup"}:
            save(arguments.command + "-evidence.json", result)
    except RuntimeError as exc:
        # These are our guard messages; subprocess output is redacted at its boundary.
        parser().exit(1, f"Release stopped: {exc}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not print raw provider data or exceptions containing tokens.
        parser().exit(
            1,
            f"Release stopped ({type(exc).__name__}). Inspect the local inputs and AWS operation status before retrying.\n",
        )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
