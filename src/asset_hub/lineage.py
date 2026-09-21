"""血缘反查与变更影响面分析。

两类验收视角：

* :meth:`Lineage.trace_shot` —— 从成片镜头反查全部输入版本、尝试、
  回调（含迟到/重复留证）、候选隔离记录、审批轨迹与下游合成产物；
* :meth:`Lineage.stale_work` —— 从一份变更列出受影响且*尚未用当前
  输入重跑*的镜头/合成产物（返工单的可执行范围）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import media as media_mod
from .models import ChangeKind
from .store import parse_iso


class Lineage:
    def __init__(self, hub):
        self.hub = hub
        self.conn = hub.store.conn

    # -- 镜头全量反查 -------------------------------------------------------

    def trace_shot(self, shot_id: str) -> dict[str, Any]:
        conn = self.conn
        shot = conn.execute("SELECT * FROM shots WHERE shot_id=:s", {"s": shot_id}).fetchone()
        if not shot:
            raise KeyError(f"镜头不存在: {shot_id}")
        prompt_versions = [
            {
                "package_id": r["package_id"], "version": r["version"],
                "template": r["template"], "variables": json.loads(r["variables_json"]),
                "deprecated": json.loads(r["deprecated_json"]),
                "created_by": r["created_by"], "created_at": r["created_at"],
            }
            for r in conn.execute(
                "SELECT * FROM prompt_packages WHERE shot_id=:s ORDER BY version", {"s": shot_id}
            )
        ]
        refs = []
        for r in conn.execute(
            """SELECT r.* FROM reference_images r JOIN shot_reference_bindings b
               ON b.reference_id=r.reference_id WHERE b.shot_id=:s
               ORDER BY r.reference_id, r.version""",
            {"s": shot_id},
        ):
            refs.append(dict(r))
        # 同一参考的全部版本（含已被替换的旧版）
        ref_ids = sorted({r["reference_id"] for r in refs})
        ref_versions = {
            rid: [dict(x) for x in conn.execute(
                "SELECT version, sha256, media_type, created_by, created_at FROM reference_images "
                "WHERE reference_id=:r ORDER BY version", {"r": rid})]
            for rid in ref_ids
        }
        attempts = []
        for a in conn.execute(
            "SELECT * FROM attempts WHERE shot_id=:s ORDER BY created_at, rowid", {"s": shot_id}
        ):
            frozen = json.loads(a["frozen_json"])
            dispatches = [
                dict(d) for d in conn.execute(
                    "SELECT * FROM dispatches WHERE attempt_id=:a ORDER BY id", {"a": a["attempt_id"]}
                )
            ]
            events = [
                dict(c) for c in conn.execute(
                    "SELECT * FROM callbacks WHERE attempt_id=:a ORDER BY occurred_at, rowid",
                    {"a": a["attempt_id"]},
                )
            ]
            deliveries = [
                dict(d) for d in conn.execute(
                    "SELECT * FROM callback_deliveries WHERE attempt_id=:a ORDER BY id",
                    {"a": a["attempt_id"]},
                )
            ]
            candidates = [
                dict(c) for c in conn.execute(
                    "SELECT * FROM candidates WHERE attempt_id=:a ORDER BY received_at, rowid",
                    {"a": a["attempt_id"]},
                )
            ]
            for c in candidates:
                c["attributes"] = json.loads(c.pop("attributes_json"))
            attempts.append({
                "attempt_id": a["attempt_id"],
                "status": a["status"],
                "frozen_digest": a["frozen_digest"],
                "frozen_input": frozen,
                "expected_media_type": a["expected_media_type"],
                "created_at": a["created_at"],
                "dispatch_id": a["dispatch_id"],
                "attempts_made": a["attempts_made"],
                "failure_code": a["failure_code"],
                "terminal_event_id": a["terminal_event_id"],
                "dispatches": dispatches,
                "callbacks": events,
                "deliveries": deliveries,
                "candidates": candidates,
            })
        approvals = [
            dict(r) for r in conn.execute(
                "SELECT * FROM adoptions WHERE shot_id=:s ORDER BY id", {"s": shot_id}
            )
        ]
        active = conn.execute(
            """SELECT aa.*, c.sha256, c.media_type, c.attempt_id AS candidate_attempt
               FROM active_adoptions aa JOIN candidates c ON c.candidate_id=aa.candidate_id
               WHERE aa.shot_id=:s""",
            {"s": shot_id},
        ).fetchone()
        composites = [
            self.trace_product(r["product_id"])
            for r in conn.execute(
                "SELECT product_id FROM composites WHERE shot_id=:s ORDER BY created_at", {"s": shot_id}
            )
        ]
        return {
            "shot": dict(shot),
            "prompt_packages": prompt_versions,
            "references": {rid: ref_versions[rid] for rid in ref_ids},
            "attempts": attempts,
            "approvals": approvals,
            "active_adoption": dict(active) if active else None,
            "composites": composites,
        }

    def trace_product(self, product_id: str) -> dict[str, Any]:
        conn = self.conn
        p = conn.execute("SELECT * FROM composites WHERE product_id=:p", {"p": product_id}).fetchone()
        if not p:
            raise KeyError(f"合成产物不存在: {product_id}")
        inputs = []
        for r in conn.execute(
            """SELECT ci.*, c.sha256, c.media_type, c.status AS candidate_status,
                      a.frozen_digest, a.frozen_json
               FROM composite_inputs ci
               JOIN candidates c ON c.candidate_id=ci.candidate_id
               JOIN attempts a ON a.attempt_id=ci.attempt_id
               WHERE ci.product_id=:p""",
            {"p": product_id},
        ):
            d = dict(r)
            d["frozen_input"] = json.loads(d.pop("frozen_json"))
            inputs.append(d)
        return {
            "product_id": p["product_id"], "shot_id": p["shot_id"], "kind": p["kind"],
            "sha256": p["sha256"], "recipe": json.loads(p["recipe_json"]),
            "created_at": p["created_at"], "inputs": inputs,
        }

    # -- 变更影响面 ---------------------------------------------------------

    def _current_digest(self, shot_id: str, adapter: str, parameters: dict[str, Any]) -> str:
        with self.hub.store.tx() as conn:
            frozen = self.hub._freeze(conn, shot_id, adapter, parameters)
        return media_mod.sha256_hex(frozen.canonical_json())

    def _rerun_state(self, shot_id: str, adapter: str, parameters: dict[str, Any]) -> str:
        """当前输入下的重跑状态：not_rerun | in_progress | rerun。"""
        digest = self._current_digest(shot_id, adapter, parameters)
        rows = self.conn.execute(
            "SELECT status FROM attempts WHERE shot_id=:s AND frozen_digest=:d",
            {"s": shot_id, "d": digest},
        ).fetchall()
        if not rows:
            return "not_rerun"
        statuses = {r["status"] for r in rows}
        if "succeeded" in statuses:
            return "rerun"
        if statuses & {"dispatched", "pending"}:
            return "in_progress"
        return "not_rerun"  # 仅有失败/死信尝试，仍需返工

    def _stale_attempts(self, shot_id: str, since: datetime) -> list:
        """变更时间点之前、建立在旧输入上的尝试。"""
        return self.conn.execute(
            "SELECT * FROM attempts WHERE shot_id=:s AND created_at<=:t ORDER BY rowid",
            {"s": shot_id, "t": since.isoformat()},
        ).fetchall()

    def _affected_composites(self, shot_id: str, stale_attempt_ids: set[str],
                             rejected_candidate_ids: set[str] | None = None,
                             since: datetime | None = None) -> list[dict[str, Any]]:
        rejected_candidate_ids = rejected_candidate_ids or set()
        out = []
        for p in self.conn.execute(
            "SELECT * FROM composites WHERE shot_id=:s ORDER BY created_at", {"s": shot_id}
        ):
            inputs = self.conn.execute(
                "SELECT * FROM composite_inputs WHERE product_id=:p", {"p": p["product_id"]}
            ).fetchall()
            tainted = []
            for ci in inputs:
                if ci["attempt_id"] in stale_attempt_ids:
                    tainted.append({"candidate_id": ci["candidate_id"], "because": "stale_frozen_input",
                                    "role": ci["role"]})
                if ci["candidate_id"] in rejected_candidate_ids:
                    tainted.append({"candidate_id": ci["candidate_id"], "because": "candidate_rejected",
                                    "role": ci["role"]})
            if not tainted:
                continue
            # 变更后已有同镜头同类型的更新合成产物：视为已重渲染，不再挂起
            remediated = False
            if since is not None:
                remediated = bool(self.conn.execute(
                    """SELECT 1 FROM composites WHERE shot_id=:s AND kind=:k AND created_at>:t
                       AND product_id<>:p LIMIT 1""",
                    {"s": shot_id, "k": p["kind"], "t": since.isoformat(), "p": p["product_id"]},
                ).fetchone())
            out.append({"product_id": p["product_id"], "kind": p["kind"],
                        "tainted_inputs": tainted, "remediated": remediated})
        return out

    def stale_work(self, *, only_open: bool = True) -> list[dict[str, Any]]:
        """每份变更 -> 受影响镜头、重跑状态、受污染合成产物。

        ``only_open=True`` 时只返回仍需返工（未重跑或重跑进行中）的条目，
        即“需要返工但尚未重跑的范围”。
        """
        results = []
        for ch in self.conn.execute("SELECT * FROM changes ORDER BY id"):
            kind = ch["kind"]
            at = parse_iso(ch["at"])
            detail = json.loads(ch["detail_json"])
            affected_shots: list[dict[str, Any]] = []

            if kind == ChangeKind.REFERENCE_REPLACED.value:
                shot_rows = self._shots_using_reference(ch["ref_id"])
                for s in shot_rows:
                    shot_id = s["shot_id"]
                    stale = [
                        a for a in self._stale_attempts(shot_id, at)
                        if self._frozen_used_reference(a, ch["ref_id"], detail.get("old_version"))
                    ]
                    if not stale:
                        continue
                    combos = self._adapter_param_combos(stale)
                    reruns = [
                        {"adapter": adapter, "parameters": params,
                         "state": self._rerun_state(shot_id, adapter, params)}
                        for adapter, params in combos
                    ]
                    composites = self._affected_composites(
                        shot_id, {a["attempt_id"] for a in stale}, since=at)
                    affected_shots.append({"shot_id": shot_id, "reruns": reruns,
                                           "composites": composites})

            elif kind == ChangeKind.PROMPT_REVISED.value:
                shot_id = ch["shot_id"]
                stale = self._stale_attempts(shot_id, at)
                if stale:
                    combos = self._adapter_param_combos(stale)
                    reruns = [
                        {"adapter": adapter, "parameters": params,
                         "state": self._rerun_state(shot_id, adapter, params)}
                        for adapter, params in combos
                    ]
                    composites = self._affected_composites(
                        shot_id, {a["attempt_id"] for a in stale}, since=at)
                    affected_shots.append({"shot_id": shot_id, "reruns": reruns,
                                           "composites": composites})

            elif kind == ChangeKind.CANDIDATE_REJECTED.value:
                shot_id = ch["shot_id"]
                rejected = {ch["ref_id"]}
                # 否决生效候选：引用它的全部合成产物受污染；旧尝试不算脏
                composites = self._affected_composites(
                    shot_id, set(), rejected_candidate_ids=rejected, since=at
                )
                has_new_adoption = bool(self.conn.execute(
                    """SELECT 1 FROM active_adoptions WHERE shot_id=:s""", {"s": shot_id}
                ).fetchone())
                state = "rerun" if has_new_adoption else "not_rerun"
                affected_shots.append({"shot_id": shot_id,
                                       "reruns": [{"adapter": None, "parameters": {}, "state": state}],
                                       "composites": composites})

            elif kind == ChangeKind.ADOPTION_REPLACED.value:
                # 采用替换：旧候选退出，引用旧候选的合成产物需重渲染
                shot_id = ch["shot_id"]
                old_cid = detail.get("replaced_candidate_id")
                composites = self._affected_composites(
                    shot_id, set(), rejected_candidate_ids={old_cid} if old_cid else set(), since=at
                )
                affected_shots.append({"shot_id": shot_id, "reruns": [], "composites": composites})

            if only_open:
                open_shots = []
                for s in affected_shots:
                    open_reruns = [r for r in s["reruns"] if r["state"] != "rerun"]
                    open_composites = [c for c in s["composites"] if not c.get("remediated")]
                    if open_reruns or open_composites:
                        open_shots.append({**s, "reruns": open_reruns, "composites": open_composites})
                if not open_shots:
                    continue
                affected_shots = open_shots

            if affected_shots:
                results.append({"change_id": ch["id"], "kind": kind, "at": ch["at"],
                                "detail": detail, "affected": affected_shots})
        return results

    def _shots_using_reference(self, reference_id: str):
        return self.conn.execute(
            """SELECT DISTINCT s.shot_id FROM shots s
               JOIN shot_reference_bindings b ON b.shot_id=s.shot_id
               WHERE b.reference_id=:r ORDER BY s.shot_id""",
            {"r": reference_id},
        ).fetchall()

    @staticmethod
    def _frozen_used_reference(attempt_row, reference_id: str, old_version: int | None) -> bool:
        frozen = json.loads(attempt_row["frozen_json"])
        for rid, version, _sha in frozen["references"]:
            if rid == reference_id:
                # old_version 为 0/None 时（首次替换前）一律算旧
                return old_version in (None, 0) or version <= old_version
        return False

    @staticmethod
    def _adapter_param_combos(attempts: list) -> list[tuple[str, dict[str, Any]]]:
        """从旧尝试归纳出需要按当前输入重跑的 (适配器, 参数) 组合。"""
        combos: dict[tuple[str, str], dict[str, Any]] = {}
        for a in attempts:
            frozen = json.loads(a["frozen_json"])
            key = (frozen["adapter"], json.dumps(frozen["parameters"], sort_keys=True))
            combos.setdefault(key, frozen["parameters"])
        return [(adapter, params) for (adapter, _key), params in combos.items()]
