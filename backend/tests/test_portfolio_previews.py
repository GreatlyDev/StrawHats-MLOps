import json

import yaml

from backend.app.deployments import preview
from backend.app.schemas import DeploymentInput
from scripts.portfolio import export_iris


def test_preview_oracle_matches_real_backend_resources():
    oracle = export_iris.build_preview_oracle()
    assert len(oracle["cases"]) == 3
    assert {case["input"]["replicas"] for case in oracle["cases"]} == {1, 2, 10}
    for case in oracle["cases"]:
        expected = list(
            yaml.safe_load_all(preview(DeploymentInput(**case["input"]))["manifest"])
        )
        assert case["resources"] == expected
        assert [resource["kind"] for resource in expected] == [
            "Namespace",
            "Deployment",
            "Service",
        ]


def test_checked_in_preview_oracle_is_current():
    actual = json.loads(export_iris.PREVIEWS.read_text(encoding="utf-8"))
    assert actual == export_iris.build_preview_oracle()
