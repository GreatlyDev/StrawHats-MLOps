"""Thin owner commands around the established observation and adjustment core."""

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from backend.app.owner_target import owner_connection
from scripts.monitoring.observations import PROMETHEUS_URL, MonitoringError, observe
from scripts.monitoring.traffic import run_demo
from scripts.optimization.execution import run_adjustment, save_evidence

from .runtime_contract import RuntimeReceipt

ROOT = Path(__file__).resolve().parents[2]


def arguments(operation, argv):
    parser = argparse.ArgumentParser(
        description=f"Owner Iris {operation}; explicit verified runtime required."
    )
    parser.add_argument("--target", required=True, choices=("eks", "rehearsal"))
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--account")
    if operation == "traffic":
        parser.add_argument("--run-demo", required=True, action="store_true")
    if operation == "adjust":
        parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.target == "eks" and (not args.profile or not args.account):
        parser.error("EKS requires --profile and --account.")
    if args.target == "rehearsal" and (args.profile or args.account):
        parser.error("Rehearsal does not accept AWS identity options.")
    return args


def command_main(operation, argv=None):
    if operation not in ("observe", "traffic", "adjust"):
        raise ValueError("Unsupported owner command.")
    args = arguments(operation, argv)
    evidence_dir = ROOT / ".local/owner-eks" / args.target
    config = {
        "EKS_RELEASE_RECEIPT": str(args.receipt),
        "REHEARSAL_RECEIPT": str(args.receipt),
        "EKS_AWS_PROFILE": args.profile,
        "EKS_EXPECTED_ACCOUNT": args.account,
    }
    result, success = {"complete": False, "kind": args.target}, False
    try:
        receipt = RuntimeReceipt.load(args.receipt)
        if receipt.kind != args.target:
            raise ValueError("Receipt kind differs from the selected runtime.")
        if operation == "adjust":
            load_dotenv(ROOT / ".env.local", override=False)
        with (
            owner_connection(args.target, config) as connection,
            httpx.Client(
                base_url=PROMETHEUS_URL,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as prometheus,
        ):
            if operation == "observe":
                observation = observe(
                    connection.cluster, prometheus, target=connection.target
                )
                result = asdict(observation)
                success = observation.available
            elif operation == "traffic":
                result = run_demo(
                    connection.cluster,
                    prometheus,
                    clock=time.monotonic,
                    sleep=time.sleep,
                    target=connection.target,
                )
                success = result["complete"]
            else:
                result = run_adjustment(
                    connection.cluster,
                    prometheus,
                    apply=args.apply,
                    key=os.getenv("OPENAI_API_KEY"),
                    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                    evidence_dir=evidence_dir / "adjustments",
                    target=connection.target,
                    before_mutation=connection.check_mutation,
                )
                success = result["state"] in ("held", "preview", "applied")
            result = {
                **result,
                "kind": connection.target.kind,
                "context": connection.target.context,
                "namespace": connection.target.namespace,
            }
    except MonitoringError as error:
        result.update(getattr(error, "evidence", {}))
    except (HTTPException, OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        pass
    path = evidence_dir / (operation + ".json")
    try:
        save_evidence(path, result)
    except OSError:
        success = False
    print(
        json.dumps(
            {"success": success, "kind": args.target, "evidence": str(path)},
            allow_nan=False,
        )
    )
    return 0 if success else 1
