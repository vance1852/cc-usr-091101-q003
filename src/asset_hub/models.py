"""领域实体与枚举。

血缘链路::

    Shot ──< PromptPackage (versions) ──┐
      │                                ├─< GenerationAttempt (frozen input)
      └─< ReferenceImage (versions) ───┘            │
                                          dispatch (幂等键/计费)
                                                    │
                                            callbacks（信封）
                                                    │
                                          CandidateAsset（校验隔离）
                                                    │
                                          Adoption（采用/否决，留痕）
                                                    │
                                          CompositeProduct（下游合成）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssetKind(str, Enum):
    REFERENCE = "reference"
    IMAGE = "image"
    VIDEO = "video"
    COMPOSITE = "composite"


class AttemptStatus(str, Enum):
    PENDING = "pending"        # 已冻结输入，尚未派发
    DISPATCHED = "dispatched"  # 已拿到幂等任务号（已计费）
    SUCCEEDED = "succeeded"    # 成功终态：存在通过校验的候选
    FAILED = "failed"          # 失败终态
    DEAD_LETTER = "dead_letter"  # 成功但文件始终无法通过校验


class CandidateStatus(str, Enum):
    QUARANTINED = "quarantined"  # 校验未通过，隔离
    AVAILABLE = "available"      # 校验通过，进入候选区
    ADOPTED = "adopted"          # 已被采用为某镜头产物
    REJECTED = "rejected"        # 人工否决


class AdoptionKind(str, Enum):
    ADOPT = "adopt"
    REJECT = "reject"
    REPLACE = "replace"  # 用新候选替换已采用候选


class ChangeKind(str, Enum):
    REFERENCE_REPLACED = "reference_replaced"
    PROMPT_REVISED = "prompt_revised"
    CANDIDATE_REJECTED = "candidate_rejected"
    ADOPTION_REPLACED = "adoption_replaced"


@dataclass(frozen=True)
class Shot:
    shot_id: str
    code: str
    scene: str
    description: str


@dataclass
class PromptPackage:
    package_id: str
    shot_id: str
    version: int
    template: str
    variables: dict[str, Any]
    # 被废弃的服装提示词仍保留在变量历史中，但当前包版本不再引用
    deprecated: list[str] = field(default_factory=list)
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)

    def render(self) -> str:
        try:
            return self.template.format(**self.variables)
        except KeyError as exc:  # pragma: no cover - 装配阶段应拦截
            raise ValueError(f"提示词变量缺失: {exc.args[0]}") from exc


@dataclass
class ReferenceImage:
    reference_id: str
    shot_id: str | None  # None = 跨镜头共享的角色参考
    role: str           # 如 "hero_costume"
    version: int
    sha256: str
    media_type: str
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class FrozenInput:
    """一次尝试的冻结输入快照；其摘要派生幂等任务号。"""

    adapter: str
    prompt_package_id: str
    prompt_version: int
    prompt_sha256: str
    references: tuple[tuple[str, int, str], ...]  # (reference_id, version, sha256)
    parameters: dict[str, Any]

    def canonical_json(self) -> bytes:
        import json

        body = {
            "adapter": self.adapter,
            "prompt_package_id": self.prompt_package_id,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "references": [list(r) for r in sorted(self.references)],
            "parameters": self.parameters,
            # 键顺序固定，确保不同进程算出同一摘要
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class GenerationAttempt:
    attempt_id: str
    shot_id: str
    frozen_digest: str
    frozen: FrozenInput
    expected_media_type: str
    status: AttemptStatus
    created_at: datetime = field(default_factory=utc_now)
    dispatch_id: str | None = None       # 供应商幂等任务号
    dispatched_at: datetime | None = None
    attempts_made: int = 0               # 实际派发到供应商的次数（计费次数）
    terminal_at: datetime | None = None
    terminal_event_id: str | None = None
    failure_code: str | None = None


@dataclass
class CandidateAsset:
    candidate_id: str
    attempt_id: str
    shot_id: str
    sha256: str
    media_type: str
    status: CandidateStatus
    received_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    quarantine_reason: str | None = None
    event_id: str | None = None


@dataclass
class AdoptionRecord:
    id: int | None
    shot_id: str
    kind: AdoptionKind
    candidate_id: str
    replaced_candidate_id: str | None
    reviewer: str
    reason: str
    at: datetime


@dataclass
class ChangeRecord:
    id: int | None
    kind: ChangeKind
    ref_type: str          # "reference" | "prompt_package" | "candidate"
    ref_id: str
    shot_id: str | None    # None 时影响面通过引用关系展开
    detail: dict[str, Any]
    at: datetime
