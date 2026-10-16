"""Internal runtime selection and immutable connection binding checks."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import HTTPException

from scripts.cloud.runtime_contract import RuntimeReceipt, fingerprint
from scripts.cloud.runtime_manifests import document_key, render_runtime

from .iris_target import LOCAL_TARGET, RuntimeTarget

UNAVAILABLE = "Owner runtime unavailable. Check its named profile, approved window and release binding."


@dataclass
class OwnerConnection:
    target: RuntimeTarget
    cluster: object
    receipt: RuntimeReceipt | None
    receipt_path: Path | None
    purpose: str
    kube: object
    guard: object

    def check_mutation(self):
        try:
            self.guard()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, UNAVAILABLE) from None


def receipt_binding(receipt):
    value = asdict(receipt)
    value.pop("phase")
    value.pop("inventory")
    return fingerprint(value)


def check_phase(receipt, purpose):
    allowed = {
        "install": {"prepared", "partial", "installed"},
        "operate": {"installed"},
        "cleanup": {"partial", "installed", "removed"},
    }
    if purpose not in allowed or receipt.phase not in allowed[purpose]:
        raise ValueError("Runtime phase does not permit this operation.")
    if purpose == "operate":
        bundle = render_runtime(receipt.target)
        required = {
            document_key(d)
            for d in (
                *bundle.documents,
                *bundle.metrics_documents,
                *bundle.secret_references,
            )
        }
        if {document_key(r.document()) for r in receipt.inventory} != required:
            raise ValueError("The installed runtime inventory is incomplete.")


def check_binding(connection, binding):
    receipt = RuntimeReceipt.load(connection.receipt_path)
    if (
        receipt_binding(receipt) != binding
        or receipt.inventory != connection.receipt.inventory
    ):
        raise ValueError("Runtime receipt changed while connected.")
    check_phase(receipt, connection.purpose)
    return receipt


def check_inventory(connection):
    for record in connection.receipt.inventory:
        actual = connection.kube.get(record.document())
        if actual is None:
            if connection.purpose == "cleanup":
                continue
            raise ValueError("A recorded runtime object is missing.")
        if actual.get("metadata", {}).get("uid") != record.uid:
            raise ValueError("A recorded runtime UID changed.")


@contextmanager
def owner_connection(kind, config, *, purpose="operate"):
    if purpose not in ("operate", "install", "cleanup"):
        raise HTTPException(503, UNAVAILABLE)
    try:
        if kind == "local":
            if purpose != "operate":
                raise ValueError("Local commands use their existing installation path.")
            from .local_cluster import connect_local_cluster

            with connect_local_cluster() as cluster:
                yield OwnerConnection(
                    LOCAL_TARGET, cluster, None, None, purpose, None, lambda: None
                )
        elif kind == "eks":
            from .eks_cluster import connect_eks

            with connect_eks(
                Path(config["EKS_RELEASE_RECEIPT"]),
                profile=config["EKS_AWS_PROFILE"],
                expected_account=config["EKS_EXPECTED_ACCOUNT"],
                purpose=purpose,
            ) as connection:
                yield connection
        elif kind == "rehearsal":
            from .rehearsal_cluster import connect_rehearsal

            with connect_rehearsal(
                Path(config["REHEARSAL_RECEIPT"]), purpose=purpose
            ) as connection:
                yield connection
        else:
            raise ValueError("Unsupported owner target.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, UNAVAILABLE) from None
