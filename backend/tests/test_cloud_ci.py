"""The release accepts only the latest successful main-push CI for its SHA."""

import importlib
import json
from copy import deepcopy

import pytest

SHA = "a" * 40
REPO = "GreatlyDev/StrawHats-MLOps"
JOBS = [
    "Backend tests and lint",
    "Frontend tests and build",
    "Monitoring configuration and alert rules",
]


def ci_run(**changes):
    return {
        "id": 50,
        "run_attempt": 1,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/{REPO}/actions/runs/50",
        **changes,
    }


@pytest.fixture
def ci():
    try:
        return importlib.import_module("scripts.cloud.ci")
    except ModuleNotFoundError:
        pytest.fail("Exact-SHA CI release gate is not implemented")


def boundary(runs, *, jobs=None):
    def run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        assert "--paginate" in argv and "--slurp" in argv
        path = argv[2]
        assert path.startswith(f"repos/{REPO}/actions/")
        if "/workflows/ci.yml/runs?" in path:
            assert (
                f"head_sha={SHA}" in path
                and "event=push" in path
                and "branch=main" in path
            )
            return json.dumps([{"workflow_runs": runs}])
        assert path == f"repos/{REPO}/actions/runs/50/attempts/1/jobs?per_page=100"
        return json.dumps(
            [
                {
                    "jobs": jobs
                    if jobs is not None
                    else [
                        {"name": n, "status": "completed", "conclusion": "success"}
                        for n in JOBS
                    ]
                }
            ]
        )

    return run


def test_exact_sha_main_push_with_all_required_jobs_passes(ci):
    result = ci.require_green_ci(SHA, boundary([ci_run()]))
    assert result == {
        "repository": REPO,
        "source_sha": SHA,
        "run_id": 50,
        "url": f"https://github.com/{REPO}/actions/runs/50",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"head_sha": "b" * 40},
        {"repository": {"full_name": "foreign/repo"}},
        {"head_repository": {"full_name": "fork/repo"}},
        {"event": "pull_request"},
        {"path": ".github/workflows/other.yml"},
        {"head_branch": "feature/other"},
    ],
)
def test_untrusted_or_incomplete_ci_never_passes(ci, changes):
    with pytest.raises(RuntimeError):
        ci.require_green_ci(SHA, boundary([ci_run(**changes)]))


def test_missing_or_newer_failed_ci_blocks_older_success(ci):
    for runs in ([], [ci_run(), ci_run(id=51, conclusion="failure")]):
        with pytest.raises(RuntimeError):
            ci.require_green_ci(SHA, boundary(runs))


@pytest.mark.parametrize(
    "bad", ["missing", "skipped", "failure", "pending", "duplicate"]
)
def test_each_required_job_must_succeed_once(ci, bad):
    jobs = [{"name": n, "status": "completed", "conclusion": "success"} for n in JOBS]
    if bad == "missing":
        jobs.pop()
    elif bad == "duplicate":
        jobs.append(deepcopy(jobs[0]))
    else:
        jobs[0].update(
            status="in_progress" if bad == "pending" else "completed", conclusion=bad
        )
    with pytest.raises(RuntimeError):
        ci.require_green_ci(SHA, boundary([ci_run()], jobs=jobs))
