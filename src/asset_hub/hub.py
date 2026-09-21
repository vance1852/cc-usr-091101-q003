"""分镜生成资产中枢：血缘、幂等、核验、治理与恢复的全部业务规则。

核心不变量：
1. 同一组冻结输入永远得到同一个幂等任务号，重复提交不产生新尝试、不重复计费。
2. 每个任务至多一份有效结果：超时重试、重复回调、先到的失败通知都只被记录。
3. 供应商文件只有通过摘要与声明格式核验才进入候选区，否则进隔离区。
4. 已批准/已采用的资产从不被系统自动覆盖；重新采用必须留下人和理由。
5. 回调先落库后处理；进程中断后 recover() 重放收件箱、按幂等键重提，
   既不丢回调也不重复计费。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Callable, Iterable

from .adapters import ProviderAdapter, SubmitRequest
from .contracts import CallbackStatus, ProviderCallback
from .errors import HubError
from .ids import asset_id_for, attempt_id_for, canonical_json, new_id, task_id_for
from .models import (
    TERMINAL_ATTEMPT_STATUSES,
    AnnotationAction,
    AssetState,
    AttemptStatus,
    ChangeKind,
    TargetKind,
    TaskStatus,
    iso,
    utcnow,
)
from .store import Store
from .verify import verify_payload

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class AssetHub:
    def __init__(
        self,
        store: Store,
        providers: dict[str, ProviderAdapter] | None = None,
        clock: Callable[[], Any] = utcnow,
        auto_retry_on_timeout: bool = True,
        default_timeout_seconds: int = 300,
    ):
        self.store = store
        self.providers = dict(providers or {})
        self.clock = clock
        self.auto_retry_on_timeout = auto_retry_on_timeout
        self.default_timeout_seconds = default_timeout_seconds

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return iso(self.clock())

    def _adapter(self, name: str) -> ProviderAdapter:
        try:
            return self.providers[name]
        except KeyError:
            raise HubError(f"未注册的供应商适配器: {name}") from None

    # ------------------------------------------------------------------
    # 镜头清单与版本化输入
    # ------------------------------------------------------------------
    def import_shots(self, shots: Iterable[dict[str, Any]]) -> int:
        """导入镜头清单（镜头拆解结果）。"""
        count = 0
        with self.store.tx():
            for raw in shots:
                missing = {"shot_id", "scene_id", "description"} - raw.keys()
                if missing:
                    raise HubError(f"镜头缺少必需字段: {sorted(missing)}")
                self.store.insert_or_ignore(
                    "shots",
                    {
                        "shot_id": str(raw["shot_id"]),
                        "scene_id": str(raw["scene_id"]),
                        "description": str(raw["description"]),
                        "characters_json": json.dumps(raw.get("characters", []), ensure_ascii=False),
                        "meta_json": json.dumps(raw.get("meta", {}), ensure_ascii=False),
                    },
                )
                count += 1
        return count

    def create_prompt_pack(self, pack_id: str, content: dict[str, Any], actor: str) -> dict[str, Any]:
        return self._insert_pack(pack_id, 1, content, actor)

    def revise_prompt_pack(self, pack_id: str, content: dict[str, Any], actor: str) -> dict[str, Any]:
        latest = self.store.one(
            "SELECT MAX(version) AS v FROM prompt_packs WHERE pack_id = ?", (pack_id,)
        )
        if latest is None or latest["v"] is None:
            raise HubError(f"提示词包不存在: {pack_id}")
        return self._insert_pack(pack_id, latest["v"] + 1, content, actor)

    def _insert_pack(self, pack_id: str, version: int, content: dict[str, Any], actor: str) -> dict[str, Any]:
        row = {
            "pack_id": pack_id,
            "version": version,
            "content_json": canonical_json(content),
            "retired": 0,
            "created_by": actor,
            "created_at": self._now(),
        }
        with self.store.tx():
            if self.store.one("SELECT 1 AS x FROM prompt_packs WHERE pack_id=? AND version=?", (pack_id, version)):
                raise HubError(f"提示词包版本已存在: {pack_id}@{version}")
            self.store.insert("prompt_packs", row)
        return row

    def register_reference(self, ref_id: str, role: str, sha256: str, media_type: str, actor: str) -> dict[str, Any]:
        return self._insert_reference(ref_id, 1, role, sha256, media_type, actor)

    def revise_reference(self, ref_id: str, sha256: str, media_type: str, actor: str) -> dict[str, Any]:
        latest = self.store.one(
            "SELECT * FROM reference_images WHERE ref_id = ? ORDER BY version DESC LIMIT 1", (ref_id,)
        )
        if latest is None:
            raise HubError(f"参考图不存在: {ref_id}")
        return self._insert_reference(ref_id, latest["version"] + 1, latest["role"], sha256, media_type, actor)

    def _insert_reference(
        self, ref_id: str, version: int, role: str, sha256: str, media_type: str, actor: str
    ) -> dict[str, Any]:
        if not _SHA256_RE.fullmatch(sha256):
            raise HubError("参考图摘要必须是小写十六进制 SHA-256")
        row = {
            "ref_id": ref_id,
            "version": version,
            "role": role,
            "sha256": sha256,
            "media_type": media_type,
            "superseded": 0,
            "created_by": actor,
            "created_at": self._now(),
        }
        with self.store.tx():
            if self.store.one(
                "SELECT 1 AS x FROM reference_images WHERE ref_id=? AND version=?", (ref_id, version)
            ):
                raise HubError(f"参考图版本已存在: {ref_id}@{version}")
            self.store.insert("reference_images", row)
        return row

    # ------------------------------------------------------------------
    # 提交生成：冻结输入 + 幂等任务号
    # ------------------------------------------------------------------
    def submit_generation(
        self,
        shot_id: str,
        pack_id: str,
        pack_version: int,
        references: list[tuple[str, int]],
        provider: str,
        params: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """提交一次生成。相同冻结输入重复提交返回既有任务，不产生新尝试。"""
        self._adapter(provider)
        if not self.store.one("SELECT 1 AS x FROM shots WHERE shot_id = ?", (shot_id,)):
            raise HubError(f"镜头不存在: {shot_id}")
        pack = self.store.one(
            "SELECT * FROM prompt_packs WHERE pack_id = ? AND version = ?", (pack_id, pack_version)
        )
        if pack is None:
            raise HubError(f"提示词包版本不存在: {pack_id}@{pack_version}")
        if pack["retired"]:
            raise HubError(f"提示词包版本已废弃，禁止用于新任务: {pack_id}@{pack_version}")
        frozen_refs = []
        for ref_id, version in sorted(references):
            ref = self.store.one(
                "SELECT * FROM reference_images WHERE ref_id = ? AND version = ?", (ref_id, version)
            )
            if ref is None:
                raise HubError(f"参考图版本不存在: {ref_id}@{version}")
            if ref["superseded"]:
                raise HubError(f"参考图版本已被替换，禁止用于新任务: {ref_id}@{version}")
            frozen_refs.append(
                {"ref_id": ref_id, "version": version, "role": ref["role"], "sha256": ref["sha256"]}
            )
        frozen = {
            "shot_id": shot_id,
            "prompt_pack": {"pack_id": pack_id, "version": pack_version},
            "references": frozen_refs,
            "provider": provider,
            "params": params or {},
        }
        task_id = task_id_for(frozen)
        existing = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if existing is not None:
            return {"task_id": task_id, "created": False, "attempt_id": None, "status": existing["status"]}

        attempt_id = attempt_id_for(task_id, 1)
        with self.store.tx():
            self.store.insert(
                "tasks",
                {
                    "task_id": task_id,
                    "shot_id": shot_id,
                    "frozen_json": canonical_json(frozen),
                    "status": TaskStatus.OPEN.value,
                    "winning_attempt_id": None,
                    "created_by": actor,
                    "created_at": self._now(),
                },
            )
            self._insert_attempt(task_id, attempt_id, 1, provider, {"actor": actor})
        self._dispatch(attempt_id)
        return {"task_id": task_id, "created": True, "attempt_id": attempt_id, "status": TaskStatus.OPEN.value}

    def _insert_attempt(self, task_id: str, attempt_id: str, seq: int, provider: str, detail: dict[str, Any]) -> None:
        self.store.insert(
            "attempts",
            {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "seq": seq,
                "provider": provider,
                "provider_job_id": None,
                "idempotency_key": attempt_id,
                "status": AttemptStatus.PENDING_SUBMIT.value,
                "declared_sha256": None,
                "declared_format": None,
                "deadline_at": None,
                "submitted_at": None,
                "settled_at": None,
                "billed": 0,
                "detail_json": json.dumps(detail, ensure_ascii=False),
            },
        )

    def _dispatch(self, attempt_id: str) -> bool:
        """提交到供应商。崩溃若发生在提交与落库之间，恢复流程会用同一幂等键重提。"""
        attempt = self.store.one("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))
        if attempt is None:
            raise HubError(f"尝试不存在: {attempt_id}")
        if attempt["status"] != AttemptStatus.PENDING_SUBMIT.value:
            return False
        task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (attempt["task_id"],))
        frozen = json.loads(task["frozen_json"])
        timeout = int(frozen.get("params", {}).get("timeout_seconds", self.default_timeout_seconds))
        adapter = self._adapter(attempt["provider"])
        job = adapter.submit(
            SubmitRequest(
                attempt_id=attempt_id,
                idempotency_key=attempt["idempotency_key"],
                frozen_inputs=frozen,
                timeout_seconds=timeout,
            )
        )
        now = self.clock()
        with self.store.tx():
            self.store.update(
                "attempts",
                {
                    "provider_job_id": job.provider_job_id,
                    "status": AttemptStatus.SUBMITTED.value,
                    "submitted_at": iso(now),
                    "deadline_at": iso(now + timedelta(seconds=timeout)),
                    "billed": 1,
                },
                "attempt_id = ?",
                (attempt_id,),
            )
        return True

    def retry_task(self, task_id: str, actor: str, reason: str, force: bool = False) -> str:
        """在同一幂等任务号下发起下一次尝试（超时/失败后的重试）。"""
        task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if task is None:
            raise HubError(f"任务不存在: {task_id}")
        if task["status"] != TaskStatus.OPEN.value:
            raise HubError(f"任务已有有效结果，无需重试: {task_id}")
        attempts = self.store.all(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task_id,)
        )
        latest = attempts[-1]
        if latest["status"] not in {s.value for s in TERMINAL_ATTEMPT_STATUSES} and not force:
            raise HubError(f"最新尝试 {latest['attempt_id']} 仍在进行，不能重试")
        attempt_id = attempt_id_for(task_id, latest["seq"] + 1)
        with self.store.tx():
            self._insert_attempt(
                task_id, attempt_id, latest["seq"] + 1, latest["provider"],
                {"actor": actor, "retry_reason": reason},
            )
        self._dispatch(attempt_id)
        return attempt_id

    def sweep_timeouts(self) -> list[str]:
        """把超过截止时间的在途尝试判为超时，并按策略自动重试。"""
        now = self._now()
        stale = self.store.all(
            "SELECT * FROM attempts WHERE status = ? AND deadline_at IS NOT NULL AND deadline_at < ?",
            (AttemptStatus.SUBMITTED.value, now),
        )
        retried: list[str] = []
        with self.store.tx():
            for attempt in stale:
                self.store.update(
                    "attempts",
                    {"status": AttemptStatus.TIMED_OUT.value, "settled_at": now},
                    "attempt_id = ?",
                    (attempt["attempt_id"],),
                )
        if self.auto_retry_on_timeout:
            for attempt in stale:
                task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (attempt["task_id"],))
                if task["status"] == TaskStatus.OPEN.value:
                    retried.append(self.retry_task(task["task_id"], actor="system", reason="超时自动重试"))
        return [a["attempt_id"] for a in stale]

    # ------------------------------------------------------------------
    # 回调收件箱：先落库、去重、按到达顺序处理
    # ------------------------------------------------------------------
    def receive_callback(self, raw: dict[str, Any]) -> dict[str, Any]:
        """接收回调信封：契约校验后原样落库。重复 event_id 直接判重。"""
        callback = ProviderCallback.from_dict(raw)  # 契约校验（必需字段/时区/摘要格式）
        with self.store.tx():
            if self.store.one("SELECT 1 AS x FROM callback_events WHERE event_id = ?", (callback.event_id,)):
                return {"event_id": callback.event_id, "duplicate": True}
            self.store.insert(
                "callback_events",
                {
                    "event_id": callback.event_id,
                    "attempt_id": callback.attempt_id,
                    "status": callback.status.value,
                    "occurred_at": iso(callback.occurred_at),
                    "payload_json": json.dumps(raw, ensure_ascii=False),
                    "received_at": self._now(),
                    "processed_at": None,
                    "outcome": None,
                },
            )
        return {"event_id": callback.event_id, "duplicate": False}

    def ingest_callback(self, raw: dict[str, Any]) -> dict[str, Any]:
        """接收并立即处理（含积压事件），返回该事件的处理结果。"""
        receipt = self.receive_callback(raw)
        outcomes = self.process_pending_events()
        outcome = next((o for o in outcomes if o["event_id"] == receipt["event_id"]), None)
        return {**receipt, "outcome": outcome["outcome"] if outcome else "DUPLICATE_EVENT"}

    def process_pending_events(self) -> list[dict[str, str]]:
        """按到达顺序处理所有未处理事件；每个事件独立事务，崩溃安全。"""
        outcomes: list[dict[str, str]] = []
        while True:
            event = self.store.one(
                "SELECT * FROM callback_events WHERE processed_at IS NULL ORDER BY n LIMIT 1"
            )
            if event is None:
                return outcomes
            outcome = self._process_event(event)
            outcomes.append({"event_id": event["event_id"], "outcome": outcome})

    def _process_event(self, event: dict[str, Any]) -> str:
        payload = json.loads(event["payload_json"])
        callback = ProviderCallback.from_dict(payload)
        attempt = self.store.one("SELECT * FROM attempts WHERE attempt_id = ?", (callback.attempt_id,))

        def finish(outcome: str) -> str:
            with self.store.tx():
                self.store.update(
                    "callback_events",
                    {"processed_at": self._now(), "outcome": outcome},
                    "event_id = ?",
                    (event["event_id"],),
                )
            return outcome

        if attempt is None:
            return finish("UNKNOWN_ATTEMPT")
        if attempt["status"] in {s.value for s in TERMINAL_ATTEMPT_STATUSES}:
            # 重复回调、迟到回调、先到的失败通知：只记录，不再改变终态
            return finish(f"IGNORED_{attempt['status']}")

        if callback.status is CallbackStatus.FAILED:
            detail = json.loads(attempt["detail_json"])
            detail["error_code"] = callback.attributes.get("error_code")
            with self.store.tx():
                self.store.update(
                    "attempts",
                    {
                        "status": AttemptStatus.FAILED.value,
                        "settled_at": iso(callback.occurred_at),
                        "detail_json": json.dumps(detail, ensure_ascii=False),
                    },
                    "attempt_id = ?",
                    (attempt["attempt_id"],),
                )
            return finish("ATTEMPT_FAILED")

        # 成功回调：任务已有有效结果则本次成功降级为 SUPERSEDED，不再取文件
        task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (attempt["task_id"],))
        if task["status"] == TaskStatus.RESOLVED.value:
            with self.store.tx():
                self.store.update(
                    "attempts",
                    {"status": AttemptStatus.SUPERSEDED.value, "settled_at": iso(callback.occurred_at)},
                    "attempt_id = ?",
                    (attempt["attempt_id"],),
                )
            return finish("SUPERSEDED")

        adapter = self._adapter(attempt["provider"])
        handle = callback.attributes.get("file") or attempt["provider_job_id"]
        data = adapter.fetch(handle)  # 取件失败则事件保持未处理，留待恢复重试
        declared_format = callback.attributes.get("format")
        verdict = verify_payload(data, callback.asset_sha256, declared_format)

        if not verdict.ok:
            with self.store.tx():
                self.store.update(
                    "attempts",
                    {
                        "status": AttemptStatus.FAILED_VERIFICATION.value,
                        "declared_sha256": callback.asset_sha256,
                        "declared_format": declared_format,
                        "settled_at": iso(callback.occurred_at),
                    },
                    "attempt_id = ?",
                    (attempt["attempt_id"],),
                )
                self.store.insert(
                    "quarantine",
                    {
                        "quarantine_id": new_id("qua"),
                        "attempt_id": attempt["attempt_id"],
                        "declared_sha256": callback.asset_sha256,
                        "declared_format": declared_format,
                        "actual_sha256": verdict.actual_sha256,
                        "actual_format": verdict.actual_format,
                        "reason": verdict.reason,
                        "created_at": self._now(),
                    },
                )
            return finish("QUARANTINED")

        asset_id = asset_id_for(verdict.actual_sha256)
        with self.store.tx():
            self.store.update(
                "attempts",
                {
                    "status": AttemptStatus.SUCCEEDED.value,
                    "declared_sha256": callback.asset_sha256,
                    "declared_format": declared_format,
                    "settled_at": iso(callback.occurred_at),
                    "detail_json": json.dumps(
                        {**json.loads(attempt["detail_json"]), "attributes": callback.attributes},
                        ensure_ascii=False,
                    ),
                },
                "attempt_id = ?",
                (attempt["attempt_id"],),
            )
            created = self.store.insert_or_ignore(
                "candidates",
                {
                    "asset_id": asset_id,
                    "attempt_id": attempt["attempt_id"],
                    "task_id": attempt["task_id"],
                    "shot_id": task["shot_id"],
                    "sha256": verdict.actual_sha256,
                    "media_type": verdict.actual_format,
                    "bytes": len(data),
                    "state": AssetState.CANDIDATE.value,
                    "valid_for_task": 1,
                    "created_at": self._now(),
                },
            )
            self.store.update(
                "tasks",
                {"status": TaskStatus.RESOLVED.value, "winning_attempt_id": attempt["attempt_id"]},
                "task_id = ?",
                (task["task_id"],),
            )
        return finish("ACCEPTED" if created else "ACCEPTED_KNOWN_CONTENT")

    # ------------------------------------------------------------------
    # 人工批注与采用（治理）
    # ------------------------------------------------------------------
    def annotate(self, asset_id: str, actor: str, action: str, reason: str = "") -> dict[str, Any]:
        """人工批注。批准/否决必须给出理由；否决会触发返工计算。"""
        asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (asset_id,))
        if asset is None:
            raise HubError(f"资产不存在: {asset_id}")
        action_enum = AnnotationAction(action)
        if action_enum is not AnnotationAction.COMMENT and not reason:
            raise HubError("批准或否决必须给出理由")
        if not actor:
            raise HubError("批注必须记录操作人")
        row = {
            "annotation_id": new_id("ann"),
            "asset_id": asset_id,
            "action": action_enum.value,
            "author": actor,
            "reason": reason,
            "created_at": self._now(),
        }
        with self.store.tx():
            self.store.insert("annotations", row)
            if action_enum is AnnotationAction.APPROVE:
                self.store.update(
                    "candidates", {"state": AssetState.APPROVED.value}, "asset_id = ?", (asset_id,)
                )
            elif action_enum is AnnotationAction.REJECT:
                self.store.update(
                    "candidates", {"state": AssetState.REJECTED.value}, "asset_id = ?", (asset_id,)
                )
                self._apply_change(
                    kind=ChangeKind.ASSET_REJECTED,
                    subject=asset_id,
                    detail={"asset_id": asset_id},
                    actor=actor,
                    reason=reason,
                    rejected_assets={asset_id},
                )
        return row

    def adopt(self, shot_id: str, asset_id: str, actor: str, reason: str) -> dict[str, Any]:
        """采用资产。重新采用会保留历史，且必须留下明确的人和理由。"""
        if not actor or not reason:
            raise HubError("采用必须记录操作人和理由")
        if not self.store.one("SELECT 1 AS x FROM shots WHERE shot_id = ?", (shot_id,)):
            raise HubError(f"镜头不存在: {shot_id}")
        asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (asset_id,))
        if asset is None:
            raise HubError(f"资产不存在: {asset_id}")
        if asset["state"] != AssetState.APPROVED.value:
            raise HubError(f"只有已批准的资产才能采用（当前状态 {asset['state']}）")
        current = self.store.one(
            "SELECT * FROM adoptions WHERE shot_id = ? AND active = 1", (shot_id,)
        )
        if current and current["asset_id"] == asset_id and not self._pending_rejection(shot_id, asset_id):
            return current  # 幂等：重复采用同一资产不产生新记录
        # 若该资产曾被否决：重新批准后的再次采用必须落成新记录，留下人和理由
        adoption_id = new_id("ado")
        row = {
            "adoption_id": adoption_id,
            "shot_id": shot_id,
            "asset_id": asset_id,
            "adopted_by": actor,
            "reason": reason,
            "adopted_at": self._now(),
            "superseded_by": None,
            "active": 1,
        }
        with self.store.tx():
            if current:
                self.store.update(
                    "adoptions",
                    {"active": 0, "superseded_by": adoption_id},
                    "adoption_id = ?",
                    (current["adoption_id"],),
                )
            self.store.insert("adoptions", row)
        return row

    def _pending_rejection(self, shot_id: str, asset_id: str) -> bool:
        """该镜头当前采用的资产是否带有未清除的否决标记（需要一次新的明示采用）。"""
        return any(
            item["target_kind"] == TargetKind.SHOT.value
            and item["target_id"] == shot_id
            and item["kind"] == ChangeKind.ASSET_REJECTED.value
            and item["subject"] == asset_id
            for item in self.rework_scope()
        )

    # ------------------------------------------------------------------
    # 变更与返工计算
    # ------------------------------------------------------------------
    def replace_reference(
        self, ref_id: str, old_version: int, new_version: int, actor: str, reason: str
    ) -> dict[str, Any]:
        """替换角色参考图：标记旧版本失效并计算受影响的镜头与合成产物。"""
        old = self.store.one(
            "SELECT * FROM reference_images WHERE ref_id = ? AND version = ?", (ref_id, old_version)
        )
        if old is None:
            raise HubError(f"参考图版本不存在: {ref_id}@{old_version}")
        if not self.store.one(
            "SELECT 1 AS x FROM reference_images WHERE ref_id = ? AND version = ?", (ref_id, new_version)
        ):
            raise HubError(f"替换目标版本不存在: {ref_id}@{new_version}")
        if not actor or not reason:
            raise HubError("变更必须记录操作人和理由")

        def matcher(frozen: dict[str, Any]) -> bool:
            return any(
                r.get("ref_id") == ref_id and r.get("version") == old_version
                for r in frozen.get("references", [])
            )

        with self.store.tx():
            self.store.update(
                "reference_images", {"superseded": 1}, "ref_id = ? AND version = ?", (ref_id, old_version)
            )
            return self._apply_change(
                kind=ChangeKind.REFERENCE_REPLACED,
                subject=f"{ref_id}@{old_version}",
                detail={"ref_id": ref_id, "old_version": old_version, "new_version": new_version},
                actor=actor,
                reason=reason,
                task_matcher=matcher,
            )

    def retire_prompt_pack(self, pack_id: str, version: int, actor: str, reason: str) -> dict[str, Any]:
        """废弃提示词包版本：禁止新任务使用，并计算受影响范围。"""
        pack = self.store.one(
            "SELECT * FROM prompt_packs WHERE pack_id = ? AND version = ?", (pack_id, version)
        )
        if pack is None:
            raise HubError(f"提示词包版本不存在: {pack_id}@{version}")
        if not actor or not reason:
            raise HubError("变更必须记录操作人和理由")

        def matcher(frozen: dict[str, Any]) -> bool:
            pack_ref = frozen.get("prompt_pack", {})
            return pack_ref.get("pack_id") == pack_id and pack_ref.get("version") == version

        with self.store.tx():
            self.store.update(
                "prompt_packs", {"retired": 1}, "pack_id = ? AND version = ?", (pack_id, version)
            )
            return self._apply_change(
                kind=ChangeKind.PROMPT_RETIRED,
                subject=f"{pack_id}@{version}",
                detail={"pack_id": pack_id, "version": version},
                actor=actor,
                reason=reason,
                task_matcher=matcher,
            )

    def _apply_change(
        self,
        kind: ChangeKind,
        subject: str,
        detail: dict[str, Any],
        actor: str,
        reason: str,
        task_matcher: Callable[[dict[str, Any]], bool] | None = None,
        rejected_assets: set[str] | None = None,
    ) -> dict[str, Any]:
        """登记变更并计算受影响镜头与合成产物。只标记，绝不自动覆盖已批准资产。"""
        rejected_assets = rejected_assets or set()
        change_id = new_id("chg")
        now = self._now()
        self.store.insert(
            "change_events",
            {
                "change_id": change_id,
                "kind": kind.value,
                "subject": subject,
                "detail_json": json.dumps(detail, ensure_ascii=False),
                "actor": actor,
                "reason": reason,
                "created_at": now,
            },
        )

        # 受影响的任务 → 镜头
        offending_task_ids: set[str] = set()
        affected_shots: set[str] = set()
        if task_matcher is not None:
            for task in self.store.all("SELECT * FROM tasks"):
                if task_matcher(json.loads(task["frozen_json"])):
                    offending_task_ids.add(task["task_id"])
                    affected_shots.add(task["shot_id"])
        if rejected_assets:
            for adoption in self.store.all("SELECT * FROM adoptions WHERE active = 1"):
                if adoption["asset_id"] in rejected_assets:
                    affected_shots.add(adoption["shot_id"])

        for shot_id in sorted(affected_shots):
            self.store.insert_or_ignore(
                "invalidations",
                {
                    "change_id": change_id,
                    "target_kind": TargetKind.SHOT.value,
                    "target_id": shot_id,
                    "detail_json": "{}",
                    "invalidated_at": now,
                },
            )

        # 受影响的合成产物（按最新版本判断）
        affected_composites: list[str] = []
        latest_versions = self.store.all(
            """
            SELECT c.* FROM composites c
            JOIN (SELECT composite_id, MAX(version) AS v FROM composites GROUP BY composite_id) m
              ON c.composite_id = m.composite_id AND c.version = m.v
            """
        )
        for composite in latest_versions:
            entries = json.loads(composite["built_from_json"])
            hit_tasks = sorted({e["task_id"] for e in entries} & offending_task_ids)
            hit_assets = sorted({e["asset_id"] for e in entries} & rejected_assets)
            if hit_tasks or hit_assets:
                self.store.insert_or_ignore(
                    "invalidations",
                    {
                        "change_id": change_id,
                        "target_kind": TargetKind.COMPOSITE.value,
                        "target_id": composite["composite_id"],
                        "detail_json": json.dumps(
                            {"task_ids": hit_tasks, "asset_ids": hit_assets}, ensure_ascii=False
                        ),
                        "invalidated_at": now,
                    },
                )
                affected_composites.append(composite["composite_id"])

        return {
            "change_id": change_id,
            "kind": kind.value,
            "subject": subject,
            "shots": sorted(affected_shots),
            "composites": sorted(affected_composites),
        }

    # ------------------------------------------------------------------
    # 合成产物
    # ------------------------------------------------------------------
    def build_composite(self, composite_id: str, name: str, shot_ids: list[str], actor: str) -> dict[str, Any]:
        """以各镜头当前采用关系构建合成产物新版本；拒绝使用被否决资产。"""
        entries = []
        for shot_id in shot_ids:
            adoption = self.store.one(
                "SELECT * FROM adoptions WHERE shot_id = ? AND active = 1", (shot_id,)
            )
            if adoption is None:
                raise HubError(f"镜头 {shot_id} 尚无采用资产，无法合成")
            asset = self.store.one(
                "SELECT * FROM candidates WHERE asset_id = ?", (adoption["asset_id"],)
            )
            if asset["state"] == AssetState.REJECTED.value:
                raise HubError(f"镜头 {shot_id} 的采用资产已被否决，无法合成")
            entries.append(
                {
                    "shot_id": shot_id,
                    "asset_id": adoption["asset_id"],
                    "task_id": asset["task_id"],
                    "adoption_id": adoption["adoption_id"],
                }
            )
        with self.store.tx():
            latest = self.store.one(
                "SELECT MAX(version) AS v FROM composites WHERE composite_id = ?", (composite_id,)
            )
            version = (latest["v"] or 0) + 1
            row = {
                "composite_id": composite_id,
                "version": version,
                "name": name,
                "built_from_json": json.dumps(entries, ensure_ascii=False),
                "built_by": actor,
                "built_at": self._now(),
            }
            self.store.insert("composites", row)
        return row

    # ------------------------------------------------------------------
    # 查询：返工范围与血缘
    # ------------------------------------------------------------------
    def rework_scope(self, change_id: str | None = None, include_cleared: bool = False) -> list[dict[str, Any]]:
        """列出需要返工但尚未重跑的范围（可限定某次变更）。"""
        sql = """
            SELECT i.*, c.kind, c.subject, c.detail_json AS change_detail, c.actor, c.reason
            FROM invalidations i JOIN change_events c ON c.change_id = i.change_id
        """
        params: tuple = ()
        if change_id:
            sql += " WHERE i.change_id = ?"
            params = (change_id,)
        items = []
        for row in self.store.all(sql + " ORDER BY i.id", params):
            cleared, note = self._check_cleared(row)
            if cleared and not include_cleared:
                continue
            items.append(
                {
                    "change_id": row["change_id"],
                    "kind": row["kind"],
                    "subject": row["subject"],
                    "target_kind": row["target_kind"],
                    "target_id": row["target_id"],
                    "invalidated_at": row["invalidated_at"],
                    "cleared": cleared,
                    "cleared_note": note,
                }
            )
        return items

    def _check_cleared(self, invalidation: dict[str, Any]) -> tuple[bool, str]:
        """判断返工标记是否已被后续重跑/重建清除。"""
        if invalidation["target_kind"] == TargetKind.SHOT.value:
            adoption = self.store.one(
                "SELECT * FROM adoptions WHERE shot_id = ? AND active = 1",
                (invalidation["target_id"],),
            )
            if adoption is None or adoption["adopted_at"] <= invalidation["invalidated_at"]:
                return False, ""
            if invalidation["kind"] == ChangeKind.ASSET_REJECTED.value:
                # 被否决资产无法直接采用；任何更新的采用都意味着人工已处置
                return True, f"已由采用 {adoption['adoption_id']} 清除"
            asset = self.store.one(
                "SELECT * FROM candidates WHERE asset_id = ?", (adoption["asset_id"],)
            )
            task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (asset["task_id"],))
            frozen = json.loads(task["frozen_json"])
            if self._frozen_hits_change(frozen, invalidation):
                return False, ""
            return True, f"已由采用 {adoption['adoption_id']}（任务 {asset['task_id']}）清除"

        # 合成产物：存在更新的版本且不再包含肇事任务/资产
        detail = json.loads(invalidation["detail_json"] or "{}")
        bad_tasks = set(detail.get("task_ids", []))
        bad_assets = set(detail.get("asset_ids", []))
        versions = self.store.all(
            "SELECT * FROM composites WHERE composite_id = ? AND built_at > ? ORDER BY version",
            (invalidation["target_id"], invalidation["invalidated_at"]),
        )
        for version in versions:
            entries = json.loads(version["built_from_json"])
            if not ({e["task_id"] for e in entries} & bad_tasks) and not (
                {e["asset_id"] for e in entries} & bad_assets
            ):
                return True, f"已由版本 v{version['version']} 清除"
        return False, ""

    @staticmethod
    def _frozen_hits_change(frozen: dict[str, Any], invalidation: dict[str, Any]) -> bool:
        detail = json.loads(invalidation["change_detail"])
        if invalidation["kind"] == ChangeKind.REFERENCE_REPLACED.value:
            return any(
                r.get("ref_id") == detail["ref_id"] and r.get("version") == detail["old_version"]
                for r in frozen.get("references", [])
            )
        if invalidation["kind"] == ChangeKind.PROMPT_RETIRED.value:
            pack_ref = frozen.get("prompt_pack", {})
            return pack_ref.get("pack_id") == detail["pack_id"] and pack_ref.get("version") == detail["version"]
        return False

    def shot_lineage(self, shot_id: str) -> dict[str, Any]:
        """从镜头反查：全部任务（冻结输入）、尝试（含回调与计费）、候选、批注与采用史。"""
        shot = self.store.one("SELECT * FROM shots WHERE shot_id = ?", (shot_id,))
        if shot is None:
            raise HubError(f"镜头不存在: {shot_id}")
        tasks = []
        for task in self.store.all("SELECT * FROM tasks WHERE shot_id = ? ORDER BY created_at", (shot_id,)):
            attempts = []
            for attempt in self.store.all(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task["task_id"],)
            ):
                events = self.store.all(
                    "SELECT event_id, status, occurred_at, processed_at, outcome FROM callback_events "
                    "WHERE attempt_id = ? ORDER BY n",
                    (attempt["attempt_id"],),
                )
                attempts.append({**attempt, "events": events})
            candidates = self.store.all(
                "SELECT * FROM candidates WHERE task_id = ?", (task["task_id"],)
            )
            quarantine = self.store.all(
                "SELECT * FROM quarantine WHERE attempt_id IN "
                "(SELECT attempt_id FROM attempts WHERE task_id = ?)",
                (task["task_id"],),
            )
            tasks.append(
                {
                    **task,
                    "frozen": json.loads(task["frozen_json"]),
                    "attempts": attempts,
                    "candidates": candidates,
                    "quarantine": quarantine,
                }
            )
        asset_ids = [c["asset_id"] for t in tasks for c in t["candidates"]]
        annotations = (
            self.store.all(
                f"SELECT * FROM annotations WHERE asset_id IN ({','.join('?' * len(asset_ids))}) ORDER BY created_at",
                tuple(asset_ids),
            )
            if asset_ids
            else []
        )
        adoptions = self.store.all(
            "SELECT * FROM adoptions WHERE shot_id = ? ORDER BY adopted_at", (shot_id,)
        )
        invalidations = [
            item
            for item in self.rework_scope(include_cleared=True)
            if item["target_kind"] == TargetKind.SHOT.value and item["target_id"] == shot_id
        ]
        return {
            "shot": {**shot, "characters": json.loads(shot["characters_json"])},
            "tasks": tasks,
            "annotations": annotations,
            "adoptions": adoptions,
            "invalidations": invalidations,
        }

    def composite_lineage(self, composite_id: str) -> dict[str, Any]:
        """从成片镜头反查：每个成分资产的输入、尝试与审批链。"""
        versions = self.store.all(
            "SELECT * FROM composites WHERE composite_id = ? ORDER BY version", (composite_id,)
        )
        if not versions:
            raise HubError(f"合成产物不存在: {composite_id}")
        current = versions[-1]
        entries = json.loads(current["built_from_json"])
        assets: dict[str, Any] = {}
        for entry in entries:
            asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (entry["asset_id"],))
            if asset is None or asset["asset_id"] in assets:
                continue
            task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (asset["task_id"],))
            attempts = self.store.all(
                "SELECT attempt_id, seq, status, billed, submitted_at, settled_at FROM attempts "
                "WHERE task_id = ? ORDER BY seq",
                (task["task_id"],),
            )
            annotations = self.store.all(
                "SELECT * FROM annotations WHERE asset_id = ? ORDER BY created_at", (asset["asset_id"],)
            )
            adoption = self.store.one(
                "SELECT * FROM adoptions WHERE adoption_id = ?", (entry["adoption_id"],)
            )
            assets[asset["asset_id"]] = {
                "asset": asset,
                "annotations": annotations,
                "adoption": adoption,
                "task": {**task, "frozen": json.loads(task["frozen_json"])},
                "attempts": attempts,
            }
        invalidations = [
            item
            for item in self.rework_scope(include_cleared=True)
            if item["target_kind"] == TargetKind.COMPOSITE.value and item["target_id"] == composite_id
        ]
        return {
            "composite_id": composite_id,
            "name": current["name"],
            "versions": [
                {**v, "built_from": json.loads(v["built_from_json"])} for v in versions
            ],
            "current_version": current["version"],
            "assets": assets,
            "invalidations": invalidations,
        }

    def task_view(self, task_id: str) -> dict[str, Any]:
        task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if task is None:
            raise HubError(f"任务不存在: {task_id}")
        attempts = self.store.all("SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task_id,))
        return {**task, "frozen": json.loads(task["frozen_json"]), "attempts": attempts}

    # ------------------------------------------------------------------
    # 崩溃恢复
    # ------------------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        """进程重启后的恢复：

        1. 重放收件箱里未处理的回调（不丢回调）；
        2. 对提交了但没落库的尝试按幂等键重提（供应商去重，不重复计费）；
        3. 对在途尝试向供应商回收结果，回收不到且超时的判超时并按策略重试。
        """
        report: dict[str, Any] = {"events_processed": 0, "resubmitted": [], "reconciled": [], "timed_out": []}
        report["events_processed"] = len(self.process_pending_events())

        for attempt in self.store.all(
            "SELECT * FROM attempts WHERE status = ? ORDER BY task_id, seq",
            (AttemptStatus.PENDING_SUBMIT.value,),
        ):
            if self._dispatch(attempt["attempt_id"]):
                report["resubmitted"].append(attempt["attempt_id"])

        now = self._now()
        for attempt in self.store.all(
            "SELECT * FROM attempts WHERE status = ?", (AttemptStatus.SUBMITTED.value,)
        ):
            adapter = self._adapter(attempt["provider"])
            payload = adapter.poll(attempt["provider_job_id"]) if attempt["provider_job_id"] else None
            if payload is not None:
                self.ingest_callback(payload)  # 与真实回调同 event_id，天然去重
                report["reconciled"].append(attempt["attempt_id"])
            elif attempt["deadline_at"] and attempt["deadline_at"] < now:
                with self.store.tx():
                    self.store.update(
                        "attempts",
                        {"status": AttemptStatus.TIMED_OUT.value, "settled_at": now},
                        "attempt_id = ?",
                        (attempt["attempt_id"],),
                    )
                report["timed_out"].append(attempt["attempt_id"])
                if self.auto_retry_on_timeout:
                    task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (attempt["task_id"],))
                    if task["status"] == TaskStatus.OPEN.value:
                        self.retry_task(task["task_id"], actor="system", reason="恢复后超时重试")
        return report
