"""Own two loopback kubectl children; never reuse an occupied listener."""

import base64
import hashlib
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import yaml

from .cli import cli_path  # noqa: F401


def require_free_port(port):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise RuntimeError(
            f"Loopback port {port} is occupied; stop its owner explicitly."
        ) from None


def wait_ready(process, output, port):
    deadline = time.monotonic() + 30
    expected = f"Forwarding from 127.0.0.1:{port} -> {port}"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The owned forward exited before becoming ready.")
        output.seek(0)
        if expected in output.read().decode("utf-8", errors="replace"):
            return
        time.sleep(0.1)
    raise RuntimeError("The owned forward did not confirm its loopback listener.")


class ForwardSession:
    def __init__(self):
        self.processes = []

    def check(self):
        if len(self.processes) != 2 or any(
            p.poll() is not None for p in self.processes
        ):
            raise RuntimeError(
                "An owned monitoring forward stopped; no reading is trusted."
            )

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@contextmanager
def owned_kubeconfig(connection, executable):
    """Pass only validated SDK material to kubectl; never reopen user plugins."""
    settings = connection.cluster.sdk.configuration
    receipt = connection.receipt
    ca = Path(settings.ssl_ca_cert).read_bytes()
    if (
        settings.host != receipt.endpoint
        or not settings.verify_ssl
        or settings.proxy
        or hashlib.sha256(ca).hexdigest() != receipt.ca_sha256
    ):
        raise ValueError("Forward transport differs from the verified SDK.")
    if receipt.kind == "eks":
        user = {"token": settings.api_key["authorization"]}
    else:
        user = {
            "client-certificate-data": base64.b64encode(
                Path(settings.cert_file).read_bytes()
            ).decode(),
            "client-key-data": base64.b64encode(
                Path(settings.key_file).read_bytes()
            ).decode(),
        }
    context = connection.target.context
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "contexts": [
            {"name": context, "context": {"cluster": context, "user": context}}
        ],
        "clusters": [
            {
                "name": context,
                "cluster": {
                    "server": settings.host,
                    "certificate-authority-data": base64.b64encode(ca).decode(),
                },
            }
        ],
        "users": [{"name": context, "user": user}],
    }
    with tempfile.TemporaryDirectory(
        prefix="owned-forward-", dir=connection.receipt_path.parent
    ) as folder:
        path = Path(folder) / "kubeconfig"
        path.touch(mode=0o600)
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        yield cli_path(path, executable)


@contextmanager
def managed_forwards(connection):
    if (
        connection.receipt.kind not in ("eks", "rehearsal")
        or connection.receipt.phase != "installed"
    ):
        raise RuntimeError("An installed owner runtime is required.")
    for port in (9090, 3000):
        require_free_port(port)
    executable = shutil.which("kubectl") or shutil.which("kubectl.exe")
    if not executable:
        raise RuntimeError("kubectl is required.")
    owned = ForwardSession()
    with ExitStack() as stack:
        try:
            for name, port in (("prometheus", 9090), ("grafana", 3000)):
                output = stack.enter_context(tempfile.TemporaryFile())
                connection.check_mutation()
                kubeconfig = stack.enter_context(
                    owned_kubeconfig(connection, executable)
                )
                argv = [
                    executable,
                    "--context",
                    connection.target.context,
                    "--kubeconfig",
                    kubeconfig,
                    "-n",
                    connection.target.monitoring_namespace,
                    "port-forward",
                    f"service/{name}",
                    f"{port}:{port}",
                    "--address",
                    "127.0.0.1",
                ]
                process = subprocess.Popen(
                    argv, stdout=output, stderr=subprocess.STDOUT, shell=False
                )
                owned.processes.append(process)
                wait_ready(process, output, port)
            owned.check()
            yield owned
        finally:
            owned.close()
