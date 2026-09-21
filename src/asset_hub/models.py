"""资产中枢的领域状态机与时间约定。

所有持久化时间均为带 UTC 偏移的 ISO-8601 字符串（微秒定宽），
保证字典序与时间序一致，可直接用于 SQL 比较。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class TaskStatus(str, Enum):
    """生成任务状态：一个任务至多产生一份有效结果。"""

    OPEN = "OPEN"  # 尚无有效结果
    RESOLVED = "RESOLVED"  # 已有唯一有效结果（winning_attempt_id）


class AttemptStatus(str, Enum):
    """单次生成尝试状态。"""

    PENDING_SUBMIT = "PENDING_SUBMIT"  # 已建单、尚未成功提交（崩溃恢复点）
    SUBMITTED = "SUBMITTED"  # 已提交并计费，等待回调
    SUCCEEDED = "SUCCEEDED"  # 成功且文件通过核验，是任务的有效结果
    SUPERSEDED = "SUPERSEDED"  # 成功但任务已有有效结果，不再取用
    FAILED = "FAILED"  # 供应商失败回调
    TIMED_OUT = "TIMED_OUT"  # 本地超时判定
    FAILED_VERIFICATION = "FAILED_VERIFICATION"  # 摘要或声明格式不符


#: 终态尝试集合：到达后任何迟到/重复回调都不再改变它
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.SUPERSEDED,
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.FAILED_VERIFICATION,
    }
)


class AssetState(str, Enum):
    """候选资产治理状态（由人工批注驱动）。"""

    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AnnotationAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    COMMENT = "COMMENT"


class ChangeKind(str, Enum):
    """触发返工计算的变更类型。"""

    REFERENCE_REPLACED = "REFERENCE_REPLACED"  # 角色参考图被替换
    PROMPT_RETIRED = "PROMPT_RETIRED"  # 提示词包版本被废弃
    ASSET_REJECTED = "ASSET_REJECTED"  # 资产被否决


class TargetKind(str, Enum):
    """受影响对象类型。"""

    SHOT = "SHOT"
    COMPOSITE = "COMPOSITE"


def iso(dt: datetime) -> str:
    """格式化为定宽 UTC ISO 字符串；拒绝朴素时间。"""
    if dt.tzinfo is None:
        raise ValueError("时间必须带 UTC 偏移")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    """解析 ISO 字符串并归一到 UTC。"""
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
