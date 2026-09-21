"""回调/结果字节的持久收件箱与单工人消费者。

供应商投递先原子落盘到 ``spool/``，工人处理成功（含识别出重复）后才
移到 ``processed/``；进程在任何时刻被 SIGKILL，未确认的文件都会在
重启后重放。重放依赖数据库唯一约束（``event_id``、候选唯一键、幂等
派发行）做到天然幂等——同一条回调不会生效两次。
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .hub import AssetHub, HubError, RetriesExhausted


class Inbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.spool = self.root / "spool"
        self.processed = self.root / "processed"
        self.dead = self.root / "dead"
        for d in (self.spool, self.processed, self.dead):
            d.mkdir(parents=True, exist_ok=True)

    def _write_atomic(self, target_dir: Path, name: str, payload: dict[str, Any]) -> Path:
        path = target_dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def enqueue(self, item: dict[str, Any]) -> Path:
        seq = len(list(self.spool.glob("*.json")))
        name = f"{seq:06d}-{uuid.uuid4().hex[:12]}.json"
        return self._write_atomic(self.spool, name, item)

    def pending(self) -> list[Path]:
        return sorted(self.spool.glob("*.json"))


def recover_in_flight(hub: AssetHub) -> list[dict[str, Any]]:
    """启动恢复：把崩溃时停在 in_flight 的派发改发。

    复用同一幂等键，供应商只认一个任务号；已 recorded 的行不会被重调。
    """
    report = []
    for row in hub.in_flight_dispatches():
        try:
            result = hub.dispatch(row["attempt_id"])
            report.append({"attempt_id": row["attempt_id"], **result, "recovered": True})
        except RetriesExhausted:
            report.append({"attempt_id": row["attempt_id"], "recovered": False,
                           "note": "仍超时，保持 in_flight 等待下次恢复"})
    return report


class Worker:
    def __init__(self, hub: AssetHub, inbox: Inbox):
        self.hub = hub
        self.inbox = inbox

    def apply_item(self, item: dict[str, Any]) -> dict[str, Any]:
        kind = item.get("kind", "callback")
        if kind == "callback":
            return {"kind": "callback", **self.hub.receive_callback(item["envelope"])}
        if kind == "bytes":
            data = base64.b64decode(item["data_b64"])
            return {"kind": "bytes", **self.hub.deliver_result_bytes(
                item["attempt_id"], data,
                declared_sha256=item["declared_sha256"],
                declared_media_type=item["declared_media_type"],
                event_id=item.get("event_id"),
                attributes=item.get("attributes"),
            )}
        raise HubError(f"未知收件条目类型: {kind!r}")

    def run_once(self) -> list[dict[str, Any]]:
        done = []
        for path in self.inbox.pending():
            item = json.loads(path.read_text(encoding="utf-8"))
            try:
                result = self.apply_item(item)
            except HubError as exc:
                # 业务拒绝（未知尝试/格式问题等）：进死信目录并留原因，不阻塞队列
                dead_path = self.inbox.dead / path.name
                payload = {"item": item, "error": str(exc)}
                (dead_path.with_suffix(".json.err")).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(path, dead_path)
                done.append({"file": path.name, "dead": True, "error": str(exc)})
                continue
            os.replace(path, self.inbox.processed / path.name)
            done.append({"file": path.name, **result})
        return done

    def run_forever(self, poll_interval: float = 0.2) -> None:  # pragma: no cover - 演示入口
        while True:
            self.run_once()
            time.sleep(poll_interval)
