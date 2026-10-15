"""Preview one bounded local Iris resource decision; --apply permits one change."""

import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException
from urllib3.util.retry import Retry

from backend.app.local_cluster import connect_local_cluster
from scripts.monitoring.observations import PROMETHEUS_URL

from .execution import run_adjustment

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply at most one eligible change and verify readiness/inference.",
    )
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env.local", override=False)
    try:
        with connect_local_cluster() as cluster:
            # No pool has served a request yet. Unknown write outcomes must never retry.
            retry = Retry(total=0, redirect=0, raise_on_redirect=False)
            cluster.sdk.configuration.retries = retry
            cluster.sdk.rest_client.pool_manager.connection_pool_kw["retries"] = retry
            with httpx.Client(
                base_url=PROMETHEUS_URL,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as prometheus:
                result = run_adjustment(
                    cluster,
                    prometheus,
                    apply=args.apply,
                    key=os.getenv("OPENAI_API_KEY"),
                    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                    evidence_dir=ROOT / ".local" / "optimization",
                )
    except HTTPException:
        result = {
            "state": "unavailable",
            "action": "hold",
            "message": "Cannot connect to the verified docker-desktop context. Start local Kubernetes and check its setup.",
        }
    print(json.dumps(result, allow_nan=False))
    return 0 if result["state"] in ("held", "preview", "applied") else 1


if __name__ == "__main__":
    raise SystemExit(main())
