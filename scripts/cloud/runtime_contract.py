"""Non-secret release records shared by installation and owner connections."""

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from backend.app.iris_target import RuntimeTarget

from .images import DIGEST, ReleaseImage, ReleaseImages


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@lru_cache(maxsize=8)
def _inventory_keys(target):
    from .runtime_manifests import document_key, render_runtime

    bundle = render_runtime(target)
    return frozenset(
        document_key(d)
        for d in (
            *bundle.documents,
            *bundle.metrics_documents,
            *bundle.secret_references,
        )
    )


@dataclass(frozen=True)
class LocalImages:
    source_sha: str
    model_reference: str
    model_image_id: str
    operator_reference: str
    operator_image_id: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-f0-9]{40}", self.source_sha) or not all(
            re.fullmatch(DIGEST, v)
            for v in (self.model_image_id, self.operator_image_id)
        ):
            raise ValueError("Actual local source and image identities are required.")


@dataclass(frozen=True)
class ObjectRecord:
    api_version: str
    kind: str
    namespace: str
    name: str
    uid: str
    ownership: Literal["created", "borrowed"]
    manifest_sha256: str

    def __post_init__(self):
        if (
            self.ownership not in ("created", "borrowed")
            or not isinstance(self.uid, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.uid)
            or not re.fullmatch(r"[a-f0-9]{64}", self.manifest_sha256)
        ):
            raise ValueError(
                "A captured object UID and manifest fingerprint are required."
            )

    def document(self):
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "metadata": {
                "name": self.name,
                **({"namespace": self.namespace} if self.namespace else {}),
            },
        }


@dataclass(frozen=True)
class RuntimeReceipt:
    kind: Literal["eks", "rehearsal"]
    phase: Literal["prepared", "partial", "installed", "removed"]
    release_id: str
    source_sha: str
    target: RuntimeTarget
    images: ReleaseImages | LocalImages
    kubeconfig: str
    endpoint: str
    ca_sha256: str
    inventory: tuple[ObjectRecord, ...] = ()
    account: str | None = None
    profile: str | None = None
    owner_arn: str | None = None
    stack_arn: str | None = None
    budget_usd: float | None = None
    end_at: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        from .runtime_manifests import document_key

        UUID(self.release_id)
        if (
            self.schema_version != 1
            or self.phase not in ("prepared", "partial", "installed", "removed")
            or self.kind not in ("eks", "rehearsal")
            or self.target.kind != self.kind
            or self.source_sha != self.images.source_sha
            or not re.fullmatch(r"[a-f0-9]{40}", self.source_sha)
            or not re.fullmatch(r"[a-f0-9]{64}", self.ca_sha256)
        ):
            raise ValueError("Invalid runtime receipt binding.")
        if self.kind == "rehearsal":
            if (
                not isinstance(self.images, LocalImages)
                or any(
                    v is not None
                    for v in (
                        self.account,
                        self.profile,
                        self.owner_arn,
                        self.stack_arn,
                        self.budget_usd,
                        self.end_at,
                    )
                )
                or self.images.model_reference != self.target.image
                or self.images.operator_reference != self.target.operator_image
            ):
                raise ValueError(
                    "Local rehearsal cannot carry cloud credentials or identity."
                )
            if not self.source_sha.startswith(self.target.image.rsplit("-", 1)[1]):
                raise ValueError(
                    "Rehearsal image tag does not match its source commit."
                )
        else:
            if (
                not isinstance(self.images, ReleaseImages)
                or self.images.account != self.account
                or self.images.model.image != self.target.image
                or self.images.operator.image != self.target.operator_image
                or not self.profile
                or not self.owner_arn
                or not self.stack_arn
                or not self.end_at
                or not self.budget_usd
            ):
                raise ValueError("Complete EKS owner binding is required.")
        allowed = _inventory_keys(self.target)
        identities = [document_key(r.document()) for r in self.inventory]
        if len(set(identities)) != len(identities) or any(
            i not in allowed for i in identities
        ):
            raise ValueError("Inventory contains an unrelated or duplicate object.")
        if self.phase == "prepared" and self.inventory:
            raise ValueError("A prepared receipt cannot claim installed resources.")

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(self), stream, indent=2, allow_nan=False)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            value["target"] = RuntimeTarget(**value["target"])
            image_data = value["images"]
            if value["kind"] == "eks":
                for name in ("model", "operator"):
                    image_data[name] = ReleaseImage(**image_data[name])
                value["images"] = ReleaseImages(**image_data)
            else:
                value["images"] = LocalImages(**image_data)
            value["inventory"] = tuple(ObjectRecord(**r) for r in value["inventory"])
            return cls(**value)
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            raise RuntimeError(
                "A valid, supported runtime receipt is required."
            ) from None
