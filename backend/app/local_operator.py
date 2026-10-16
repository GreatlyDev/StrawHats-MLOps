"""Compatibility entry point for the fixed Docker Desktop Iris router."""

from contextlib import contextmanager

from .iris_routes import iris_router
from .iris_target import LOCAL_TARGET
from .local_cluster import connect_local_cluster
from .owner_target import OwnerConnection


def local_operator_router(enabled: bool, record_event):
    @contextmanager
    def connect():
        with connect_local_cluster() as cluster:
            yield OwnerConnection(
                LOCAL_TARGET, cluster, None, None, "operate", None, lambda: None
            )

    return iris_router(
        prefix="/api/local-iris",
        label="Local Iris",
        enabled=enabled,
        connect=connect,
        record_event=record_event,
    )
