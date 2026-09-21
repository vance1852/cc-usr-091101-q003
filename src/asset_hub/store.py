"""SQLite 持久化层：所有血缘与状态都落在单库事务里。

回调事件先落库、后处理（收件箱模式），进程在任意点中断都能恢复；
尝试的提交与计费标记同事务写入，恢复时按幂等键重提而不重复计费。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    shot_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    description TEXT NOT NULL,
    characters_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS prompt_packs (
    pack_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (pack_id, version)
);

CREATE TABLE IF NOT EXISTS reference_images (
    ref_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ref_id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,          -- 幂等任务号：冻结输入的摘要
    shot_id TEXT NOT NULL,
    frozen_json TEXT NOT NULL,         -- 冻结的输入版本快照
    status TEXT NOT NULL,              -- OPEN / RESOLVED
    winning_attempt_id TEXT,           -- 唯一有效结果来自哪次尝试
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_job_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,  -- 供应商侧去重键
    status TEXT NOT NULL,
    declared_sha256 TEXT,
    declared_format TEXT,
    deadline_at TEXT,
    submitted_at TEXT,
    settled_at TEXT,
    billed INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS callback_events (
    n INTEGER PRIMARY KEY AUTOINCREMENT,   -- 到达顺序，恢复时按此重放
    event_id TEXT NOT NULL UNIQUE,         -- 重复回调按此去重
    attempt_id TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,                     -- NULL = 待处理（崩溃恢复点）
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    asset_id TEXT PRIMARY KEY,         -- 内容寻址
    attempt_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    state TEXT NOT NULL,               -- CANDIDATE / APPROVED / REJECTED
    valid_for_task INTEGER NOT NULL,   -- 1 = 任务的唯一有效结果
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    declared_sha256 TEXT,
    declared_format TEXT,
    actual_sha256 TEXT,
    actual_format TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    action TEXT NOT NULL,              -- APPROVE / REJECT / COMMENT
    author TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adoptions (
    adoption_id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    adopted_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    adopted_at TEXT NOT NULL,
    superseded_by TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS composites (
    composite_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    built_from_json TEXT NOT NULL,     -- [{shot_id, asset_id, task_id, adoption_id}]
    built_by TEXT NOT NULL,
    built_at TEXT NOT NULL,
    PRIMARY KEY (composite_id, version)
);

CREATE TABLE IF NOT EXISTS change_events (
    change_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                -- REFERENCE_REPLACED / PROMPT_RETIRED / ASSET_REJECTED
    subject TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,         -- SHOT / COMPOSITE
    target_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    invalidated_at TEXT NOT NULL,
    UNIQUE (change_id, target_kind, target_id)
);
"""


class Store:
    """薄封装：可重入事务 + 行访问。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """可重入写事务：最外层提交，内层共享同一事务。"""
        if self._depth > 0:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._depth = 0

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(row.values()))

    def insert_or_ignore(self, table: str, row: dict[str, Any]) -> bool:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})", tuple(row.values())
        )
        return cur.rowcount > 0

    def update(self, table: str, row: dict[str, Any], where: str, params: tuple = ()) -> None:
        assignments = ", ".join(f"{k} = ?" for k in row)
        self.conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}", tuple(row.values()) + tuple(params))

    def close(self) -> None:
        self.conn.close()
