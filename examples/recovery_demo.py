"""真实进程中断与恢复演示。

以子进程方式运行，父进程在关键窗口发送 SIGKILL（kill -9，不给清理机会），
随后以*全新进程*打开同一账本恢复，证明：

1. 派发表停在 in_flight，恢复时复用同一幂等键 -> 只有一个计费任务号；
2. 已确认的 spool 文件进入 processed，未处理的文件在磁盘上幸存；
3. 重放回调/字节时 event_id 去重与候选唯一约束保证不产生第二份结果。

运行：``python3 examples/recovery_demo.py``
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asset_hub import AssetHub, FlakyVideoAdapter, Inbox, Lineage, Worker, recover_in_flight
from asset_hub.media import sha256_hex, synthetic_png
from asset_hub.store import Store

DEMO_ROOT = Path("/tmp/asset-hub-recovery-demo")
SHOT = "shot-crash"


def _open(adapter_fail_times: int) -> tuple[Store, AssetHub, Inbox]:
    store = Store(DEMO_ROOT)
    hub = AssetHub(store)
    hub.set_adapter_instance("flaky-video", FlakyVideoAdapter(fail_times=adapter_fail_times))
    return store, hub, Inbox(DEMO_ROOT / "inbox")


def phase_setup() -> None:
    import shutil

    shutil.rmtree(DEMO_ROOT, ignore_errors=True)
    store, hub, inbox = _open(adapter_fail_times=0)
    hub.add_shot(SHOT, "CRASH-1", "恢复演示镜头", "中断后必须不重复计费、不丢回调")
    hub.add_prompt_package(SHOT, f"pkg-{SHOT}", "镜头提示 {mood}", {"mood": "紧张"})
    ref = hub.add_reference("ref-x", "style", synthetic_png(4, 4, (5, 6, 7)),
                            created_by="demo")
    hub.bind_reference(SHOT, "ref-x", "style")

    # 尝试 1：派发过程中将被 kill（适配器永远超时）
    prep1 = hub.prepare_generation(SHOT, "flaky-video", {"fps": 24}, "video/mp4")
    (DEMO_ROOT / "attempt1.txt").write_text(prep1["attempt_id"])

    # 尝试 2：正常派发，回调与字节走收件箱；工人处理完第 1 封后被 kill
    prep2 = hub.prepare_generation(SHOT, "echo-image", {"steps": 12}, "image/png")
    hub.dispatch(prep2["attempt_id"])
    png = synthetic_png(10, 10, (70, 90, 120))
    digest = sha256_hex(png)
    (DEMO_ROOT / "attempt2.txt").write_text(prep2["attempt_id"])
    (DEMO_ROOT / "digest.txt").write_text(digest)
    inbox.enqueue({"kind": "callback", "envelope": {
        "event_id": "ev-crash-1", "attempt_id": prep2["attempt_id"],
        "status": "succeeded", "occurred_at": "2026-09-12T10:00:00Z",
        "asset_sha256": digest, "width": 1080}})
    inbox.enqueue({"kind": "bytes", "attempt_id": prep2["attempt_id"],
                   "declared_sha256": digest, "declared_media_type": "image/png",
                   "event_id": "ev-crash-1",
                   "data_b64": base64.b64encode(png).decode()})
    inbox.enqueue({"kind": "callback", "envelope": {  # 重复回调，验证重放去重
        "event_id": "ev-crash-1", "attempt_id": prep2["attempt_id"],
        "status": "succeeded", "occurred_at": "2026-09-12T10:00:00Z",
        "asset_sha256": digest}})
    store.close()
    print("[setup] 账本与 3 封 spool 已就绪")


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise RuntimeError("等待条件超时")


def phase_dispatch_crash() -> None:
    """在永远超时的派发循环中被 SIGKILL。"""
    store, hub, _ = _open(adapter_fail_times=99)
    attempt_id = (DEMO_ROOT / "attempt1.txt").read_text()
    hub.dispatch(attempt_id, max_transport_attempts=10_000)  # 会一直超时到被杀
    store.close()


def phase_worker_crash() -> None:
    """处理完第一封 spool 后睡眠，等待父进程 SIGKILL。"""
    store, hub, inbox = _open(adapter_fail_times=0)
    worker = Worker(hub, inbox)
    first = inbox.pending()[0]
    item = json.loads(first.read_text(encoding="utf-8"))
    worker.apply_item(item)
    os.replace(first, inbox.processed / first.name)  # apply_item 已按事务提交
    print("[worker] 已处理第一封，进入长睡眠…", flush=True)
    time.sleep(120)


def launch_killed(phase: str, ready_predicate) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, __file__, phase],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _wait_for(ready_predicate)
    time.sleep(0.15)  # 确保它停在“派发中途/睡眠中”而非干净退出点
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert proc.returncode == -9, proc.returncode
    print(f"[parent] 子进程 {phase} 已被 SIGKILL（退出码 {proc.returncode}）")
    return proc


def phase_parent() -> None:
    phase_setup()

    a1 = (DEMO_ROOT / "attempt1.txt").read_text()

    # 窗口一：派发中途被 kill；等 in_flight 行落盘
    def in_flight_visible():
        store = Store(DEMO_ROOT)
        row = store.query_one(
            "SELECT state FROM dispatches WHERE attempt_id=:a", a=a1)
        visible = row is not None and row["state"] == "in_flight"
        calls = store.query_one("SELECT calls_made FROM dispatches WHERE attempt_id=:a", a=a1)
        store.close()
        if visible:
            print(f"[parent] 观测到 in_flight 派发行（已记录传输尝试 {calls['calls_made']} 次）")
        return visible

    launch_killed("--phase-dispatch-crash", in_flight_visible)

    # 窗口二：工人处理完第一封 spool 后被 kill
    def first_mail_processed():
        return len(list((DEMO_ROOT / "inbox" / "processed").glob("*.json"))) >= 1

    launch_killed("--phase-worker-crash", first_mail_processed)

    # ---- 全新进程恢复 -----------------------------------------------------
    print("\n=== 以全新进程恢复 ===")
    store, hub, inbox = _open(adapter_fail_times=0)
    spool_before = [p.name for p in inbox.pending()]
    print(f"[recover] spool 中幸存未确认邮件: {spool_before}")
    assert len(spool_before) == 2, "未处理邮件必须在磁盘上幸存"

    report = recover_in_flight(hub)
    print(f"[recover] in_flight 恢复结果: {json.dumps(report, ensure_ascii=False)}")

    processed = Worker(hub, inbox).run_once()
    print(f"[recover] 重放处理: {[p.get('note', p.get('accepted')) for p in processed]}")

    # ---- 验收：不重复计费 -------------------------------------------------
    drow = store.query_one("SELECT * FROM dispatches WHERE attempt_id=:a", a=a1)
    task_rows = store.query_all(
        "SELECT COUNT(*) AS c, COUNT(DISTINCT idempotency_key) AS keys, "
        "COUNT(DISTINCT dispatch_id) AS tasks FROM dispatches WHERE attempt_id=:a", a=a1)
    print("\n--- 计费核对（attempt 1）---")
    print(f"派发行数={task_rows[0]['c']}  幂等键数={task_rows[0]['keys']}  "
          f"供应商任务号数={task_rows[0]['tasks']}  最终状态={drow['state']}  "
          f"传输尝试合计={drow['calls_made']}")
    assert task_rows[0]["c"] == 1 and task_rows[0]["keys"] == 1 and task_rows[0]["tasks"] == 1

    # ---- 验收：不丢回调、不重复生效 ---------------------------------------
    a2 = (DEMO_ROOT / "attempt2.txt").read_text()
    print("\n--- 回调核对（attempt 2）---")
    cbs = store.query_all("SELECT event_id, applied, note FROM callbacks ORDER BY rowid")
    deliveries = store.query_all("SELECT dedup FROM callback_deliveries ORDER BY id")
    candidates = store.query_all("SELECT status, COUNT(*) AS c FROM candidates GROUP BY status")
    final_status = store.query_one("SELECT status FROM attempts WHERE attempt_id=:a", a=a2)["status"]
    for r in cbs:
        print(f"回调 {r['event_id']}: applied={r['applied']} note={r['note']}")
    print("物理投递留证:", [r["dedup"] for r in deliveries])
    print("候选分布:", {r["status"]: r["c"] for r in candidates})
    print("attempt 2 终态:", final_status)
    assert len(cbs) == 1 and cbs[0]["applied"] == 1
    dedups = [r["dedup"] for r in deliveries]
    assert dedups.count("duplicate_event") == 1
    assert sum(1 for r in candidates if r["status"] == "available") == 1
    assert final_status == "succeeded"
    assert not inbox.pending()

    print("\n✅ SIGKILL 恢复演示通过：任务只计费一次，邮件零丢失、零重复生效。")
    store.close()


PHASES = {
    "--phase-setup": phase_setup,
    "--phase-dispatch-crash": phase_dispatch_crash,
    "--phase-worker-crash": phase_worker_crash,
}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in PHASES:
        PHASES[sys.argv[1]]()
        return 0
    phase_parent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
