import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "workspace.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, name TEXT, version TEXT, payload TEXT,
                    UNIQUE(name, version)
                );
                CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS inferences (
                    id INTEGER PRIMARY KEY, model_id TEXT, label TEXT,
                    latency_ms REAL, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, kind TEXT, title TEXT,
                    description TEXT, created_at TEXT
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def models(self) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row["payload"])
                for row in db.execute("SELECT payload FROM models ORDER BY rowid DESC")
            ]

    def model(self, model_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM models WHERE id=?", (model_id,)
            ).fetchone()
            return json.loads(row["payload"]) if row else None

    def has_identity(self, name: str, version: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM models WHERE name=? AND version=?", (name, version)
                ).fetchone()
                is not None
            )

    def add_model(self, model: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO models VALUES (?,?,?,?)",
                (model["id"], model["name"], model["version"], json.dumps(model)),
            )
            self.event(
                "model", "Model registered", f"{model['name']} · {model['version']}", db
            )

    def plans(self) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row["payload"])
                for row in db.execute("SELECT payload FROM plans ORDER BY rowid DESC")
            ]

    def add_plan(self, plan: dict):
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?,?)", (plan["id"], json.dumps(plan)))
            self.event(
                "deployment",
                "Deployment preview created",
                f"{plan['name']} · {plan['replicas']} replica(s)",
                db,
            )

    def prediction(self, model_id: str, label: str, latency_ms: float):
        with self.connect() as db:
            db.execute(
                "INSERT INTO inferences (model_id,label,latency_ms,created_at) VALUES (?,?,?,?)",
                (model_id, label, latency_ms, now()),
            )
            self.event(
                "inference",
                "Prediction completed",
                f"{label} · {latency_ms:.2f} ms",
                db,
            )

    def event(self, kind: str, title: str, description: str, db=None):
        if db is not None:
            db.execute(
                "INSERT INTO events (kind,title,description,created_at) VALUES (?,?,?,?)",
                (kind, title, description, now()),
            )
        else:
            with self.connect() as connection:
                self.event(kind, title, description, connection)

    def events(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")
            ]

    def overview(self) -> dict:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM inferences").fetchone()[0]
            recent = [
                dict(row)
                for row in db.execute(
                    "SELECT model_id,label,latency_ms,created_at FROM inferences ORDER BY id DESC LIMIT 30"
                )
            ]
            return {
                "models": db.execute("SELECT COUNT(*) FROM models").fetchone()[0],
                "deployment_plans": db.execute("SELECT COUNT(*) FROM plans").fetchone()[
                    0
                ],
                "predictions": total,
                "median_latency_ms": median(row["latency_ms"] for row in recent)
                if recent
                else None,
                "recent_inferences": list(reversed(recent)),
            }
