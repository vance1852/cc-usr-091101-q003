"""血缘反查与变更影响面（返工范围）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub.media import sha256_hex, synthetic_png

from _base import HubCase


def successful_candidate(hub, shot_id, rgb, *, adapter="echo-image", params=None):
    params = params or {"steps": 20}
    prep = hub.prepare_generation(shot_id, adapter, params, "image/png")
    png = synthetic_png(8, 8, rgb)
    digest = sha256_hex(png)
    hub.receive_callback({"event_id": f"ev-{shot_id}-{digest[:6]}", "attempt_id": prep["attempt_id"],
                          "status": "succeeded", "occurred_at": "2026-09-10T09:00:00Z",
                          "asset_sha256": digest})
    hub.deliver_result_bytes(prep["attempt_id"], png, declared_sha256=digest,
                             declared_media_type="image/png")
    return prep["attempt_id"], f"cand-{prep['attempt_id'][-8:]}-{digest[:12]}", png, digest


class LineageTraceTest(HubCase):
    def test_trace_shot_covers_inputs_attempts_approvals_and_composites(self):
        self.seed_shot()
        attempt_id, cid, _, _ = successful_candidate(self.hub, "shot-1", (7, 7, 7))
        self.hub.adopt("shot-1", cid, "director-lin", "验收通过")
        self.hub.register_composite(
            "comp-1", "shot-1", "video", b"fake-mp4", {"fps": 24}, [(cid, "plate")])

        trace = self.lineage.trace_shot("shot-1")
        self.assertEqual(trace["shot"]["code"], "S010")
        self.assertEqual(trace["prompt_packages"][0]["version"], 1)
        self.assertEqual(trace["references"]["ref-hero"][0]["version"], 1)
        att = trace["attempts"][0]
        self.assertEqual(att["attempt_id"], attempt_id)
        self.assertEqual(att["frozen_input"]["prompt_version"], 1)
        self.assertEqual(att["frozen_input"]["references"][0][0], "ref-hero")
        self.assertEqual(att["candidates"][0]["candidate_id"], cid)
        self.assertEqual(trace["active_adoption"]["candidate_id"], cid)
        comp = trace["composites"][0]
        self.assertEqual(comp["inputs"][0]["candidate_id"], cid)
        self.assertEqual(comp["inputs"][0]["frozen_digest"], att["frozen_digest"])
        self.assertEqual(trace["approvals"][0]["reviewer"], "director-lin")

    def test_deprecated_costume_prompt_stays_in_history(self):
        self.seed_shot(costume="红色风衣")
        self.hub.revise_prompt(
            "shot-1", "pkg-shot-1",
            "电影感特写，角色服装：{costume}；情绪：{mood}",
            {"costume": "墨蓝工装", "mood": "克制"},
            deprecated=["costume:红色风衣"], reviewer="director-lin",
            reason="红风衣设定废弃",
        )
        trace = self.lineage.trace_shot("shot-1")
        versions = trace["prompt_packages"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertIn("costume:红色风衣", versions[1]["deprecated"])


class ImpactAnalysisTest(HubCase):
    def _two_shots_shared_reference(self):
        self.hub.add_shot("shot-1", "S010", "天桥", "特写")
        self.hub.add_shot("shot-2", "S011", "巷口", "全景")
        for sid in ("shot-1", "shot-2"):
            self.hub.add_prompt_package(
                sid, f"pkg-{sid}", "镜头：角色穿{costume}", {"costume": "红风衣"})
            self.hub.bind_reference(sid, "ref-hero", "hero_costume")
        ref = self.hub.add_reference(
            "ref-hero", "hero_costume", synthetic_png(8, 16, (180, 30, 30)),
            created_by="art-director")
        return ref

    def test_reference_replacement_lists_affected_shots_and_clears_after_rerun(self):
        self._two_shots_shared_reference()
        _, cid1, _, _ = successful_candidate(self.hub, "shot-1", (1, 1, 1))
        _, cid2, _, _ = successful_candidate(self.hub, "shot-2", (2, 2, 2))
        self.hub.adopt("shot-1", cid1, "director-lin", "通过")
        self.hub.adopt("shot-2", cid2, "director-lin", "通过")
        self.hub.register_composite(
            "comp-1", "shot-1", "video", b"v1", {}, [(cid1, "plate")])

        self.hub.replace_reference(
            "ref-hero", synthetic_png(8, 16, (20, 40, 90)),
            reviewer="director-gao", reason="角色服装改为墨蓝工装，旧参考废弃")

        stale = self.lineage.stale_work()
        self.assertEqual(len(stale), 1)
        change = stale[0]
        self.assertEqual(change["kind"], "reference_replaced")
        affected = {s["shot_id"]: s for s in change["affected"]}
        self.assertEqual(set(affected), {"shot-1", "shot-2"})
        self.assertEqual(affected["shot-1"]["reruns"][0]["state"], "not_rerun")
        comps = affected["shot-1"]["composites"]
        self.assertEqual(comps[0]["product_id"], "comp-1")
        self.assertEqual(comps[0]["tainted_inputs"][0]["because"], "stale_frozen_input")

        # 用当前（新参考）输入重跑 shot-1 并采用、重渲染合成
        _, new_cid, _, _ = successful_candidate(self.hub, "shot-1", (3, 3, 3))
        self.hub.replace_adoption("shot-1", new_cid, "director-gao",
                                  "按新服装参考重跑后替换")
        self.hub.register_composite(
            "comp-2", "shot-1", "video", b"v2", {}, [(new_cid, "plate")])

        stale = self.lineage.stale_work()
        affected = {s["shot_id"]: s for s in stale[0]["affected"]}
        self.assertNotIn("shot-1", affected)          # 已闭环
        self.assertIn("shot-2", affected)             # 仍待返工

    def test_prompt_revision_marks_old_attempts_stale(self):
        self.seed_shot(costume="红色风衣")
        successful_candidate(self.hub, "shot-1", (9, 9, 9))
        self.hub.revise_prompt(
            "shot-1", "pkg-shot-1",
            "电影感特写，角色服装：{costume}；情绪：{mood}",
            {"costume": "墨蓝工装", "mood": "克制"},
            deprecated=["costume:红色风衣"], reviewer="director-lin", reason="纠正废弃服装词")
        stale = self.lineage.stale_work()
        self.assertEqual(stale[0]["kind"], "prompt_revised")
        self.assertEqual(stale[0]["affected"][0]["reruns"][0]["state"], "not_rerun")

        successful_candidate(self.hub, "shot-1", (11, 11, 11))
        self.assertEqual(self.lineage.stale_work(), [])  # 重跑闭环

    def test_rejected_adopted_candidate_taints_composites_until_replaced(self):
        self.seed_shot()
        _, cid, _, _ = successful_candidate(self.hub, "shot-1", (4, 4, 4))
        self.hub.adopt("shot-1", cid, "director-lin", "通过")
        self.hub.register_composite(
            "comp-1", "shot-1", "video", b"v1", {}, [(cid, "plate")])
        self.hub.reject_candidate(cid, "director-gao",
                                  "精修沿用废弃服装提示词，否决")
        stale = self.lineage.stale_work()
        kinds = {c["kind"] for c in stale}
        self.assertIn("candidate_rejected", kinds)
        rej = next(c for c in stale if c["kind"] == "candidate_rejected")
        self.assertEqual(rej["affected"][0]["composites"][0]["product_id"], "comp-1")
        self.assertEqual(rej["affected"][0]["reruns"][0]["state"], "not_rerun")

        _, cid2, _, _ = successful_candidate(self.hub, "shot-1", (5, 5, 5))
        self.hub.adopt("shot-1", cid2, "director-gao", "复核提示词已修正，采用重跑结果")
        self.hub.register_composite(
            "comp-2", "shot-1", "video", b"v2", {}, [(cid2, "plate")])
        stale = self.lineage.stale_work()
        # 否决单与替换单都已闭环
        self.assertEqual(
            [c for c in stale if c["affected"][0]["composites"]], [])

    def test_in_progress_rerun_stays_on_worklist(self):
        self.seed_shot()
        successful_candidate(self.hub, "shot-1", (6, 6, 6))
        self.hub.revise_prompt(
            "shot-1", "pkg-shot-1",
            "电影感特写，角色服装：{costume}；情绪：{mood}",
            {"costume": "墨蓝工装", "mood": "克制"},
            reviewer="director-lin", reason="v2")
        prep = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 20}, "image/png")
        self.hub.dispatch(prep["attempt_id"])  # 已派发、未回调：进行中
        stale = self.lineage.stale_work()
        self.assertEqual(stale[0]["affected"][0]["reruns"][0]["state"], "in_progress")
