"""Require the exact release commit's latest main-push GitHub Actions result."""

import json
import re

REPOSITORY = "GreatlyDev/StrawHats-MLOps"
JOBS = (
    "Backend tests and lint",
    "Frontend tests and build",
    "Monitoring configuration and alert rules",
)


def require_green_ci(source_sha: str, run) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise RuntimeError("A complete source SHA is required for the CI gate.")

    def fetch(path, key):
        try:
            pages = json.loads(
                run(
                    [
                        "gh",
                        "api",
                        f"repos/{REPOSITORY}/actions/{path}",
                        "--paginate",
                        "--slurp",
                    ]
                )
            )
            if not isinstance(pages, list):
                raise ValueError
            return [row for page in pages for row in page[key]]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("GitHub returned incomplete CI evidence.") from None

    runs = fetch(
        f"workflows/ci.yml/runs?branch=main&event=push&head_sha={source_sha}&per_page=100",
        "workflow_runs",
    )
    if not runs or any(type(r.get("id")) is not int for r in runs):
        raise RuntimeError("No verified CI run exists for this release.")
    latest = max(runs, key=lambda r: r["id"])
    run_id, attempt = latest["id"], latest.get("run_attempt")
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    if not (
        run_id > 0
        and type(attempt) is int
        and attempt > 0
        and latest.get("head_sha") == source_sha
        and latest.get("head_branch") == "main"
        and latest.get("event") == "push"
        and latest.get("path") == ".github/workflows/ci.yml"
        and latest.get("repository", {}).get("full_name") == REPOSITORY
        and latest.get("head_repository", {}).get("full_name") == REPOSITORY
        and latest.get("status") == "completed"
        and latest.get("conclusion") == "success"
        and latest.get("html_url") == url
    ):
        raise RuntimeError(
            "Latest release CI is incomplete, unsuccessful or unrelated."
        )
    jobs = fetch(f"runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs")
    for name in JOBS:
        matching = [j for j in jobs if j.get("name") == name]
        if (
            len(matching) != 1
            or matching[0].get("status") != "completed"
            or matching[0].get("conclusion") != "success"
        ):
            raise RuntimeError("All three required release CI jobs must pass.")
    return {
        "repository": REPOSITORY,
        "source_sha": source_sha,
        "run_id": run_id,
        "url": url,
    }
