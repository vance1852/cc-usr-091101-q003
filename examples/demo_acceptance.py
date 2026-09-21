#!/usr/bin/env python3
"""验收演示：分镜生成资产中枢的完整生命周期。

覆盖验收点：
  1. 镜头拆解、提示词包、参考图全部版本化，提交即冻结输入并给出幂等任务号
  2. 超时重试 / 重复回调 / 先到的失败通知都不会制造多份有效结果
  3. 供应商文件只有摘要与声明格式吻合才进入候选区，否则进隔离区
  4. 替换角色参考、废弃提示词、否决图片时自动计算受影响的镜头与合成产物
  5. 已批准资产不被自动覆盖，重新采用留下明确的人和理由
  6. 从成片镜头反查全部输入、尝试与审批；从变更列出未重跑的返工范围
  7. 进程中断后恢复：不重复计费、不丢失回调

运行：python examples/demo_acceptance.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import AssetHub, FakeProvider, PlannedResult, Store  # noqa: E402

FIXTURES = Path(__file__).parents[1] / "fixtures"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(microseconds=1)
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(label: str, obj) -> None:
    print(f"  {label}: {json.dumps(obj, ensure_ascii=False, default=str)}")


def plan_success(provider: FakeProvider, attempt_id: str, fixture: str = "ok.png") -> str:
    """按样例清单安排一次成功出图（按尝试加盐模拟不同输入产出不同图），返回完整声明摘要。"""
    manifest = json.loads((FIXTURES / "assets" / "manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["files"] if e["file"] == fixture)
    data = (FIXTURES / "assets" / fixture).read_bytes() + attempt_id.encode()
    import hashlib

    declared = hashlib.sha256(data).hexdigest()
    provider.plan(
        attempt_id,
        PlannedResult(
            status="succeeded",
            file_bytes=data,
            declared_sha256=declared,
            declared_format=entry["declared_format"],
        ),
    )
    return declared


def asset_id(sha256: str) -> str:
    return "asset-" + sha256[:16]


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    clock = Clock()
    provider = FakeProvider(clock=clock)
    hub = AssetHub(Store(Path(tmp.name) / "hub.db"), {"fake": provider}, clock=clock, default_timeout_seconds=60)

    section("1. 镜头清单与版本化输入（提示词包 / 角色参考图）")
    shots = json.loads((FIXTURES / "shots.json").read_text(encoding="utf-8"))
    hub.import_shots(shots)
    print(f"  导入镜头 {len(shots)} 个：{[s['shot_id'] for s in shots]}")
    hub.create_prompt_pack("costume-linxue", {"prompt": "红色风衣, 雨夜霓虹", "negative": "模糊"}, actor="导演")
    hub.register_reference("char-linxue", "character:林雪", "a" * 64, "image/png", actor="美术")
    print("  提示词包 costume-linxue@1、参考图 char-linxue@1 已登记")

    section("2. 幂等提交：同一组冻结输入永远得到同一个任务号")
    res1 = hub.submit_generation("SH-01", "costume-linxue", 1, [("char-linxue", 1)], "fake", actor="分镜师")
    res2 = hub.submit_generation("SH-01", "costume-linxue", 1, [("char-linxue", 1)], "fake", actor="分镜师")
    show("首次提交", res1)
    show("重复提交", res2)
    show("供应商计费流水（仅 1 次）", provider.charges)
    sha_sh01 = plan_success(provider, res1["attempt_id"])
    out = hub.ingest_callback(provider.make_callback(res1["attempt_id"]))
    show("SH-01 出图回调", out)

    section("3. 超时重试 + 乱序回调（回放 fixtures/callbacks.json 的场景）")
    res = hub.submit_generation("SH-02", "costume-linxue", 1, [("char-linxue", 1)], "fake", actor="分镜师")
    task_id, try1 = res["task_id"], res["attempt_id"]
    clock.advance(120)  # 供应商静默，越过超时线
    show("超时清扫", hub.sweep_timeouts())
    try2 = f"{task_id}-a2"
    print(f"  已自动重试：{try1}（超时）→ {try2}（同一任务号 {task_id}）")
    sha_sh02 = plan_success(provider, try2)
    # 回放样例：失败通知先到（属于旧尝试），成功回调后到（属于新尝试）
    sample = json.loads((FIXTURES / "callbacks.json").read_text(encoding="utf-8"))
    failure = sample[0]  # cb-301：try-1 的失败通知，发生时间更晚却先到达
    failure["attempt_id"] = try1
    success = provider.make_callback(try2)
    success["event_id"] = sample[1]["event_id"]  # cb-302
    success["occurred_at"] = sample[1]["occurred_at"]  # 发生时间更早却后到达
    show("先到的失败通知", hub.ingest_callback(failure))
    show("后到的成功回调", hub.ingest_callback(success))
    show("重复投递同一回调", hub.ingest_callback(dict(success)))
    view = hub.task_view(task_id)
    show("任务有效结果（唯一）", {"status": view["status"], "winner": view["winning_attempt_id"]})
    show("两次尝试各计一次费", provider.charges)

    section("4. 摘要与声明格式核验：不符的文件进隔离区，不进候选区")
    res = hub.submit_generation("SH-03", "costume-linxue", 1, [("char-linxue", 1)], "fake", actor="分镜师")
    corrupt = (FIXTURES / "assets" / "corrupt.png").read_bytes()
    provider.plan(res["attempt_id"], PlannedResult(
        status="succeeded", file_bytes=corrupt,
        declared_sha256="c" * 64, declared_format="png"))  # 声明摘要与文件不符
    show("摘要造假的回调", hub.ingest_callback(provider.make_callback(res["attempt_id"])))
    show("隔离区", hub.store.all("SELECT attempt_id, reason FROM quarantine"))
    show("候选区（仍为空）", hub.store.all("SELECT asset_id FROM candidates WHERE shot_id = 'SH-03'"))
    try2 = hub.retry_task(res["task_id"], actor="分镜师", reason="供应商文件损坏，重跑")
    sha_sh03 = plan_success(provider, try2)
    show("重跑后合格入库", hub.ingest_callback(provider.make_callback(try2)))

    section("5. 人工批注与采用：批准 → 采用，人和理由留痕")
    for shot, asset in (("SH-01", asset_id(sha_sh01)), ("SH-02", asset_id(sha_sh02)), ("SH-03", asset_id(sha_sh03))):
        hub.annotate(asset, actor="总监", action="APPROVE", reason="成片抽查通过")
        hub.adopt(shot, asset, actor="总监", reason="首版采用")
    print("  三个镜头均已批准并采用")
    hub.build_composite("EP01", "第一集成片", ["SH-01", "SH-02", "SH-03"], actor="合成师")
    print("  合成产物 EP01 v1 已构建")

    section("6. 变更影响：替换角色参考图 + 废弃服装提示词")
    hub.revise_reference("char-linxue", "b" * 64, "image/png", actor="美术")
    report = hub.replace_reference("char-linxue", 1, 2, actor="总监", reason="角色设定更新")
    show("替换参考图的影响", report)
    report2 = hub.retire_prompt_pack("costume-linxue", 1, actor="总监", reason="服装提示词废弃")
    show("废弃提示词的影响", report2)
    adoption = hub.store.one("SELECT * FROM adoptions WHERE shot_id = 'SH-01' AND active = 1")
    asset = hub.store.one("SELECT * FROM candidates WHERE asset_id = ?", (adoption["asset_id"],))
    show("已批准资产未被自动覆盖", {"adoption": adoption["asset_id"], "state": asset["state"]})
    show("需要返工但尚未重跑的范围", [
        f"{i['target_kind']}:{i['target_id']} ← {i['kind']}" for i in hub.rework_scope()
    ])

    section("7. 重跑与重新采用（留人和理由），返工范围随之收敛")
    hub.revise_prompt_pack("costume-linxue", {"prompt": "黑色长风衣, 雨夜霓虹", "negative": "模糊"}, actor="导演")
    for shot in ("SH-01", "SH-02", "SH-03"):
        res = hub.submit_generation(shot, "costume-linxue", 2, [("char-linxue", 2)], "fake", actor="分镜师")
        new_sha = plan_success(provider, res["attempt_id"])
        hub.ingest_callback(provider.make_callback(res["attempt_id"]))
        hub.annotate(asset_id(new_sha), actor="总监", action="APPROVE", reason="新设定下重出合格")
        hub.adopt(shot, asset_id(new_sha), actor="总监", reason="参考图与服装提示词更新后重出")
        print(f"  {shot} 已用 char-linxue@2 + costume-linxue@2 重跑并重新采用")
    hub.build_composite("EP01", "第一集成片", ["SH-01", "SH-02", "SH-03"], actor="合成师")
    show("剩余返工范围", hub.rework_scope())

    section("8. 血缘反查：从成片镜头反查全部输入、尝试与审批")
    lineage = hub.composite_lineage("EP01")
    print(f"  EP01 当前版本 v{lineage['current_version']}，成分：")
    for entry in lineage["versions"][-1]["built_from"]:
        node = lineage["assets"][entry["asset_id"]]
        frozen = node["task"]["frozen"]
        print(f"    {entry['shot_id']} ← {entry['asset_id']}")
        print(f"      输入: {frozen['prompt_pack']['pack_id']}@{frozen['prompt_pack']['version']}"
              f" + {frozen['references'][0]['ref_id']}@{frozen['references'][0]['version']}")
        print(f"      尝试: {[(a['attempt_id'].split('-')[-1], a['status']) for a in node['attempts']]}")
        print(f"      审批: {node['annotations'][0]['author']}/{node['annotations'][0]['action']}"
              f"「{node['annotations'][0]['reason']}」，采用人 {node['adoption']['adopted_by']}"
              f"「{node['adoption']['reason']}」")

    section("9. 崩溃恢复：不重复计费、不丢失回调")
    recovery_demo()
    print("\n全部验收演示完成。")


def recovery_demo() -> None:
    """独立库上演示三种中断点的恢复。"""
    tmp = tempfile.TemporaryDirectory()
    clock = Clock()

    class CrashyProvider(FakeProvider):
        crashed = False

        def submit(self, request):
            job = super().submit(request)  # 供应商已受理、已计费
            if not self.crashed:
                self.crashed = True
                raise ConnectionError("进程崩溃：响应丢失")
            return job

    # 场景一：提交后、响应落库前崩溃 → 按幂等键重提，不重复计费
    db = Path(tmp.name) / "crash1.db"
    provider = CrashyProvider(clock=clock)
    hub = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
    hub.import_shots(json.loads((FIXTURES / "shots.json").read_text(encoding="utf-8")))
    hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
    hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
    try:
        hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
    except ConnectionError as exc:
        print(f"  [场景一] 提交瞬间崩溃：{exc}")
    hub.store.close()
    hub2 = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
    report = hub2.recover()
    print(f"  [场景一] 恢复重提 {len(report['resubmitted'])} 个尝试；"
          f"供应商被调用 {provider.submit_calls} 次，计费流水仍只有 {len(provider.charges)} 笔")
    hub2.store.close()

    # 场景二：回调已落库未处理即崩溃 → 恢复时重放，不丢回调
    db2 = Path(tmp.name) / "crash2.db"
    provider2 = FakeProvider(clock=clock)
    hub = AssetHub(Store(db2), {"fake": provider2}, clock=clock, default_timeout_seconds=60)
    hub.import_shots(json.loads((FIXTURES / "shots.json").read_text(encoding="utf-8")))
    hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
    hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
    res = hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
    provider2.plan(res["attempt_id"], PlannedResult(status="succeeded", file_bytes=b"\x89PNG\r\n\x1a\nimg"))
    hub.receive_callback(provider2.make_callback(res["attempt_id"]))  # 落库后崩溃
    hub.store.close()
    hub3 = AssetHub(Store(db2), {"fake": provider2}, clock=clock, default_timeout_seconds=60)
    report = hub3.recover()
    candidates = hub3.store.all("SELECT * FROM candidates")
    print(f"  [场景二] 恢复重放未处理回调 {report['events_processed']} 个，候选区 {len(candidates)} 张；"
          f"再次恢复重放 {hub3.recover()['events_processed']} 个（不重复）")
    hub3.store.close()

    # 场景三：回调在途中进程崩溃 → 恢复时向供应商回收结果
    db3 = Path(tmp.name) / "crash3.db"
    provider3 = FakeProvider(clock=clock)
    hub = AssetHub(Store(db3), {"fake": provider3}, clock=clock, default_timeout_seconds=60)
    hub.import_shots(json.loads((FIXTURES / "shots.json").read_text(encoding="utf-8")))
    hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
    hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
    res = hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
    provider3.plan(res["attempt_id"], PlannedResult(status="succeeded", file_bytes=b"\x89PNG\r\n\x1a\nimg"))
    hub.store.close()  # 回调尚未到达即崩溃
    hub4 = AssetHub(Store(db3), {"fake": provider3}, clock=clock, default_timeout_seconds=60)
    report = hub4.recover()
    dup = hub4.ingest_callback(provider3.make_callback(res["attempt_id"]))
    print(f"  [场景三] 恢复回收在途任务 {report['reconciled']}；迟到的真实回调判重={dup['duplicate']}；"
          f"计费流水 {len(provider3.charges)} 笔")
    hub4.store.close()


if __name__ == "__main__":
    main()
