"""可替换的生成供应商适配器。

中枢不直接调用真实模型；它只依赖 :class:`GenerationAdapter` 协议：
用冻结输入+供应商幂等键换取一个 dispatch_id（任务号）。回调由供应商
异步投递（见 :mod:`asset_hub.inbox`），适配器也可用于拉取结果。

注册新供应商::

    @register_adapter("wan-video")
    class WanAdapter(GenerationAdapter): ...
"""

from __future__ import annotations

import random
from typing import Protocol, runtime_checkable

from .models import FrozenInput


class AdapterError(RuntimeError):
    """供应商侧瞬时故障：调用方应在不更换幂等键的前提下重试。"""


class DispatchRejected(RuntimeError):
    """供应商拒绝请求（参数不合法等），重试无意义。"""


@runtime_checkable
class GenerationAdapter(Protocol):
    name: str

    def dispatch(self, frozen: FrozenInput, idempotency_key: str) -> str:
        """提交任务，返回供应商任务号。

        同一 ``idempotency_key`` 的重复提交必须返回同一任务号，
        且不得重复计费——真实适配器应把它映射到供应商的幂等请求头。
        """
        ...


_ADAPTERS: dict[str, type] = {}


def register_adapter(name: str):
    def deco(cls):
        cls.name = name
        _ADAPTERS[name] = cls
        return cls

    return deco


def get_adapter_class(name: str) -> type:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(f"未注册的适配器: {name!r}，可用: {sorted(_ADAPTERS)}") from None


def available_adapters() -> list[str]:
    return sorted(_ADAPTERS)


@register_adapter("echo-image")
class EchoImageAdapter:
    """确定性本地适配器：相同冻结输入返回相同任务号，不触网。"""

    name = "echo-image"

    def dispatch(self, frozen: FrozenInput, idempotency_key: str) -> str:
        # 真实供应商会保存幂等键；这里直接派生任务号并对同一键保持稳定。
        return f"echo-{idempotency_key[:16]}"


@register_adapter("flaky-video")
class FlakyVideoAdapter:
    """模拟超时/5xx 的适配器，用于验证同键重试不重复计费。

    每个 *新进程/新实例* 的前 ``fail_times`` 次**新键**请求抛
    :class:`AdapterError``；任务号由幂等键确定性派生——即使进程重启
    换成全新实例，同一键也永远解析到同一任务号（模拟供应商服务端去重），
    因此计费任务只有一个，``calls_made`` 只反映传输层尝试次数。
    """

    name = "flaky-video"

    def __init__(self, fail_times: int = 1, seed: int = 7):
        self.fail_times = fail_times
        self._seen: dict[str, str] = {}
        self._new_calls = 0
        self._rng = random.Random(seed)

    def dispatch(self, frozen: FrozenInput, idempotency_key: str) -> str:
        if idempotency_key in self._seen:
            # 本实例已见过此键：直接返回原任务号，不产生第二笔费用
            return self._seen[idempotency_key]
        self._new_calls += 1
        if self._new_calls <= self.fail_times:
            raise AdapterError("TIMEOUT")
        # 任务号对键确定：重启后新实例算出同一结果
        digest = idempotency_key.encode().hex()[:8]
        dispatch_id = f"flaky-{idempotency_key[5:17]}-{digest}"
        self._seen[idempotency_key] = dispatch_id
        return dispatch_id
