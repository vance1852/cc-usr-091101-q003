"""SQLite 账本与内容寻址对象仓库。

所有状态转移都在单个事务里提交；``event_id``、幂等键等唯一约束是
重复投递/崩溃恢复的最后一道防线。通过校验的媒体字节写入 ``objects/``
（候选区），校验失败的字节进入 ``quarantine/`` 留证，二者物理隔离。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    shot_id     TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    scene       TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id    TEXT NOT NULL,
    shot_id       TEXT NOT NULL REFERENCES shots(shot_id),
    version       INTEGER NOT NULL,
    template      TEXT NOT NULL,
    variables_json TEXT NOT NULL,
    deprecated_json TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(package_id, version)
);

CREATE TABLE IF NOT EXISTS reference_images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id TEXT NOT NULL,
    shot_id      TEXT REFERENCES shots(shot_id),   -- NULL = 跨镜头共享角色参考
    role         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    media_type   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(reference_id, version)
);

-- 镜头与参考（含共享参考）的显式绑定，冻结输入时自动登记
CREATE TABLE IF NOT EXISTS shot_reference_bindings (
    shot_id      TEXT NOT NULL REFERENCES shots(shot_id),
    reference_id TEXT NOT NULL,
    role         TEXT NOT NULL,
    PRIMARY KEY (shot_id, reference_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id        TEXT PRIMARY KEY,
    shot_id           TEXT NOT NULL REFERENCES shots(shot_id),
    frozen_digest     TEXT NOT NULL,
    frozen_json       TEXT NOT NULL,
    expected_media_type TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    dispatch_id       TEXT UNIQUE,
    dispatched_at     TEXT,
    attempts_made     INTEGER NOT NULL DEFAULT 0,
    success_event_id  TEXT,          -- 成功回调先落的“认领”标记
    declared_sha      TEXT,
    terminal_at       TEXT,
    terminal_event_id TEXT,
    failure_code      TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_shot ON attempts(shot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_digest ON attempts(frozen_digest);

-- 派发表即计费凭证：同一冻结尝试只有一行、一个幂等键
CREATE TABLE IF NOT EXISTS dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      TEXT NOT NULL REFERENCES attempts(attempt_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    dispatch_id     TEXT UNIQUE,
    calls_made      INTEGER NOT NULL DEFAULT 0,  -- 实际供应商调用次数（含超时重试）
    state           TEXT NOT NULL,               -- in_flight | recorded
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS callbacks (
    event_id    TEXT PRIMARY KEY,        -- 重复回调在此被挡下
    attempt_id  TEXT NOT NULL REFERENCES attempts(attempt_id),
    status      TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    raw_json    TEXT NOT NULL,
    received_at TEXT NOT NULL,
    applied     INTEGER NOT NULL,        -- 1 = 对状态机产生了效果
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_callbacks_attempt ON callbacks(attempt_id, received_at);

-- 每一次物理投递都留证（重复 event_id 也不丢），用于审计“没有被静默吞掉”
CREATE TABLE IF NOT EXISTS callback_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    received_at TEXT NOT NULL,
    dedup       TEXT NOT NULL            -- applied | duplicate_event | late_*
);
CREATE INDEX IF NOT EXISTS idx_deliveries_event ON callback_deliveries(event_id);

CREATE TABLE IF NOT EXISTS objects (
    sha256      TEXT PRIMARY KEY,        -- 仅收录通过校验的字节（候选区）
    media_type  TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id      TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL REFERENCES attempts(attempt_id),
    shot_id           TEXT NOT NULL REFERENCES shots(shot_id),
    sha256            TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    status            TEXT NOT NULL,
    quarantine_reason TEXT,
    event_id          TEXT,
    attributes_json   TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    UNIQUE(attempt_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_candidates_shot ON candidates(shot_id, status);

CREATE TABLE IF NOT EXISTS adoptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id    TEXT NOT NULL REFERENCES shots(shot_id),
    kind       TEXT NOT NULL,            -- adopt | replace | readopt
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    replaced_candidate_id TEXT REFERENCES candidates(candidate_id),
    reviewer   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    at         TEXT NOT NULL
);

-- 每个镜头至多一个生效采用；替换是先写历史再移动指针
CREATE TABLE IF NOT EXISTS active_adoptions (
    shot_id       TEXT PRIMARY KEY REFERENCES shots(shot_id),
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    adoption_id   INTEGER NOT NULL REFERENCES adoptions(id)
);

CREATE TABLE IF NOT EXISTS composites (
    product_id  TEXT PRIMARY KEY,
    shot_id     TEXT NOT NULL REFERENCES shots(shot_id),
    kind        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS composite_inputs (
    product_id   TEXT NOT NULL REFERENCES composites(product_id),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    attempt_id   TEXT NOT NULL REFERENCES attempts(attempt_id),
    role         TEXT NOT NULL,
    PRIMARY KEY (product_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_candidate ON composite_inputs(candidate_id);

CREATE TABLE IF NOT EXISTS changes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    ref_type  TEXT NOT NULL,
    ref_id    TEXT NOT NULL,
    shot_id   TEXT REFERENCES shots(shot_id),
    detail_json TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_ref ON changes(ref_type, ref_id);
"""


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("时间必须带 UTC 偏移")
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        (self.root / "quarantine").mkdir(exist_ok=True)
        self.db_path = self.root / "hub.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # -- 对象仓库 -----------------------------------------------------------

    def object_path(self, digest: str) -> Path:
        return self.root / "objects" / digest[:2] / digest

    def quarantine_path(self, digest: str) -> Path:
        return self.root / "quarantine" / digest[:2] / digest

    def has_object(self, digest: str) -> bool:
        return self.object_path(digest).exists()

    def write_bytes_atomic(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def read_object(self, digest: str) -> bytes:
        return self.object_path(digest).read_bytes()

    # -- 便捷读写 -----------------------------------------------------------

    def insert(self, table: str, **row: Any) -> None:
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)

    def query_one(self, sql: str, **params: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, **params: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))
