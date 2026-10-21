"""Projects: a named folder holding a SQLite store of runs and their results.

Each run has its own prompts (a frozen copy of the file chosen for it) and its own
settings (models, per-model temperature / max tokens, system prompt), so a project
can re-run the same or different prompts with different choices.
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import slugify, write_json_atomic

RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    config     TEXT NOT NULL
)
"""
RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS results (
    run_id        INTEGER NOT NULL,
    prompt_idx    INTEGER NOT NULL,
    target_id     TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    response      TEXT,
    error         TEXT,
    finish_reason TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    latency_ms    INTEGER,
    attempts      INTEGER,
    finished_at   TEXT,
    PRIMARY KEY (run_id, prompt_idx, target_id)
)
"""


def read_prompts(csv_path: Path) -> list:
    """First column of every row after the header, skipping blank cells."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    prompts = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if row and row[0].strip():
                prompts.append(row[0].strip())
    return prompts


def target_id(provider_id: str, model: str) -> str:
    return f"{provider_id}:{model}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_run_config() -> dict:
    return {
        "prompts": [],
        "source_file": None,
        "targets": [],
        "system_prompt": None,
        "system_prompt_file": None,
    }


def is_project_dir(path: Path) -> bool:
    return (path / "project.json").is_file()


@dataclass
class Run:
    """One run's prompts and settings.

    config = {
        "prompts": [str],
        "source_file": str | None,
        "targets": [{"id", "provider", "model", "temperature", "max_tokens"}],
        "system_prompt": str | None,
        "system_prompt_file": str | None,
    }
    """
    id: int
    created_at: str
    config: dict

    @property
    def targets(self) -> list:
        return self.config["targets"]

    @property
    def prompts(self) -> list:
        return self.config.get("prompts", [])

    def settings_for(self, target: dict) -> dict:
        """The request settings providers.complete() expects, for one target."""
        return {
            "system_prompt": self.config.get("system_prompt"),
            "temperature": target.get("temperature"),
            "max_tokens": target.get("max_tokens"),
        }


