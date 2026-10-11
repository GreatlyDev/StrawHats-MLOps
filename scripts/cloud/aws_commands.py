"""Small fail-closed boundaries for the owner-operated AWS release."""

import math
import re
import subprocess
from datetime import datetime, timedelta


def run_checked(
    argv: list[str], *, stdin: str | None = None, timeout_seconds: int = 60
) -> str:
    try:
        result = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"{argv[0]} could not finish; inspect the operation before retrying."
        ) from exc
    if result.returncode:
        # Provider output or stdin can contain credentials. Never echo either.
        raise RuntimeError(f"{argv[0]} exited {result.returncode}; sequence stopped.")
    return result.stdout.strip()


def validate_identity(identity: dict, expected_account: str) -> str:
    if not re.fullmatch(r"\d{12}", expected_account):
        raise RuntimeError("An explicit 12-digit AWS account is required.")
    arn = identity.get("Arn", "")
    if identity.get("Account") != expected_account or not re.fullmatch(
        rf"arn:aws:(?:iam::{expected_account}:(?:user|role)/[^\s]+|"
        rf"sts::{expected_account}:assumed-role/[^\s]+/[^\s]+)",
        arn,
    ):
        raise RuntimeError(
            "Expected account and a named non-root IAM principal are required."
        )
    return arn


def validate_creation_window(
    budget_usd: float, end_at: datetime, now: datetime
) -> None:
    if (
        not isinstance(budget_usd, (int, float))
        or not math.isfinite(budget_usd)
        or budget_usd <= 0
        or not isinstance(end_at, datetime)
        or end_at.tzinfo is None
        or now.tzinfo is None
        or not timedelta(0) < end_at - now <= timedelta(hours=72)
    ):
        raise RuntimeError(
            "A positive usage budget and a future UTC end within 72 hours are required."
        )


def require_eks_context(
    context: str, expected_account: str, region: str, cluster_name: str
) -> None:
    if context != f"arn:aws:eks:{region}:{expected_account}:cluster/{cluster_name}":
        raise RuntimeError("Refusing an unrelated Kubernetes context.")
