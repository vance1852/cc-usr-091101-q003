"""资产中枢核心服务。

关键不变量：

1. *冻结*：每次尝试把提示词版本、参考图版本、参数与适配器快照成
   :class:`FrozenInput`，派发生成的幂等任务号由其摘要派生，事后任何
   提示词/参考替换都不会改写历史尝试。
2. *计费一次*：``dispatches`` 对幂等键唯一；超时重试与崩溃恢复都复用
   同一行、同一键，供应商侧只产生一个任务号。
3. *回调幂等*：``event_id`` 唯一去重；终态一旦落定，迟到的反向通知
   只留存不生效（先到失败、后到成功不会制造第二份有效结果）。
4. *校验隔离*：成功文件只有在实际字节的 SHA-256 与格式均吻合回调
   声明时才进入候选区，否则物理隔离进 ``quarantine/``。
5. *批准不可自动覆盖*：生效采用只能被带 reviewer+reason 的显式替换
   或否决移动，历史逐行留存。
"""

from __future__ import annotations

import json
from typing import Any

from . import media as media_mod
from .adapters import AdapterError, DispatchRejected, get_adapter_class
from .contracts import CallbackStatus, ProviderCallback
from .models import (
    AdoptionKind,
    AttemptStatus,
    CandidateStatus,
    ChangeKind,
    FrozenInput,
    PromptPackage,
    ReferenceImage,
    Shot,
    utc_now,
)
from .store import Store, iso, parse_iso


class HubError(RuntimeError):
    """业务规则冲突（参数可向调用方展示）。"""


class RetriesExhausted(HubError):
    def __init__(self, attempt_id: str, key: str, calls: int):
        super().__init__(f"尝试 {attempt_id} 连续 {calls} 次派发超时，幂等键 {key} 仍保留")
        self.attempt_id = attempt_id
        self.idempotency_key = key
        self.calls = calls


def _frozen_from_row(row) -> FrozenInput:
    raw = json.loads(row["frozen_json"])
    return FrozenInput(
        adapter=raw["adapter"],
        prompt_package_id=raw["prompt_package_id"],
        prompt_version=raw["prompt_version"],
        prompt_sha256=raw["prompt_sha256"],
        references=tuple(tuple(r) for r in raw["references"]),
        parameters=raw["parameters"],
    )