class Project:
    def __init__(self, path: Path) -> None:
        self.path = path
        with open(path / "project.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.name: str = meta["name"]
        self.created_at: str = meta.get("created_at", "")
        self.last_opened: Optional[str] = meta.get("last_opened")
        self._connect()
        self._migrate_v1(meta)
        self.db.execute(RUNS_TABLE)
        self.db.execute(RESULTS_TABLE)
        self.db.commit()
        self._migrate_v2(meta)
        if {"targets", "settings", "prompts", "source_csv"} & set(meta):
            self.save_meta()

    def _connect(self) -> None:
        self.db = sqlite3.connect(str(self.path / "results.db"))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")

    def _migrate_v1(self, meta: dict) -> None:
        """Older projects kept one set of targets/settings in project.json; make that run 1."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(results)")]
        if cols and "run_id" not in cols:
            backup = sqlite3.connect(str(self.path / "results.v1-backup.db"))
            self.db.backup(backup)
            backup.close()
            settings = meta.get("settings", {})
            config = {
                "targets": [
                    dict(t, temperature=settings.get("temperature"), max_tokens=settings.get("max_tokens"))
                    for t in meta.get("targets", [])
                ],
                "system_prompt": settings.get("system_prompt"),
            }
            self.db.isolation_level = None
            try:
                self.db.execute("BEGIN")
                self.db.execute("ALTER TABLE results RENAME TO results_v1")
                self.db.execute(RUNS_TABLE)
                self.db.execute(RESULTS_TABLE)
                self.db.execute(
                    "INSERT INTO runs VALUES (1, ?, ?)",
                    (meta.get("created_at") or now_iso(), json.dumps(config)),
                )
                self.db.execute("INSERT INTO results SELECT 1, * FROM results_v1")
                self.db.execute("DROP TABLE results_v1")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            finally:
                self.db.isolation_level = ""

    def _migrate_v2(self, meta: dict) -> None:
        """Older projects kept one prompt list in project.json; copy it into each run."""
        if "prompts" not in meta:
            return
        for run in self.runs():
            if "prompts" not in run.config:
                run.config["prompts"] = meta["prompts"]
                run.config["source_file"] = meta.get("source_csv")
                self.db.execute(
                    "UPDATE runs SET config = ? WHERE id = ?", (json.dumps(run.config), run.id)
                )
        self.db.commit()

    @classmethod
    def create(cls, root: Path, name: str) -> "Project":
        path = root / slugify(name)
        path.mkdir(parents=True, exist_ok=False)
        write_json_atomic(path / "project.json", {"name": name, "created_at": now_iso()})
        return cls(path)

    @staticmethod
    def list_all(root: Path) -> list:
        return [p for p in root.iterdir() if is_project_dir(p)]

    def save_meta(self) -> None:
        meta = {"name": self.name, "created_at": self.created_at}
        if self.last_opened:
            meta["last_opened"] = self.last_opened
        write_json_atomic(self.path / "project.json", meta)

    def mark_opened(self) -> Optional[str]:
        """Record that the project was opened now. Returns the previous time."""
        previous = self.last_opened
        self.last_opened = now_iso()
        self.save_meta()
        return previous

    def rename(self, name: str) -> None:
        """Rename the project, and its folder too if that name is free."""
        self.name = name
        self.save_meta()
        new_path = self.path.with_name(slugify(name))
        if new_path != self.path and not new_path.exists():
            self.close()
            self.path.rename(new_path)
            self.path = new_path
            self._connect()

    def close(self) -> None:
        self.db.close()

    def delete(self) -> None:
        self.close()
        shutil.rmtree(self.path)

    # --- runs --------------------------------------------------------------

    def runs(self) -> list:
        return [
            Run(rid, created, json.loads(config))
            for rid, created, config in self.db.execute(
                "SELECT id, created_at, config FROM runs ORDER BY id"
            )
        ]

    def last_run(self) -> Optional[Run]:
        row = self.db.execute("SELECT id, created_at, config FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return Run(row[0], row[1], json.loads(row[2])) if row else None

    def create_run(self, config: dict) -> Run:
        created = now_iso()
        cur = self.db.execute(
            "INSERT INTO runs (created_at, config) VALUES (?, ?)", (created, json.dumps(config))
        )
        self.db.commit()
        return Run(cur.lastrowid, created, config)

    def log_path(self, run_id: int) -> Path:
        logs = self.path / "logs"
        logs.mkdir(exist_ok=True)
        return logs / f"run-{run_id}.log"

    def total(self, run: Run) -> int:
        return len(run.prompts) * len(run.targets)

    def counts(self, run: Run) -> dict:
        """{target_id: {'ok': n, 'error': n}}"""
        out = {t["id"]: {"ok": 0, "error": 0} for t in run.targets}
        for tid, status, n in self.db.execute(
            "SELECT target_id, status, COUNT(*) FROM results WHERE run_id = ?"
            " GROUP BY target_id, status",
            (run.id,),
        ):
            if tid in out:
                out[tid][status] = n
        return out

    def summary(self, run: Run) -> tuple:
        """(ok, error, total)"""
        counts = self.counts(run)
        ok = sum(c["ok"] for c in counts.values())
        err = sum(c["error"] for c in counts.values())
        return ok, err, self.total(run)

    def pending(self, run: Run, include_errors: bool) -> list:
        """(prompt_idx, target) pairs still to run, interleaved across targets."""
        skip = "SELECT prompt_idx, target_id FROM results WHERE run_id = ?"
        if include_errors:
            skip += " AND status = 'ok'"
        done = set(self.db.execute(skip, (run.id,)))
        return [
            (i, t)
            for i in range(len(run.prompts))
            for t in run.targets
            if (i, t["id"]) not in done
        ]

    def save_result(
        self,
        run_id: int,
        prompt_idx: int,
        tid: str,
        status: str,
        response: Optional[str] = None,
        error: Optional[str] = None,
        finish_reason: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        latency_ms: Optional[int] = None,
        attempts: Optional[int] = None,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, prompt_idx, tid, status, response, error, finish_reason,
             input_tokens, output_tokens, latency_ms, attempts, now_iso()),
        )
        self.db.commit()

    def rows(self, run_ids: list):
        marks = ",".join("?" * len(run_ids))
        cur = self.db.execute(
            "SELECT run_id, prompt_idx, target_id, status, response, error, finish_reason,"
            " input_tokens, output_tokens, latency_ms, attempts, finished_at"
            f" FROM results WHERE run_id IN ({marks}) ORDER BY run_id, prompt_idx, target_id",
            run_ids,
        )
        cols = [d[0] for d in cur.description]
        for row in cur:
            yield dict(zip(cols, row))
