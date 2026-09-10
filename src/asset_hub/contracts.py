"""生成供应商回调的最小领域格式。"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class CallbackStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderCallback:
    event_id: str
    attempt_id: str
    status: CallbackStatus
    occurred_at: datetime
    asset_sha256: str | None
    attributes: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderCallback":
        required = {"event_id", "attempt_id", "status", "occurred_at"}
        if required - raw.keys():
            raise ValueError("回调缺少必需字段")
        status = CallbackStatus(raw["status"])
        occurred = datetime.fromisoformat(str(raw["occurred_at"]).replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            raise ValueError("occurred_at 必须包含 UTC 偏移")
        digest = raw.get("asset_sha256")
        if status is CallbackStatus.SUCCEEDED and not isinstance(digest, str):
            raise ValueError("成功回调必须声明资产摘要")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("资产摘要必须是 SHA-256")
        return cls(str(raw["event_id"]), str(raw["attempt_id"]), status, occurred, digest, {k: v for k, v in raw.items() if k not in required | {"asset_sha256"}})
