import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.serve_model import create_model_service


@pytest.fixture
def iris_http():
    """Exercise real Iris routes over HTTP; corrupt one boundary when requested."""
    with TestClient(create_model_service()) as client:
        faults = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                response = client.get(self.path)
                if self.path == "/health" and faults.get("health"):
                    self.send_response(503)
                    self.end_headers()
                    return
                body = response.content
                if self.path == "/metrics" and faults.get("metrics"):
                    body = b"strawhats_model_predictions_total 0.0\n"
                self.send_response(response.status_code)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                response = client.post(self.path, json=json.loads(body))
                data = response.json()
                status = response.status_code
                if status == 200:
                    data.update(faults.get("prediction", {}))
                elif faults.get("invalid_accepted"):
                    status = 200
                    data = {"label": "setosa"}
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", faults
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_smoke_verifies_real_prediction_and_observed_counter_increment(iris_http):
    from scripts.cloud.smoke import verify_iris

    base_url, _ = iris_http
    result = verify_iris(base_url)
    assert result["health"]["model"] == "iris-classifier"
    assert result["prediction"]["label"] == "setosa"
    assert result["prediction"]["confidence"] > 0.8
    assert result["counter_after"] > result["counter_before"]
    assert result["invalid_input_status"] == 422


@pytest.mark.parametrize(
    "fault",
    [
        {"health": True},
        {"prediction": {"label": "virginica"}},
        {"prediction": {"confidence": 0.5}},
        {"prediction": {"confidence": float("nan")}},
        {"prediction": {"probabilities": {"setosa": 1.0}}},
        {"metrics": True},
        {"invalid_accepted": True},
    ],
)
def test_smoke_rejects_failed_or_misleading_endpoint_evidence(iris_http, fault):
    from scripts.cloud.smoke import verify_iris

    base_url, faults = iris_http
    faults.update(fault)
    with pytest.raises(RuntimeError):
        verify_iris(base_url)
