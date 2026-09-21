"""可替换的生成供应商适配器。

资产中枢只依赖 :class:`ProviderAdapter` 协议；真实模型（或计费网关）
实现该协议即可接入，测试与验收演示使用确定性的 :class:`FakeProvider`。

计费约定：供应商按 ``idempotency_key`` 去重——同一键重复提交必须返回
同一个任务句柄且不得重复计费，这是崩溃恢复不重复扣费的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .ids import sha256_hex
from .models import iso, utcnow


@dataclass(frozen=True)
class SubmitRequest:
    attempt_id: str
    idempotency_key: str
    frozen_inputs: dict[str, Any]
    timeout_seconds: int


@dataclass(frozen=True)
class ProviderJob:
    provider_job_id: str
    file_handle: str
    attempt_id: str


class ProviderAdapter(Protocol):
    """生成供应商适配器协议。"""

    name: str

    def submit(self, request: SubmitRequest) -> ProviderJob:
        """提交生成请求；同一 idempotency_key 重复提交返回同一任务且不重复计费。"""
        ...

    def poll(self, provider_job_id: str) -> dict[str, Any] | None:
        """回收失联任务的结果：返回回调信封（原始 dict），无结果返回 None。"""
        ...

    def fetch(self, file_handle: str) -> bytes:
        """取回供应商文件字节。"""
        ...


@dataclass(frozen=True)
class PlannedResult:
    """FakeProvider 的剧本：某个幂等键应当产生的结局。"""

    status: str  # "succeeded" | "failed" | "silent"（永不完成，用于超时演练）
    file_bytes: bytes | None = None
    declared_sha256: str | None = None  # 缺省取文件真实摘要；可故意填错模拟摘要不符
    declared_format: str = "png"
    error_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class FakeProvider:
    """确定性内存供应商：记录计费流水、按剧本产出回调与文件。"""

    def __init__(self, name: str = "fake", clock=utcnow):
        self.name = name
        self._clock = clock
        self.submit_calls = 0
        self.charges: list[str] = []  # 每个幂等键只计费一次
        self.jobs: dict[str, ProviderJob] = {}
        self._by_idem: dict[str, ProviderJob] = {}
        self._plans: dict[str, PlannedResult] = {}
        self._files: dict[str, bytes] = {}

    # -- 剧本 -----------------------------------------------------------
    def plan(self, idempotency_key: str, result: PlannedResult) -> None:
        self._plans[idempotency_key] = result
        job = self._by_idem.get(idempotency_key)
        if job is not None and result.status == "succeeded":
            # 允许先提交后写剧本：补登文件，等价于供应商异步出图
            self._files[job.file_handle] = result.file_bytes or b""

    # -- 适配器接口 ------------------------------------------------------
    def submit(self, request: SubmitRequest) -> ProviderJob:
        self.submit_calls += 1
        existing = self._by_idem.get(request.idempotency_key)
        if existing is not None:
            return existing  # 幂等重提：返回原任务，不重复计费
        job = ProviderJob(
            provider_job_id=f"{self.name}-job-{len(self.jobs) + 1}",
            file_handle=f"{self.name}-file-{len(self.jobs) + 1}",
            attempt_id=request.attempt_id,
        )
        self.jobs[job.provider_job_id] = job
        self._by_idem[request.idempotency_key] = job
        self.charges.append(request.idempotency_key)
        plan = self._plans.get(request.idempotency_key, PlannedResult(status="silent"))
        if plan.status == "succeeded":
            self._files[job.file_handle] = plan.file_bytes or b""
        return job

    def poll(self, provider_job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(provider_job_id)
        if job is None:
            return None
        plan = self._plans.get(self._idem_of(job), PlannedResult(status="silent"))
        if plan.status == "silent":
            return None
        return self._callback_for(job, plan)

    def fetch(self, file_handle: str) -> bytes:
        return self._files[file_handle]

    # -- 回调构造 ---------------------------------------------------------
    def make_callback(self, attempt_id: str, occurred_at: datetime | None = None) -> dict[str, Any]:
        """按剧本为某次尝试生成回调信封（与 poll 回收的信封同 event_id，天然去重）。"""
        job = next(j for j in self.jobs.values() if j.attempt_id == attempt_id)
        plan = self._plans.get(self._idem_of(job), PlannedResult(status="silent"))
        return self._callback_for(job, plan, occurred_at=occurred_at)

    # -- 内部 -------------------------------------------------------------
    def _idem_of(self, job: ProviderJob) -> str:
        return next(k for k, v in self._by_idem.items() if v is job)

    def _callback_for(
        self, job: ProviderJob, plan: PlannedResult, occurred_at: datetime | None = None
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "event_id": f"evt-{job.provider_job_id}",
            "attempt_id": job.attempt_id,
            "status": plan.status,
            "occurred_at": iso(occurred_at or self._clock()),
        }
        if plan.status == "succeeded":
            data = self._files.get(job.file_handle, b"")
            envelope["asset_sha256"] = plan.declared_sha256 or sha256_hex(data)
            envelope["format"] = plan.declared_format
            envelope["file"] = job.file_handle
            envelope.update(plan.extra)
        elif plan.error_code:
            envelope["error_code"] = plan.error_code
        return envelope
