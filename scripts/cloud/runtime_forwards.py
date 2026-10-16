"""Own two loopback kubectl children; never reuse an occupied listener."""

import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager


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
                argv = [
                    executable,
                    "--context",
                    connection.target.context,
                    "--kubeconfig",
                    connection.receipt.kubeconfig,
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
