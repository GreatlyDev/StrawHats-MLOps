"""The separate API container copies backend only; cloud tooling is optional."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_api_can_start_without_repository_release_tools(tmp_path):
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source,
        tmp_path / "backend",
        ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.app.main import create_app; print(create_app(test_mode=True).title)",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "StrawHats MLOps"
