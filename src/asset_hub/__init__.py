"""分镜生成资产中枢。

血缘主线：镜头清单 → 版本化提示词包/参考图 → 幂等生成任务 → 尝试
→ 回调收件箱 → 候选资产 → 人工批注 → 采用关系 → 合成产物。
"""

from .adapters import FakeProvider, PlannedResult, ProviderAdapter, ProviderJob, SubmitRequest
from .contracts import CallbackStatus, ProviderCallback
from .errors import HubError
from .hub import AssetHub
from .models import (
    AnnotationAction,
    AssetState,
    AttemptStatus,
    ChangeKind,
    TargetKind,
    TaskStatus,
)
from .store import Store
from .verify import sniff_format, verify_payload

__all__ = [
    "AnnotationAction",
    "AssetHub",
    "AssetState",
    "AttemptStatus",
    "CallbackStatus",
    "ChangeKind",
    "FakeProvider",
    "HubError",
    "PlannedResult",
    "ProviderAdapter",
    "ProviderCallback",
    "ProviderJob",
    "Store",
    "SubmitRequest",
    "TargetKind",
    "TaskStatus",
    "sniff_format",
    "verify_payload",
]
