"""SQLite persistence for jobs and uploaded profiles."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    builtin    INTEGER NOT NULL DEFAULT 0,
    color_space TEXT NOT NULL,
    device_class TEXT NOT NULL,
    pcs        TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    filename   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    image_sha256    TEXT NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    image_mode      TEXT NOT NULL,
    source_profile  TEXT NOT NULL,
    press_profile   TEXT NOT NULL,
    display_profile TEXT NOT NULL,
    source_intent   TEXT NOT NULL,
    proof_intent    TEXT NOT NULL,
    threshold       REAL NOT NULL,
    use_embedded    INTEGER NOT NULL DEFAULT 0,
    embedded_sha256 TEXT,
    parameters_json TEXT NOT NULL,
    profile_digests_json TEXT NOT NULL,
    de_mean         REAL,
    de_p95          REAL,
    de_max          REAL,
    over_ratio      REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- 印刷配置版本发布与回归门禁
-- --------------------------------------------------------------------------

-- 配置族：同一族的草稿/版本共享族名；revision 是期望版本号并发控制的乐观锁。
CREATE TABLE IF NOT EXISTS press_families (
    family_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    revision    INTEGER NOT NULL DEFAULT 1,
    current_version_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 版本（含草稿）：profile_sha256 指向 /data/release/snapshots 下不可变 ICC 快照。
CREATE TABLE IF NOT EXISTS press_versions (
    version_id    TEXT PRIMARY KEY,
    family_id     TEXT NOT NULL REFERENCES press_families(family_id),
    version_no    INTEGER,                       -- NULL = 草稿；发布后固定
    status        TEXT NOT NULL,                 -- draft|current|superseded|withdrawn
    profile_sha256 TEXT NOT NULL,
    profile_size  INTEGER NOT NULL,
    profile_name  TEXT NOT NULL,
    label         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_at  TEXT,
    withdrawn_at  TEXT,
    gate_run_id   TEXT,
    UNIQUE(family_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_press_versions_family ON press_versions(family_id);

-- 代表性任务基线：图像/源/显示 ICC/参数全部固定，自包含存于 /data/release/baselines。
CREATE TABLE IF NOT EXISTS gate_baselines (
    baseline_id   TEXT PRIMARY KEY,
    family_id     TEXT NOT NULL REFERENCES press_families(family_id),
    name          TEXT NOT NULL,
    source_job_id TEXT,
    image_sha256  TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    image_mode    TEXT NOT NULL,
    source_profile_id TEXT NOT NULL,
    press_profile_id  TEXT NOT NULL,
    display_profile_id TEXT NOT NULL,
    source_profile_sha256 TEXT,
    press_profile_sha256  TEXT NOT NULL,
    display_profile_sha256 TEXT NOT NULL,
    source_intent TEXT NOT NULL,
    proof_intent  TEXT NOT NULL,
    threshold     REAL NOT NULL,
    use_embedded  INTEGER NOT NULL DEFAULT 0,
    limit_de_mean REAL NOT NULL,
    limit_de_p95  REAL NOT NULL,
    limit_over_ratio REAL NOT NULL,
    limit_changed_pixels INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gate_baselines_family ON gate_baselines(family_id);

-- 门禁运行：草稿快照 × 全部基线，逐项判定。
CREATE TABLE IF NOT EXISTS gate_runs (
    run_id        TEXT PRIMARY KEY,
    family_id     TEXT NOT NULL REFERENCES press_families(family_id),
    draft_version_id TEXT NOT NULL REFERENCES press_versions(version_id),
    draft_profile_sha256 TEXT NOT NULL,
    status        TEXT NOT NULL,                  -- running|passed|failed
    n_baselines   INTEGER NOT NULL,
    n_passed      INTEGER NOT NULL DEFAULT 0,
    results_json  TEXT NOT NULL,                 -- 每项基线的判定/度量/原因
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gate_runs_family ON gate_runs(family_id);

-- 不可变审计：仅追加；prev_hash/entry_hash 构成哈希链，触发器拒绝 UPDATE/DELETE。
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    family_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash  TEXT,
    entry_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 不可变：禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 不可变：禁止删除');
END;
"""

# 串行化所有发布模块写事务（发布/撤回/恢复/草稿/基线/门禁），
# 保证审计哈希链顺序与期望版本号并发控制在同一临界区内完成。
write_lock = threading.RLock()


class RevisionConflict(RuntimeError):
    """期望版本号与服务端当前 revision 不一致（HTTP 409）。"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """临界区内的 BEGIN IMMEDIATE 事务；提交/回滚由 with 块统一处理。"""
    with write_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def upsert_profile(conn: sqlite3.Connection, record) -> None:
    conn.execute(
        """
        INSERT INTO profiles (profile_id, name, role, builtin, color_space,
                              device_class, pcs, size, sha256, filename)
        VALUES (:profile_id, :name, :role, :builtin, :color_space,
                :device_class, :pcs, :size, :sha256, :filename)
        ON CONFLICT(profile_id) DO UPDATE SET
            name=excluded.name, role=excluded.role,
            color_space=excluded.color_space, device_class=excluded.device_class,
            pcs=excluded.pcs, size=excluded.size, sha256=excluded.sha256
        """,
        {
            "profile_id": record.profile_id,
            "name": record.name,
            "role": record.role,
            "builtin": 1 if record.builtin else 0,
            "color_space": record.color_space,
            "device_class": record.device_class,
            "pcs": record.pcs,
            "size": record.size,
            "sha256": record.sha256,
            "filename": record.filename,
        },
    )
    conn.commit()


def insert_job(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs
        (job_id, image_sha256, width, height, image_mode, source_profile,
         press_profile, display_profile, source_intent, proof_intent,
         threshold, use_embedded, embedded_sha256, parameters_json,
         profile_digests_json, de_mean, de_p95, de_max, over_ratio)
        VALUES (:job_id, :image_sha256, :width, :height, :image_mode,
                :source_profile, :press_profile, :display_profile,
                :source_intent, :proof_intent, :threshold, :use_embedded,
                :embedded_sha256, :parameters_json, :profile_digests_json,
                :de_mean, :de_p95, :de_max, :over_ratio)
        """,
        {
            "job_id": job["job_id"],
            "image_sha256": job["image_sha256"],
            "width": job["width"],
            "height": job["height"],
            "image_mode": job["image_mode"],
            "source_profile": job["source_profile"],
            "press_profile": job["press_profile"],
            "display_profile": job["display_profile"],
            "source_intent": job["source_intent"],
            "proof_intent": job["proof_intent"],
            "threshold": job["threshold"],
            "use_embedded": 1 if job.get("use_embedded") else 0,
            "embedded_sha256": job.get("embedded_sha256"),
            "parameters_json": json.dumps(job["parameters"], sort_keys=True, ensure_ascii=False),
            "profile_digests_json": json.dumps(job["profile_digests"], sort_keys=True, ensure_ascii=False),
            "de_mean": job["stats"]["mean"],
            "de_p95": job["stats"]["p95"],
            "de_max": job["stats"]["max"],
            "over_ratio": job["stats"]["over_ratio"],
        },
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def recent_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,)
    ).fetchall()
