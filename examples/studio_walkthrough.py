"""业务全流程走查：从镜头拆解到返工闭环。

场景：成片抽查发现 S010 精修图沿用了已废弃的“红色风衣”提示词。
本脚本演示中枢如何让问题可定位、返工范围可计算、重跑结果可验收。

运行：``python3 examples/studio_walkthrough.py``
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asset_hub import AssetHub, Inbox, Lineage, Worker
from asset_hub.media import sha256_hex, synthetic_png
from asset_hub.store import Store

DEMO_ROOT = Path("/tmp/asset-hub-walkthrough")


def generate_image(hub, inbox, shot_id, rgb, *, params=None, event_prefix="ev"):
    """走完整生成链路：冻结 -> 派发 -> 回调信封 -> 字节校验 -> 候选。"""
    params = params or {"steps": 20, "ratio": "9:16"}
    prep = hub.prepare_generation(shot_id, "echo-image", params, "image/png")
    hub.dispatch(prep["attempt_id"])
    png = synthetic_png(9, 16, rgb)
    digest = sha256_hex(png)
    envelope = {
        "event_id": f"{event_prefix}-{prep['attempt_id'][-6:]}",
        "attempt_id": prep["attempt_id"], "status": "succeeded",
        "occurred_at": "2026-09-10T05:00:58Z", "asset_sha256": digest,
        "width": 1080, "height": 1920,
    }
    import base64

    inbox.enqueue({"kind": "callback", "envelope": envelope})
    inbox.enqueue({"kind": "bytes", "attempt_id": prep["attempt_id"],
                   "declared_sha256": digest, "declared_media_type": "image/png",
                   "event_id": envelope["event_id"],
                   "data_b64": base64.b64encode(png).decode()})
    Worker(hub, inbox).run_once()
    cid = hub.store.query_one(
        "SELECT candidate_id FROM candidates WHERE attempt_id=:a", a=prep["attempt_id"]
    )["candidate_id"]
    return prep["attempt_id"], cid, png


def main() -> int:
    shutil.rmtree(DEMO_ROOT, ignore_errors=True)
    store = Store(DEMO_ROOT)
    hub = AssetHub(store)
    inbox = Inbox(DEMO_ROOT / "inbox")
    lineage = Lineage(hub)

    # 1. 镜头拆解 + 提示词包 + 跨镜头共享的角色参考 -----------------------
    hub.add_shot("shot-s010", "S010", "雨夜天桥", "主角转身特写")
    hub.add_shot("shot-s011", "S011", "雨夜巷口", "主角远去全景")
    for sid in ("shot-s010", "shot-s011"):
        hub.add_prompt_package(
            sid, f"pkg-{sid}",
            "电影感，{scene}；角色服装：{costume}；情绪：{mood}",
            {"scene": "雨夜", "costume": "红色风衣", "mood": "克制"},
        )
        hub.bind_reference(sid, "ref-hero", "hero_costume")
    hub.add_reference("ref-hero", "hero_costume", synthetic_png(9, 16, (170, 30, 30)),
                      created_by="art-director")

    # 2. 两镜生成、采用、下游合成 ------------------------------------------
    _, cid_s010, png1 = generate_image(hub, inbox, "shot-s010", (160, 40, 40), event_prefix="v1")
    _, cid_s011, _ = generate_image(hub, inbox, "shot-s011", (150, 50, 50), event_prefix="v1")
    hub.adopt("shot-s010", cid_s010, "director-lin", "初版精修通过")
    hub.adopt("shot-s011", cid_s011, "director-lin", "初版精修通过")
    hub.register_composite("comp-s010-v1", "shot-s010", "video", b"fake-mp4-s010",
                           {"fps": 24}, [(cid_s010, "hero_plate")])
    hub.register_composite("comp-s011-v1", "shot-s011", "video", b"fake-mp4-s011",
                           {"fps": 24}, [(cid_s011, "hero_plate")])

    # 3. 抽查出事：美术总监否决 S010，并修订提示词/替换参考 -----------------
    print("=" * 68)
    print("抽查结论：S010 沿用已废弃的红色风衣提示词")
    print("=" * 68)
    hub.reject_candidate(cid_s010, "director-gao",
                         "成片抽查：服装仍为红色风衣，与已废弃设定一致，否决")
    hub.replace_reference("ref-hero", synthetic_png(9, 16, (35, 60, 110)),
                          reviewer="director-gao",
                          reason="角色服装定稿为墨蓝工装，红风衣参考废弃")
    hub.revise_prompt(
        "shot-s010", "pkg-shot-s010",
        "电影感，{scene}；角色服装：{costume}；情绪：{mood}",
        {"scene": "雨夜", "costume": "墨蓝工装", "mood": "克制"},
        deprecated=["costume:红色风衣"],
        reviewer="director-gao",
        reason="废弃红色服装提示词，按定稿墨蓝工装修正",
    )

    # 4. 返工单不再只写“重做”：列出受影响且未重跑的范围 ---------------------
    print("\n--- 待返工范围（stale_work）---")
    stale = lineage.stale_work()
    for ch in stale:
        for s in ch["affected"]:
            print(f"变更[{ch['kind']}] -> 镜头 {s['shot_id']}")
            for r in s["reruns"]:
                print(f"    需用适配器 {r['adapter']} 重跑，当前状态: {r['state']}")
            for c in s["composites"]:
                print(f"    受污染合成产物: {c['product_id']} "
                      f"({', '.join(t['because'] for t in c['tainted_inputs'])})")

    # 5. 只重跑 S010（S011 尚未处理，仍挂在返工单上）-----------------------
    _, cid_s010_v2, _ = generate_image(hub, inbox, "shot-s010", (35, 60, 110), event_prefix="v2")
    hub.adopt("shot-s010", cid_s010_v2, "director-gao",
              "按墨蓝工装定稿重跑，复核服装与参考一致")
    hub.register_composite("comp-s010-v2", "shot-s010", "video", b"fake-mp4-s010-v2",
                           {"fps": 24}, [(cid_s010_v2, "hero_plate")])

    print("\n--- S010 闭环后的待返工范围 ---")
    still_open = [s for ch in lineage.stale_work() for s in ch["affected"]]
    print("仍挂起镜头:", sorted({s["shot_id"] for s in still_open}))
    assert {s["shot_id"] for s in still_open} == {"shot-s011"}

    # 6. 从成片镜头反查全部输入、尝试与审批 ---------------------------------
    print("\n--- S010 血缘反查（trace_shot 摘要）---")
    trace = lineage.trace_shot("shot-s010")
    print("提示词版本:", [(v["version"], v["variables"]["costume"], v["deprecated"])
                         for v in trace["prompt_packages"]])
    print("参考版本:", {rid: [(x["version"], x["sha256"][:8]) for x in vs]
                         for rid, vs in trace["references"].items()})
    for a in trace["attempts"]:
        print(f"尝试 {a['attempt_id']}: status={a['status']} "
              f"prompt_v={a['frozen_input']['prompt_version']} "
              f"ref_v={a['frozen_input']['references'][0][1]} "
              f"任务号={a['dispatch_id']} 计费传输次数={a['attempts_made']}")
    print("审批轨迹:")
    for ap in trace["approvals"]:
        print(f"  [{ap['kind']}] {ap['candidate_id'][:20]}… by {ap['reviewer']}：{ap['reason']}")
    print("当前生效候选:", trace["active_adoption"]["candidate_id"])
    print("关联合成产物:", [c["product_id"] for c in trace["composites"]])

    print("\n✅ 走查完成：问题可定位、返工范围可计算、每次采用都有人和理由。")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
