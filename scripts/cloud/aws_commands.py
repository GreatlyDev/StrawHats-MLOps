"""Small fail-closed boundaries for the owner-operated AWS release."""

import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIAGNOSTICS = (
    Path(__file__).resolve().parents[2] / ".local/milestone-one/command-failures.jsonl"
)


def record_failure(argv, exit_code, stderr="", *, credential_sensitive=False):
    """Retain identifiers and allowlisted error codes, never arbitrary process output."""
    sensitive = credential_sensitive or "get-login-password" in argv or "login" in argv
    aws_error = (
        None
        if sensitive
        else re.search(r"An error occurred \(([A-Za-z0-9_.]+)\)", stderr)
    )
    reasons = (
        "Forbidden",
        "Unauthorized",
        "connection refused",
        "context was not found",
        "timed out",
        "address already in use",
        "pod is not running",
    )
    record = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "operation": " ".join(argv[:3]),
        "exit_code": exit_code,
        "aws_error_code": aws_error[1] if aws_error else None,
        "reason": "credential output excluded"
        if sensitive
        else next(
            (r for r in reasons if r.lower() in stderr.lower()),
            "inspect operation status",
        ),
        "resource_ids": [
            a
            for a in argv
            if a == "strawhats-midterm"
            or re.fullmatch(
                r"arn:aws:cloudformation:us-east-1:\d{12}:(stack|changeSet)/[^\s]+", a
            )
        ],
    }
    DIAGNOSTICS.parent.mkdir(parents=True, exist_ok=True)
    with DIAGNOSTICS.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return str(DIAGNOSTICS)


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
        location = record_failure(
            argv,
            "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "start-failed",
            credential_sensitive=stdin is not None,
        )
        raise RuntimeError(
            f"{argv[0]} could not finish; safe failure record: {location}"
        ) from exc
    if result.returncode:
        location = record_failure(
            argv,
            result.returncode,
            result.stderr or "",
            credential_sensitive=stdin is not None,
        )
        # Provider output or stdin can contain credentials. Never echo either.
        raise RuntimeError(
            f"{argv[0]} exited {result.returncode}; sequence stopped. Safe failure record: {location}"
        )
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