class AssetHub:
    def __init__(self, store: Store):
        self.store = store
        self._adapter_instances: dict[str, Any] = {}

    # -- 适配器 -------------------------------------------------------------

    def set_adapter_instance(self, name: str, instance: Any) -> None:
        self._adapter_instances[name] = instance

    def _adapter(self, name: str) -> Any:
        if name not in self._adapter_instances:
            self._adapter_instances[name] = get_adapter_class(name)()
        return self._adapter_instances[name]

    # -- 目录：镜头 / 提示词包 / 参考图 -------------------------------------

    def add_shot(self, shot_id: str, code: str, scene: str, description: str) -> Shot:
        shot = Shot(shot_id, code, scene, description)
        with self.store.tx():
            self.store.insert(
                "shots", shot_id=shot_id, code=code, scene=scene, description=description
            )
        return shot

    def add_prompt_package(
        self,
        shot_id: str,
        package_id: str,
        template: str,
        variables: dict[str, Any],
        *,
        deprecated: list[str] | None = None,
        created_by: str = "system",
    ) -> PromptPackage:
        """登记提示词包新版本（旧版本永不被改写）。

        首次写入 version=1；同 package_id 再次调用即生成新版本，可在
        ``deprecated`` 中显式声明本版废弃的变量键（如旧服装提示词）。
        """
        deprecated = deprecated or []
        with self.store.tx() as conn:
            if not conn.execute("SELECT 1 FROM shots WHERE shot_id=:s", {"s": shot_id}).fetchone():
                raise HubError(f"镜头不存在: {shot_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM prompt_packages WHERE package_id=:p",
                {"p": package_id},
            ).fetchone()
            version = row["v"] + 1
            now = utc_now()
            pkg = PromptPackage(package_id, shot_id, version, template, variables, deprecated, created_by, now)
            conn.execute(
                """INSERT INTO prompt_packages
                   (package_id, shot_id, version, template, variables_json, deprecated_json, created_by, created_at)
                   VALUES (:package_id,:shot_id,:version,:template,:variables,:deprecated,:created_by,:created_at)""",
                {
                    "package_id": package_id,
                    "shot_id": shot_id,
                    "version": version,
                    "template": template,
                    "variables": json.dumps(variables, ensure_ascii=False, sort_keys=True),
                    "deprecated": json.dumps(deprecated, ensure_ascii=False),
                    "created_by": created_by,
                    "created_at": iso(now),
                },
            )
        return pkg

    def add_reference(
        self,
        reference_id: str,
        role: str,
        data: bytes,
        *,
        shot_id: str | None = None,
        media_type: str | None = None,
        created_by: str = "system",
    ) -> ReferenceImage:
        """登记参考图新版本。字节经内容校验写入对象仓库。"""
        actual_type = media_mod.sniff_media_type(data)
        if media_type is not None and actual_type != media_type:
            raise HubError(f"参考图声明格式 {media_type} 与实际字节不符（实际 {actual_type}）")
        if actual_type is None:
            raise HubError("参考图格式不受支持（仅 png/jpeg/mp4）")
        digest = media_mod.sha256_hex(data)
        # 字节先落盘（孤儿文件无害；数据库行指向缺失文件才是损坏）
        if not self.store.has_object(digest):
            self.store.write_bytes_atomic(self.store.object_path(digest), data)
        with self.store.tx() as conn:
            version = (
                conn.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM reference_images WHERE reference_id=:r",
                    {"r": reference_id},
                ).fetchone()["v"]
                + 1
            )
            now = utc_now()
            conn.execute(
                """INSERT INTO reference_images
                   (reference_id, shot_id, role, version, sha256, media_type, created_by, created_at)
                   VALUES (:reference_id,:shot_id,:role,:version,:sha256,:media_type,:created_by,:created_at)""",
                {
                    "reference_id": reference_id,
                    "shot_id": shot_id,
                    "role": role,
                    "version": version,
                    "sha256": digest,
                    "media_type": actual_type,
                    "created_by": created_by,
                    "created_at": iso(now),
                },
            )
            conn.execute(
                "INSERT OR IGNORE INTO objects (sha256, media_type, size_bytes, ingested_at) VALUES (?,?,?,?)",
                (digest, actual_type, len(data), iso(now)),
            )
        ref = ReferenceImage(reference_id, shot_id, role, version, digest, actual_type, created_by)
        return ref

    def bind_reference(self, shot_id: str, reference_id: str, role: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO shot_reference_bindings (shot_id, reference_id, role) VALUES (?,?,?)",
                (shot_id, reference_id, role),
            )

    def replace_reference(
        self, reference_id: str, data: bytes, *, reviewer: str, reason: str
    ) -> ReferenceImage:
        """总监替换角色参考：登记新版本并写变更单，影响面由分析器展开。"""
        if not reviewer or not reason:
            raise HubError("替换参考必须记录操作人和理由")
        old = self.latest_reference(reference_id)
        if not old:
            raise HubError(f"参考不存在，首版请用 add_reference: {reference_id}")
        ref = self.add_reference(reference_id, old.role, data, shot_id=old.shot_id,
                                 created_by=reviewer)
        with self.store.tx() as conn:
            conn.execute(
                """INSERT INTO changes (kind, ref_type, ref_id, shot_id, detail_json, at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    ChangeKind.REFERENCE_REPLACED.value,
                    "reference",
                    reference_id,
                    None,
                    json.dumps(
                        {"old_version": old.version if old else 0, "new_version": ref.version,
                         "reviewer": reviewer, "reason": reason},
                        ensure_ascii=False,
                    ),
                    iso(utc_now()),
                ),
            )
        return ref

    def revise_prompt(
        self, shot_id: str, package_id: str, template: str, variables: dict[str, Any],
        *, deprecated: list[str] | None = None, reviewer: str, reason: str,
    ) -> PromptPackage:
        if not reviewer or not reason:
            raise HubError("修订提示词必须记录操作人和理由")
        row = self.store.query_one(
            "SELECT COALESCE(MAX(version),0) AS v FROM prompt_packages WHERE package_id=:p", p=package_id
        )
        old_version = row["v"]
        pkg = self.add_prompt_package(shot_id, package_id, template, variables,
                                      deprecated=deprecated, created_by=reviewer)
        with self.store.tx() as conn:
            conn.execute(
                """INSERT INTO changes (kind, ref_type, ref_id, shot_id, detail_json, at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    ChangeKind.PROMPT_REVISED.value,
                    "prompt_package",
                    package_id,
                    shot_id,
                    json.dumps({"old_version": old_version, "new_version": pkg.version,
                                "reviewer": reviewer, "reason": reason,
                                "deprecated": deprecated or []}, ensure_ascii=False),
                    iso(utc_now()),
                ),
            )
        return pkg

    def latest_reference(self, reference_id: str):
        row = self.store.query_one(
            """SELECT * FROM reference_images WHERE reference_id=:r ORDER BY version DESC LIMIT 1""", r=reference_id
        )
        return self._ref_row(row) if row else None

    @staticmethod
    def _ref_row(row) -> ReferenceImage:
        return ReferenceImage(
            row["reference_id"], row["shot_id"], row["role"], row["version"],
            row["sha256"], row["media_type"], row["created_by"], parse_iso(row["created_at"]),
        )

    def _bound_references(self, conn, shot_id: str) -> list[ReferenceImage]:
        rows = conn.execute(
            """SELECT r.* FROM reference_images r
               JOIN shot_reference_bindings b ON b.reference_id = r.reference_id
               WHERE b.shot_id=:s
                 AND r.version = (SELECT MAX(version) FROM reference_images WHERE reference_id=r.reference_id)
               ORDER BY r.reference_id""",
            {"s": shot_id},
        ).fetchall()
        return [self._ref_row(r) for r in rows]

    def _latest_prompt(self, conn, shot_id: str) -> PromptPackage:
        row = conn.execute(
            """SELECT * FROM prompt_packages WHERE shot_id=:s
               ORDER BY version DESC LIMIT 1""",
            {"s": shot_id},
        ).fetchone()
        if not row:
            raise HubError(f"镜头 {shot_id} 尚无提示词包")
        return PromptPackage(
            row["package_id"], row["shot_id"], row["version"], row["template"],
            json.loads(row["variables_json"]), json.loads(row["deprecated_json"]),
            row["created_by"], parse_iso(row["created_at"]),
        )

    # -- 冻结输入与幂等准备 -------------------------------------------------

    def _freeze(self, conn, shot_id: str, adapter_name: str, parameters: dict[str, Any]) -> FrozenInput:
        pkg = self._latest_prompt(conn, shot_id)
        refs = self._bound_references(conn, shot_id)
        rendered = pkg.render()
        return FrozenInput(
            adapter=adapter_name,
            prompt_package_id=pkg.package_id,
            prompt_version=pkg.version,
            prompt_sha256=media_mod.sha256_hex(rendered.encode("utf-8")),
            references=tuple(sorted((r.reference_id, r.version, r.sha256) for r in refs)),
            parameters=dict(sorted(parameters.items())),
        )

    def prepare_generation(
        self, shot_id: str, adapter_name: str, parameters: dict[str, Any], expected_media_type: str
    ) -> dict[str, Any]:
        """冻结输入并取得/复用尝试。相同输入的重复提交绝不产生第二个任务。"""
        get_adapter_class(adapter_name)  # 提前拦截未知适配器
        with self.store.tx() as conn:
            frozen = self._freeze(conn, shot_id, adapter_name, parameters)
            digest = media_mod.sha256_hex(frozen.canonical_json())
            existing = conn.execute(
                """SELECT * FROM attempts WHERE shot_id=:s AND frozen_digest=:d
                   ORDER BY rowid DESC LIMIT 1""",
                {"s": shot_id, "d": digest},
            ).fetchone()
            if existing:
                return {"attempt_id": existing["attempt_id"], "created": False,
                        "status": existing["status"], "frozen_digest": digest}
            seq = conn.execute(
                "SELECT COUNT(*) AS c FROM attempts WHERE shot_id=:s AND frozen_digest=:d",
                {"s": shot_id, "d": digest},
            ).fetchone()["c"] + 1
            attempt_id = f"att-{digest[:16]}-{seq}"
            now = utc_now()
            conn.execute(
                """INSERT INTO attempts
                   (attempt_id, shot_id, frozen_digest, frozen_json, expected_media_type,
                    status, created_at, attempts_made)
                   VALUES (:a,:s,:d,:j,:e,:st,:t,0)""",
                {"a": attempt_id, "s": shot_id, "d": digest,
                 "j": json.dumps(self._frozen_to_json(frozen), ensure_ascii=False, sort_keys=True),
                 "e": expected_media_type, "st": AttemptStatus.PENDING.value, "t": iso(now)},
            )
            return {"attempt_id": attempt_id, "created": True,
                    "status": AttemptStatus.PENDING.value, "frozen_digest": digest}

    @staticmethod
    def _frozen_to_json(frozen: FrozenInput) -> dict[str, Any]:
        return {
            "adapter": frozen.adapter,
            "prompt_package_id": frozen.prompt_package_id,
            "prompt_version": frozen.prompt_version,
            "prompt_sha256": frozen.prompt_sha256,
            "references": [list(r) for r in frozen.references],
            "parameters": frozen.parameters,
        }

    def idempotency_key(self, attempt_id: str) -> str:
        row = self.store.query_one("SELECT frozen_digest FROM attempts WHERE attempt_id=:a", a=attempt_id)
        if not row:
            raise HubError(f"尝试不存在: {attempt_id}")
        # 键与冻结摘要 1:1 绑定；同一尝试的每次超时重试都是同一个键
        return f"ikey-{attempt_id}"

    def dispatch(self, attempt_id: str, *, max_transport_attempts: int = 3) -> dict[str, Any]:
        """派发（或复用）供应商任务；超时在同键下重试，不重复计费。"""
        with self.store.tx() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=:a", {"a": attempt_id}).fetchone()
            if not attempt:
                raise HubError(f"尝试不存在: {attempt_id}")
            if attempt["terminal_event_id"]:
                return {"attempt_id": attempt_id, "dispatch_id": attempt["dispatch_id"],
                        "terminal": True, "new_call": False, "calls_made": attempt["attempts_made"]}
            frozen = _frozen_from_row(attempt)
            adapter = self._adapter(frozen.adapter)
            key = f"ikey-{attempt_id}"
            drow = conn.execute("SELECT * FROM dispatches WHERE idempotency_key=:k", {"k": key}).fetchone()
            now = iso(utc_now())
            if drow is None:
                conn.execute(
                    """INSERT INTO dispatches (attempt_id, idempotency_key, calls_made, state, created_at, updated_at)
                       VALUES (:a,:k,0,'in_flight',:t,:t)""",
                    {"a": attempt_id, "k": key, "t": now},
                )
                drow = conn.execute("SELECT * FROM dispatches WHERE idempotency_key=:k", {"k": key}).fetchone()
            elif drow["state"] == "recorded":
                # 已拿到任务号后的重复派发：直接返回，绝不再次调用供应商
                return {"attempt_id": attempt_id, "dispatch_id": drow["dispatch_id"],
                        "terminal": False, "new_call": False, "calls_made": attempt["attempts_made"]}

        last_error: Exception | None = None
        for _ in range(max_transport_attempts):
            with self.store.tx() as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=:a", {"a": attempt_id}).fetchone()
                frozen = _frozen_from_row(attempt)
                drow = conn.execute("SELECT * FROM dispatches WHERE idempotency_key=:k",
                                    {"k": f"ikey-{attempt_id}"}).fetchone()
                if drow["state"] == "recorded":
                    return {"attempt_id": attempt_id, "dispatch_id": drow["dispatch_id"],
                            "terminal": False, "new_call": False, "calls_made": attempt["attempts_made"]}
                calls = drow["calls_made"] + 1
                try:
                    dispatch_id = adapter.dispatch(frozen, f"ikey-{attempt_id}")
                except (AdapterError, DispatchRejected) as exc:
                    last_error = exc
                    conn.execute(
                        "UPDATE dispatches SET calls_made=:c, updated_at=:t WHERE idempotency_key=:k",
                        {"c": calls, "t": iso(utc_now()), "k": f"ikey-{attempt_id}"},
                    )
                    conn.execute(
                        "UPDATE attempts SET attempts_made=:c WHERE attempt_id=:a",
                        {"c": calls, "a": attempt_id},
                    )
                    if isinstance(exc, DispatchRejected):
                        raise
                    continue
                t = iso(utc_now())
                conn.execute(
                    """UPDATE dispatches SET dispatch_id=:d, calls_made=:c, state='recorded', updated_at=:t
                       WHERE idempotency_key=:k""",
                    {"d": dispatch_id, "c": calls, "t": t, "k": f"ikey-{attempt_id}"},
                )
                conn.execute(
                    """UPDATE attempts SET status='dispatched', dispatch_id=:d, attempts_made=:c,
                       dispatched_at=:t WHERE attempt_id=:a""",
                    {"d": dispatch_id, "c": calls, "t": t, "a": attempt_id},
                )
                return {"attempt_id": attempt_id, "dispatch_id": dispatch_id,
                        "terminal": False, "new_call": True, "calls_made": calls}
        raise RetriesExhausted(attempt_id, f"ikey-{attempt_id}",
                               self.store.query_one(
                                   "SELECT calls_made FROM dispatches WHERE idempotency_key=:k",
                                   k=f"ikey-{attempt_id}")["calls_made"])

    def in_flight_dispatches(self) -> list[dict[str, Any]]:
        """崩溃恢复入口：进程中断时停在 in_flight 的派发。"""
        rows = self.store.query_all(
            """SELECT d.*, a.status AS attempt_status FROM dispatches d
               JOIN attempts a ON a.attempt_id=d.attempt_id
               WHERE d.state='in_flight'"""
        )
        return [dict(r) for r in rows]

    def retry_terminal(self, attempt_id: str) -> str:
        """为 FAILED/DEAD_LETTER 尝试开启同一冻结输入的下一代尝试（显式人工动作）。"""
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE attempt_id=:a", {"a": attempt_id}).fetchone()
            if not row:
                raise HubError(f"尝试不存在: {attempt_id}")
            if row["status"] not in (AttemptStatus.FAILED.value, AttemptStatus.DEAD_LETTER.value):
                raise HubError("仅失败或死信尝试可以显式重开")
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM attempts WHERE shot_id=:s AND frozen_digest=:d",
                {"s": row["shot_id"], "d": row["frozen_digest"]},
            ).fetchone()["c"]
            new_id = f"att-{row['frozen_digest'][:16]}-{count + 1}"
            conn.execute(
                """INSERT INTO attempts
                   (attempt_id, shot_id, frozen_digest, frozen_json, expected_media_type,
                    status, created_at, attempts_made)
                   VALUES (:a,:s,:d,:j,:e,'pending',:t,0)""",
                {"a": new_id, "s": row["shot_id"], "d": row["frozen_digest"],
                 "j": row["frozen_json"], "e": row["expected_media_type"], "t": iso(utc_now())},
            )
            return new_id

    # -- 回调信封 -----------------------------------------------------------

    def receive_callback(self, raw_envelope: dict[str, Any]) -> dict[str, Any]:
        """落入一封供应商回调。

        - event_id 重复：只标记 duplicate，不产生任何副作用；
        - 尝试已终态：迟到通知留存为 late 事件，状态不翻转；
        - 失败先到：立即落 FAILED 终态；成功后到时成为 late_succeeded；
        - 成功先到：登记声明摘要，等待字节校验通过后才进 SUCCEEDED。
        """
        cb = ProviderCallback.from_dict(raw_envelope)
        with self.store.tx() as conn:
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=:a", {"a": cb.attempt_id}
            ).fetchone()
            if not attempt:
                raise HubError(f"回调指向未知尝试: {cb.attempt_id}")
            dup = conn.execute(
                "SELECT 1 FROM callbacks WHERE event_id=:e", {"e": cb.event_id}
            ).fetchone()
            raw_json = json.dumps(raw_envelope, ensure_ascii=False, sort_keys=True)
            now = iso(utc_now())

            def log_delivery(dedup: str) -> None:
                conn.execute(
                    """INSERT INTO callback_deliveries (event_id, attempt_id, received_at, dedup)
                       VALUES (:e,:a,:t,:d)""",
                    {"e": cb.event_id, "a": cb.attempt_id, "t": now, "d": dedup},
                )

            if dup:
                log_delivery("duplicate_event")
                return {"event_id": cb.event_id, "applied": False, "note": "duplicate_event",
                        "attempt_status": attempt["status"]}

            if attempt["terminal_event_id"]:
                note = f"late_{cb.status.value}_after_terminal"
                log_delivery(note)
                conn.execute(
                    """INSERT INTO callbacks
                       (event_id, attempt_id, status, occurred_at, raw_json, received_at, applied, note)
                       VALUES (:e,:a,:s,:t,:j,:r,0,:n)""",
                    {"e": cb.event_id, "a": cb.attempt_id, "s": cb.status.value,
                     "t": iso(cb.occurred_at), "j": raw_json, "r": now, "n": note},
                )
                return {"event_id": cb.event_id, "applied": False, "note": note,
                        "attempt_status": attempt["status"]}

            if cb.status is CallbackStatus.FAILED:
                # 成功证据已存在（成功认领或已通过校验的候选）：迟到的失败
                # 通知只留存，不允许把即将/已经成立的成功打成失败
                has_validated = conn.execute(
                    "SELECT 1 FROM candidates WHERE attempt_id=:a AND status='available' LIMIT 1",
                    {"a": cb.attempt_id},
                ).fetchone()
                if attempt["success_event_id"] or has_validated:
                    note = "late_failure_after_success_claim"
                    log_delivery(note)
                    conn.execute(
                        """INSERT INTO callbacks
                           (event_id, attempt_id, status, occurred_at, raw_json, received_at, applied, note)
                           VALUES (:e,:a,:s,:t,:j,:r,0,:n)""",
                        {"e": cb.event_id, "a": cb.attempt_id, "s": cb.status.value,
                         "t": iso(cb.occurred_at), "j": raw_json, "r": now, "n": note},
                    )
                    return {"event_id": cb.event_id, "applied": False, "note": note,
                            "attempt_status": attempt["status"]}
                code = str(cb.attributes.get("error_code", "UNSPECIFIED"))
                log_delivery("applied:settled_failure")
                conn.execute(
                    """INSERT INTO callbacks
                       (event_id, attempt_id, status, occurred_at, raw_json, received_at, applied, note)
                       VALUES (:e,:a,:s,:t,:j,:r,1,'settled_failure')""",
                    {"e": cb.event_id, "a": cb.attempt_id, "s": cb.status.value,
                     "t": iso(cb.occurred_at), "j": raw_json, "r": now},
                )
                conn.execute(
                    """UPDATE attempts SET status='failed', terminal_event_id=:e, terminal_at=:t,
                       failure_code=:c WHERE attempt_id=:a""",
                    {"e": cb.event_id, "t": iso(cb.occurred_at), "a": cb.attempt_id, "c": code},
                )
                return {"event_id": cb.event_id, "applied": True, "note": "settled_failure",
                        "attempt_status": AttemptStatus.FAILED.value}

            # 成功通知：登记认领与声明摘要，等待字节校验
            # 已有成功认领时，声明*不同*摘要的第二封成功通知属于冲突重发：
            # 只留存，绝不覆盖首封认领（否则一次尝试可能冒出两份“有效”结果）
            if attempt["success_event_id"] and attempt["declared_sha"] != cb.asset_sha256:
                note = "conflicting_success_claim"
                log_delivery(note)
                conn.execute(
                    """INSERT INTO callbacks
                       (event_id, attempt_id, status, occurred_at, raw_json, received_at, applied, note)
                       VALUES (:e,:a,:s,:t,:j,:r,0,:n)""",
                    {"e": cb.event_id, "a": cb.attempt_id, "s": cb.status.value,
                     "t": iso(cb.occurred_at), "j": raw_json, "r": now, "n": note},
                )
                return {"event_id": cb.event_id, "applied": False, "note": note,
                        "attempt_status": attempt["status"]}
            log_delivery("applied:success_claimed")
            conn.execute(
                """INSERT INTO callbacks
                   (event_id, attempt_id, status, occurred_at, raw_json, received_at, applied, note)
                   VALUES (:e,:a,:s,:t,:j,:r,1,'success_claimed')""",
                {"e": cb.event_id, "a": cb.attempt_id, "s": cb.status.value,
                 "t": iso(cb.occurred_at), "j": raw_json, "r": now},
            )
            conn.execute(
                """UPDATE attempts SET status='dispatched', success_event_id=:e, declared_sha=:d
                   WHERE attempt_id=:a""",
                {"e": cb.event_id, "d": cb.asset_sha256, "a": cb.attempt_id},
            )
            # 字节可能先于回调到达：若已有与声明吻合的候选，立即终态化
            cand = conn.execute(
                "SELECT * FROM candidates WHERE attempt_id=:a AND sha256=:d AND status='available'",
                {"a": cb.attempt_id, "d": cb.asset_sha256},
            ).fetchone()
            if cand:
                conn.execute(
                    """UPDATE attempts SET status='succeeded', terminal_event_id=:e,
                       terminal_at=:t WHERE attempt_id=:a""",
                    {"e": cb.event_id, "t": iso(cb.occurred_at), "a": cb.attempt_id},
                )
                conn.execute(
                    "UPDATE callbacks SET note='settled_success' WHERE event_id=:e",
                    {"e": cb.event_id},
                )
                return {"event_id": cb.event_id, "applied": True, "note": "settled_success",
                        "attempt_status": AttemptStatus.SUCCEEDED.value,
                        "candidate_id": cand["candidate_id"]}
            return {"event_id": cb.event_id, "applied": True, "note": "awaiting_validated_bytes",
                    "attempt_status": AttemptStatus.DISPATCHED.value}

    # -- 结果字节校验与候选区 -----------------------------------------------

    def deliver_result_bytes(
        self,
        attempt_id: str,
        data: bytes,
        *,
        declared_sha256: str,
        declared_media_type: str,
        event_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attributes = attributes or {}
        actual_digest = media_mod.sha256_hex(data)
        reason = media_mod.judge_declaration(declared_media_type, declared_sha256, data)
        with self.store.tx() as conn:
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=:a", {"a": attempt_id}
            ).fetchone()
            if not attempt:
                raise HubError(f"尝试不存在: {attempt_id}")
            now = iso(utc_now())

            if reason is None and declared_media_type != attempt["expected_media_type"]:
                reason = "FORMAT_UNEXPECTED"

            existing = conn.execute(
                "SELECT * FROM candidates WHERE attempt_id=:a AND sha256=:d",
                {"a": attempt_id, "d": actual_digest},
            ).fetchone()

            # 已终态（失败/死信）的尝试：晚到的成功字节只能隔离，绝不复活终态；
            # 正确救济路径是 retry_terminal 开新尝试
            terminal = attempt["status"] in (
                AttemptStatus.FAILED.value, AttemptStatus.DEAD_LETTER.value
            )
            invalid = reason is not None or terminal
            final_reason = reason or ("LATE_AFTER_TERMINAL" if terminal else None)

            if invalid:
                target = self.store.quarantine_path(actual_digest)
                if not target.exists():
                    self.store.write_bytes_atomic(target, data)
                if existing:
                    cid = existing["candidate_id"]
                else:
                    cid = f"cand-{attempt_id[-8:]}-{actual_digest[:12]}"
                    conn.execute(
                        """INSERT INTO candidates
                           (candidate_id, attempt_id, shot_id, sha256, media_type, status,
                            quarantine_reason, event_id, attributes_json, received_at)
                           VALUES (:c,:a,:s,:d,:m,'quarantined',:r,:e,:j,:t)""",
                        {"c": cid, "a": attempt_id, "s": attempt["shot_id"], "d": actual_digest,
                         "m": declared_media_type or "unknown", "r": final_reason, "e": event_id,
                         "j": json.dumps({**attributes, "declared_sha256": declared_sha256},
                                         ensure_ascii=False, sort_keys=True), "t": now},
                    )
                # 成功已认领、尝试尚未终态，但文件过不了校验：死信终态
                if (
                    attempt["success_event_id"]
                    and attempt["status"] == AttemptStatus.DISPATCHED.value
                ):
                    conn.execute(
                        """UPDATE attempts SET status='dead_letter', terminal_at=:t,
                           failure_code=:c WHERE attempt_id=:a""",
                        {"t": now, "c": f"VALIDATION_FAILED:{final_reason}", "a": attempt_id},
                    )
                return {"candidate_id": cid, "accepted": False, "quarantine_reason": final_reason}

            # 校验通过
            if not self.store.has_object(actual_digest):
                self.store.write_bytes_atomic(self.store.object_path(actual_digest), data)
                conn.execute(
                    "INSERT OR IGNORE INTO objects (sha256, media_type, size_bytes, ingested_at) VALUES (?,?,?,?)",
                    (actual_digest, declared_media_type, len(data), now),
                )
            if existing:
                cid = existing["candidate_id"]
                if existing["status"] == CandidateStatus.QUARANTINED.value:
                    raise HubError("同一摘要曾被隔离，不能凭重复投递进入候选区")
            else:
                cid = f"cand-{attempt_id[-8:]}-{actual_digest[:12]}"
                conn.execute(
                    """INSERT INTO candidates
                       (candidate_id, attempt_id, shot_id, sha256, media_type, status,
                        quarantine_reason, event_id, attributes_json, received_at)
                       VALUES (:c,:a,:s,:d,:m,'available',NULL,:e,:j,:t)""",
                    {"c": cid, "a": attempt_id, "s": attempt["shot_id"], "d": actual_digest,
                     "m": declared_media_type, "e": event_id,
                     "j": json.dumps(attributes, ensure_ascii=False, sort_keys=True), "t": now},
                )

            settled = False
            if (
                attempt["success_event_id"]
                and attempt["declared_sha"] == actual_digest
                and not attempt["terminal_event_id"]
            ):
                conn.execute(
                    """UPDATE attempts SET status='succeeded', terminal_event_id=:e, terminal_at=:t
                       WHERE attempt_id=:a""",
                    {"e": attempt["success_event_id"], "t": now, "a": attempt_id},
                )
                conn.execute(
                    "UPDATE callbacks SET note='settled_success' WHERE event_id=:e",
                    {"e": attempt["success_event_id"]},
                )
                settled = True
            return {"candidate_id": cid, "accepted": True, "settled": settled,
                    "attempt_status": AttemptStatus.SUCCEEDED.value if settled else attempt["status"]}

    # -- 人工采用 / 否决 / 重新采用 -----------------------------------------

    def _get_active(self, conn, shot_id: str):
        return conn.execute(
            """SELECT aa.*, c.sha256, c.media_type, c.attempt_id FROM active_adoptions aa
               JOIN candidates c ON c.candidate_id=aa.candidate_id
               WHERE aa.shot_id=:s""",
            {"s": shot_id},
        ).fetchone()

    @staticmethod
    def _require_review(reviewer: str, reason: str) -> None:
        if not reviewer or not reason:
            raise HubError("采用状态变更必须留下明确的人和理由")

    def adopt(self, shot_id: str, candidate_id: str, reviewer: str, reason: str) -> dict[str, Any]:
        self._require_review(reviewer, reason)
        with self.store.tx() as conn:
            cand = conn.execute("SELECT * FROM candidates WHERE candidate_id=:c", {"c": candidate_id}).fetchone()
            if not cand:
                raise HubError(f"候选不存在: {candidate_id}")
            if cand["shot_id"] != shot_id:
                raise HubError("候选不属于该镜头")
            if cand["status"] not in (CandidateStatus.AVAILABLE.value, CandidateStatus.ADOPTED.value):
                raise HubError(f"候选状态为 {cand['status']}，不可采用（被否决/隔离需显式重新采用）")
            if self._get_active(conn, shot_id):
                raise HubError("该镜头已有生效采用；替换必须走 replace_adoption 并记录理由")
            now = iso(utc_now())
            cur = conn.execute(
                "INSERT INTO adoptions (shot_id,kind,candidate_id,replaced_candidate_id,reviewer,reason,at) VALUES (?,?,?,?,?,?,?)",
                (shot_id, AdoptionKind.ADOPT.value, candidate_id, None, reviewer, reason, now),
            )
            conn.execute(
                "INSERT INTO active_adoptions (shot_id,candidate_id,adoption_id) VALUES (?,?,?)",
                (shot_id, candidate_id, cur.lastrowid),
            )
            conn.execute("UPDATE candidates SET status='adopted' WHERE candidate_id=:c", {"c": candidate_id})
            return {"shot_id": shot_id, "candidate_id": candidate_id, "kind": AdoptionKind.ADOPT.value}

    def replace_adoption(
        self, shot_id: str, new_candidate_id: str, reviewer: str, reason: str
    ) -> dict[str, Any]:
        """显式替换生效采用；旧候选退回候选区，历史行保留人与理由。"""
        self._require_review(reviewer, reason)
        with self.store.tx() as conn:
            active = self._get_active(conn, shot_id)
            if not active:
                raise HubError("该镜头尚无生效采用，应使用 adopt")
            cand = conn.execute("SELECT * FROM candidates WHERE candidate_id=:c",
                                {"c": new_candidate_id}).fetchone()
            if not cand or cand["shot_id"] != shot_id:
                raise HubError("新候选不存在或不属于该镜头")
            if cand["status"] == CandidateStatus.REJECTED.value:
                raise HubError("被否决候选须先显式重新采用（readopt_rejected）")
            if cand["status"] == CandidateStatus.QUARANTINED.value:
                raise HubError("隔离候选不可采用")
            if new_candidate_id == active["candidate_id"]:
                raise HubError("新候选与当前生效候选相同")
            now = iso(utc_now())
            cur = conn.execute(
                """INSERT INTO adoptions (shot_id,kind,candidate_id,replaced_candidate_id,reviewer,reason,at)
                   VALUES (?,?,?,?,?,?,?)""",
                (shot_id, AdoptionKind.REPLACE.value, new_candidate_id, active["candidate_id"],
                 reviewer, reason, now),
            )
            conn.execute(
                "UPDATE active_adoptions SET candidate_id=:c, adoption_id=:a WHERE shot_id=:s",
                {"c": new_candidate_id, "a": cur.lastrowid, "s": shot_id},
            )
            conn.execute("UPDATE candidates SET status='available' WHERE candidate_id=:c",
                         {"c": active["candidate_id"]})
            conn.execute("UPDATE candidates SET status='adopted' WHERE candidate_id=:c",
                         {"c": new_candidate_id})
            conn.execute(
                """INSERT INTO changes (kind, ref_type, ref_id, shot_id, detail_json, at)
                   VALUES (?,?,?,?,?,?)""",
                (ChangeKind.ADOPTION_REPLACED.value, "candidate", new_candidate_id, shot_id,
                 json.dumps({"replaced_candidate_id": active["candidate_id"],
                             "reviewer": reviewer, "reason": reason}, ensure_ascii=False), now),
            )
            return {"shot_id": shot_id, "candidate_id": new_candidate_id,
                    "replaced": active["candidate_id"], "kind": AdoptionKind.REPLACE.value}

    def reject_candidate(
        self, candidate_id: str, reviewer: str, reason: str
    ) -> dict[str, Any]:
        """总监否决候选；若它正生效，镜头回到无生效采用，并记变更单。"""
        self._require_review(reviewer, reason)
        with self.store.tx() as conn:
            cand = conn.execute("SELECT * FROM candidates WHERE candidate_id=:c",
                                {"c": candidate_id}).fetchone()
            if not cand:
                raise HubError(f"候选不存在: {candidate_id}")
            if cand["status"] == CandidateStatus.QUARANTINED.value:
                raise HubError("隔离候选无需否决")
            now = iso(utc_now())
            conn.execute(
                """INSERT INTO adoptions (shot_id,kind,candidate_id,replaced_candidate_id,reviewer,reason,at)
                   VALUES (?,?,?,?,?,?,?)""",
                (cand["shot_id"], AdoptionKind.REJECT.value, candidate_id, None, reviewer, reason, now),
            )
            active = self._get_active(conn, cand["shot_id"])
            was_active = bool(active and active["candidate_id"] == candidate_id)
            if was_active:
                conn.execute("DELETE FROM active_adoptions WHERE shot_id=:s", {"s": cand["shot_id"]})
                conn.execute(
                    """INSERT INTO changes (kind, ref_type, ref_id, shot_id, detail_json, at)
                       VALUES (?,?,?,?,?,?)""",
                    (ChangeKind.CANDIDATE_REJECTED.value, "candidate", candidate_id, cand["shot_id"],
                     json.dumps({"was_active": True, "reviewer": reviewer, "reason": reason},
                                ensure_ascii=False), now),
                )
            conn.execute("UPDATE candidates SET status='rejected' WHERE candidate_id=:c",
                         {"c": candidate_id})
            return {"candidate_id": candidate_id, "was_active": was_active}

    def readopt_rejected(
        self, shot_id: str, candidate_id: str, reviewer: str, reason: str
    ) -> dict[str, Any]:
        """重新采用曾被否决的图：必须再次留下人和理由，且只能走显式替换路径。"""
        self._require_review(reviewer, reason)
        with self.store.tx() as conn:
            cand = conn.execute("SELECT * FROM candidates WHERE candidate_id=:c",
                                {"c": candidate_id}).fetchone()
            if not cand or cand["shot_id"] != shot_id:
                raise HubError("候选不存在或不属于该镜头")
            if cand["status"] != CandidateStatus.REJECTED.value:
                raise HubError("该候选未被否决，使用常规 adopt/replace_adoption")
            active = self._get_active(conn, shot_id)
            now = iso(utc_now())
            if active:
                cur = conn.execute(
                    """INSERT INTO adoptions (shot_id,kind,candidate_id,replaced_candidate_id,reviewer,reason,at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (shot_id, AdoptionKind.REPLACE.value, candidate_id, active["candidate_id"],
                     reviewer, reason, now),
                )
                conn.execute(
                    "UPDATE active_adoptions SET candidate_id=:c, adoption_id=:a WHERE shot_id=:s",
                    {"c": candidate_id, "a": cur.lastrowid, "s": shot_id},
                )
                conn.execute("UPDATE candidates SET status='available' WHERE candidate_id=:c",
                             {"c": active["candidate_id"]})
            else:
                cur = conn.execute(
                    """INSERT INTO adoptions (shot_id,kind,candidate_id,replaced_candidate_id,reviewer,reason,at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (shot_id, AdoptionKind.ADOPT.value, candidate_id, None, reviewer, reason, now),
                )
                conn.execute(
                    "INSERT INTO active_adoptions (shot_id,candidate_id,adoption_id) VALUES (?,?,?)",
                    (shot_id, candidate_id, cur.lastrowid),
                )
            conn.execute("UPDATE candidates SET status='adopted' WHERE candidate_id=:c",
                         {"c": candidate_id})
            return {"shot_id": shot_id, "candidate_id": candidate_id, "readopted": True}

    # -- 下游合成产物 -------------------------------------------------------

    def register_composite(
        self, product_id: str, shot_id: str, kind: str, data: bytes,
        recipe: dict[str, Any], inputs: list[tuple[str, str]],  # (candidate_id, role)
    ) -> dict[str, Any]:
        """登记合成产物及其输入候选血缘。inputs 必须为已采用候选。"""
        digest = media_mod.sha256_hex(data)
        # 字节先落盘，再提交血缘行（见 add_reference 的崩溃理由）
        if not self.store.has_object(digest):
            self.store.write_bytes_atomic(self.store.object_path(digest), data)
        with self.store.tx() as conn:
            if conn.execute("SELECT 1 FROM composites WHERE product_id=:p", {"p": product_id}).fetchone():
                raise HubError(f"合成产物已存在: {product_id}")
            now = iso(utc_now())
            conn.execute(
                "INSERT INTO composites (product_id,shot_id,kind,sha256,recipe_json,created_at) VALUES (?,?,?,?,?,?)",
                (product_id, shot_id, kind, digest, json.dumps(recipe, ensure_ascii=False, sort_keys=True), now),
            )
            for candidate_id, role in inputs:
                row = conn.execute(
                    """SELECT c.*, a.attempt_id FROM candidates c JOIN attempts a ON a.attempt_id=c.attempt_id
                       WHERE c.candidate_id=:c""", {"c": candidate_id},
                ).fetchone()
                if not row:
                    raise HubError(f"合成输入候选不存在: {candidate_id}")
                if row["status"] != CandidateStatus.ADOPTED.value:
                    raise HubError(f"合成输入 {candidate_id} 不是已采用候选（当前 {row['status']}）")
                conn.execute(
                    "INSERT INTO composite_inputs (product_id,candidate_id,attempt_id,role) VALUES (?,?,?,?)",
                    (product_id, candidate_id, row["attempt_id"], role),
                )
            conn.execute(
                "INSERT OR IGNORE INTO objects (sha256, media_type, size_bytes, ingested_at) VALUES (?,?,?,?)",
                (digest, "video/mp4" if kind == "video" else "application/octet-stream",
                 len(data), now),
            )
        return {"product_id": product_id, "sha256": digest}
