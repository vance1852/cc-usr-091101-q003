"""人工采用、否决、重新采用与已批准资产保护。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import HubError
from asset_hub.media import sha256_hex, synthetic_png

from _base import HubCase


def make_candidate(hub, shot_id="shot-1", rgb=(1, 2, 3)):
    prep = hub.prepare_generation(shot_id, "echo-image", {"steps": 20}, "image/png")
    hub.dispatch(prep["attempt_id"])
    attempt_id = prep["attempt_id"]
    png = synthetic_png(8, 8, rgb)
    digest = sha256_hex(png)
    hub.receive_callback({"event_id": f"ev-{digest[:8]}", "attempt_id": attempt_id,
                          "status": "succeeded", "occurred_at": "2026-09-10T08:00:00Z",
                          "asset_sha256": digest})
    r = hub.deliver_result_bytes(attempt_id, png, declared_sha256=digest,
                                 declared_media_type="image/png")
    return attempt_id, r["candidate_id"], png, digest


class AdoptionTest(HubCase):
    def test_adopt_requires_person_and_reason(self):
        self.seed_shot()
        _, cid, _, _ = make_candidate(self.hub)
        with self.assertRaises(HubError):
            self.hub.adopt("shot-1", cid, "", "理由")
        with self.assertRaises(HubError):
            self.hub.adopt("shot-1", cid, "director-lin", "")

    def test_approved_asset_is_not_auto_overwritten(self):
        self.seed_shot()
        _, cid1, _, _ = make_candidate(self.hub, rgb=(10, 20, 30))
        self.hub.adopt("shot-1", cid1, "director-lin", "神情与服装设定一致")
        # 新候选到达不会自动顶替
        _, cid2, _, _ = make_candidate(self.hub, rgb=(40, 50, 60))
        active = self.store.query_one(
            "SELECT candidate_id FROM active_adoptions WHERE shot_id='shot-1'")
        self.assertEqual(active["candidate_id"], cid1)
        # 直接 adopt 第二张被拒，必须走显式替换并记录理由
        with self.assertRaises(HubError):
            self.hub.adopt("shot-1", cid2, "director-lin", "想换")
        self.hub.replace_adoption("shot-1", cid2, "director-lin",
                                  "客户反馈要求更冷的色调")
        active = self.store.query_one(
            "SELECT candidate_id FROM active_adoptions WHERE shot_id='shot-1'")
        self.assertEqual(active["candidate_id"], cid2)
        # 历史完整：首次采用 + 替换都有人和理由
        rows = self.store.query_all(
            "SELECT kind, candidate_id, replaced_candidate_id, reviewer, reason "
            "FROM adoptions WHERE shot_id='shot-1' ORDER BY id")
        self.assertEqual([r["kind"] for r in rows], ["adopt", "replace"])
        self.assertEqual(rows[1]["replaced_candidate_id"], cid1)
        self.assertTrue(all(r["reviewer"] and r["reason"] for r in rows))
        # 旧候选退回候选区而非被删除
        self.assertEqual(
            self.store.query_one("SELECT status FROM candidates WHERE candidate_id=:c", c=cid1)["status"],
            "available")

    def test_reject_active_candidate_clears_adoption_and_records_change(self):
        self.seed_shot()
        _, cid, _, _ = make_candidate(self.hub)
        self.hub.adopt("shot-1", cid, "director-lin", "初版通过")
        self.hub.reject_candidate(cid, "director-lin",
                                  "抽查发现沿用废弃红风衣提示词")
        self.assertIsNone(self.store.query_one(
            "SELECT 1 FROM active_adoptions WHERE shot_id='shot-1'"))
        self.assertEqual(
            self.store.query_one("SELECT status FROM candidates WHERE candidate_id=:c", c=cid)["status"],
            "rejected")
        changes = self.store.query_all("SELECT kind FROM changes")
        self.assertIn("candidate_rejected", [r["kind"] for r in changes])

    def test_readopt_rejected_requires_fresh_person_and_reason(self):
        self.seed_shot()
        _, cid, _, _ = make_candidate(self.hub)
        self.hub.adopt("shot-1", cid, "director-lin", "通过")
        self.hub.reject_candidate(cid, "director-lin", "服装错误")
        with self.assertRaises(HubError):
            self.hub.readopt_rejected("shot-1", cid, "", "重新采用")
        self.hub.readopt_rejected("shot-1", cid, "director-gao",
                                  "复核确认该批次提示词已修正，重新采用")
        active = self.store.query_one(
            "SELECT candidate_id FROM active_adoptions WHERE shot_id='shot-1'")
        self.assertEqual(active["candidate_id"], cid)
        rows = self.store.query_all(
            "SELECT reviewer, reason FROM adoptions WHERE shot_id='shot-1' ORDER BY id")
        self.assertEqual([r["reviewer"] for r in rows],
                         ["director-lin", "director-lin", "director-gao"])

    def test_composite_only_accepts_adopted_inputs(self):
        self.seed_shot()
        _, cid, png, digest = make_candidate(self.hub)
        with self.assertRaises(HubError):
            self.hub.register_composite(
                "comp-1", "shot-1", "video", b"mp4bytes", {"fps": 24},
                [(cid, "hero_plate")])
        self.hub.adopt("shot-1", cid, "director-lin", "通过")
        out = self.hub.register_composite(
            "comp-1", "shot-1", "video", b"mp4bytes", {"fps": 24},
            [(cid, "hero_plate")])
        self.assertEqual(out["product_id"], "comp-1")
