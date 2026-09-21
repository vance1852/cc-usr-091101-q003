"""幂等标识派生：任务号由冻结输入的摘要决定。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序、无空白，保证同一输入得到同一摘要。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_id_for(frozen_inputs: dict[str, Any]) -> str:
    """幂等任务号：同一组冻结输入永远得到同一个任务号。"""
    return "task-" + sha256_hex(canonical_json(frozen_inputs).encode("utf-8"))[:20]


def attempt_id_for(task_id: str, seq: int) -> str:
    return f"{task_id}-a{seq}"


def asset_id_for(sha256: str) -> str:
    """候选资产按内容寻址：相同字节永远是同一资产。"""
    return "asset-" + sha256[:16]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
