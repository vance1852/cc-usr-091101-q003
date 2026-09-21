"""变更影响计算与返工范围：替换参考、废弃提示词、否决资产。"""

from hub_factory import HubCase

from asset_hub import AssetState, HubError


class ImpactTest(HubCase):
    def _build_scene(self) -> None:
        """SH-01、SH-03 各出图并采用，合成 EP01。"""
        for shot in ("SH-01", "SH-03"):
            _, asset_id = self.run_success(shot=shot)
            self.approve_and_adopt(shot, asset_id)
        self.hub.build_composite("EP01", "第一集成片", ["SH-01", "SH-03"], actor="合成师")

    def test_replace_reference_computes_impact_without_overwriting(self):
        self._build_scene()
        self.hub.revise_reference("char-linxue", "b" * 64, "image/png", actor="美术")
        report = self.hub.replace_reference("char-linxue", 1, 2, actor="总监", reason="角色设定更新")
        self.assertEqual(report["shots"], ["SH-01", "SH-03"])
        self.assertEqual(report["composites"], ["EP01"])
        # 已批准资产与采用关系不被自动覆盖
        for shot in ("SH-01", "SH-03"):
            adoption = self.store.one("SELECT * FROM adoptions WHERE shot_id = ? AND active = 1", (shot,))
            self.assertIsNotNone(adoption)
            asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (adoption["asset_id"],))
            self.assertEqual(asset["state"], AssetState.APPROVED.value)
        # 返工范围：两个镜头 + 一个合成产物，均未重跑
        scope = self.hub.rework_scope()
        self.assertEqual(
            {(i["target_kind"], i["target_id"]) for i in scope},
            {("SHOT", "SH-01"), ("SHOT", "SH-03"), ("COMPOSITE", "EP01")},
        )
        self.assertTrue(all(not i["cleared"] for i in scope))

    def test_rerun_and_rebuild_clears_rework_scope(self):
        self._build_scene()
        self.hub.revise_reference("char-linxue", "b" * 64, "image/png", actor="美术")
        report = self.hub.replace_reference("char-linxue", 1, 2, actor="总监", reason="角色设定更新")
        change_id = report["change_id"]
        # SH-01 用新参考重跑并重新采用（留人和理由）
        _, asset_id = self.run_success(shot="SH-01", ref_version=2)
        self.approve_and_adopt("SH-01", asset_id, reason="按新参考重出")
        pending = {(i["target_kind"], i["target_id"]) for i in self.hub.rework_scope()}
        self.assertEqual(pending, {("SHOT", "SH-03"), ("COMPOSITE", "EP01")})
        # SH-03 重跑后，合成产物仍引用旧任务，未清除
        _, asset3 = self.run_success(shot="SH-03", ref_version=2)
        self.approve_and_adopt("SH-03", asset3, reason="按新参考重出")
        pending = {(i["target_kind"], i["target_id"]) for i in self.hub.rework_scope()}
        self.assertEqual(pending, {("COMPOSITE", "EP01")})
        # 重建合成产物后全部清除
        self.hub.build_composite("EP01", "第一集成片", ["SH-01", "SH-03"], actor="合成师")
        self.assertEqual(self.hub.rework_scope(), [])
        # 含已清除项的完整视图仍可追溯
        full = self.hub.rework_scope(change_id=change_id, include_cleared=True)
        self.assertEqual(len(full), 3)
        self.assertTrue(all(i["cleared"] for i in full))

    def test_retire_prompt_pack_flags_consumers_and_blocks_reuse(self):
        self._build_scene()
        report = self.hub.retire_prompt_pack("costume-linxue", 1, actor="总监", reason="服装提示词废弃")
        self.assertEqual(report["shots"], ["SH-01", "SH-03"])
        self.assertEqual(report["composites"], ["EP01"])
        with self.assertRaises(HubError):
            self.submit()  # 废弃版本禁止新任务
        # 用新版本提示词重跑可清除
        self.hub.revise_prompt_pack(
            "costume-linxue", {"prompt": "黑色长风衣, 雨夜霓虹", "negative": "模糊"}, actor="导演"
        )
        _, asset_id = self.run_success(shot="SH-01", pack_version=2)
        self.approve_and_adopt("SH-01", asset_id, reason="换用新版服装提示词")
        pending = {(i["target_kind"], i["target_id"]) for i in self.hub.rework_scope()}
        self.assertNotIn(("SHOT", "SH-01"), pending)

    def test_reject_asset_flags_shot_and_composite(self):
        self._build_scene()
        adopted = self.store.one("SELECT * FROM adoptions WHERE shot_id = 'SH-01' AND active = 1")
        asset_id = adopted["asset_id"]
        self.hub.annotate(asset_id, actor="总监", action="REJECT", reason="手部崩坏")
        asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (asset_id,))
        self.assertEqual(asset["state"], AssetState.REJECTED.value)
        # 采用关系仍在（不自动覆盖），但镜头与合成产物进入返工范围
        still = self.store.one("SELECT * FROM adoptions WHERE shot_id = 'SH-01' AND active = 1")
        self.assertEqual(still["asset_id"], asset_id)
        pending = {(i["target_kind"], i["target_id"]) for i in self.hub.rework_scope()}
        self.assertEqual(pending, {("SHOT", "SH-01"), ("COMPOSITE", "EP01")})
        # 被否决资产不可采用
        with self.assertRaises(HubError):
            self.hub.adopt("SH-02", asset_id, actor="总监", reason="想复用")
        # 重跑新图并采用后清除
        self.hub.revise_prompt_pack("costume-linxue", {"prompt": "重绘手部"}, actor="导演")
        _, new_asset = self.run_success(shot="SH-01", pack_version=2)
        self.approve_and_adopt("SH-01", new_asset, reason="重绘后替换")
        pending = {(i["target_kind"], i["target_id"]) for i in self.hub.rework_scope()}
        self.assertEqual(pending, {("COMPOSITE", "EP01")})

    def test_adoption_requires_approval_person_and_reason(self):
        _, asset_id = self.run_success()
        with self.assertRaises(HubError):
            self.hub.adopt("SH-01", asset_id, actor="总监", reason="直接采用")  # 未批准
        self.hub.annotate(asset_id, actor="总监", action="APPROVE", reason="通过")
        with self.assertRaises(HubError):
            self.hub.adopt("SH-01", asset_id, actor="", reason="通过")
        with self.assertRaises(HubError):
            self.hub.adopt("SH-01", asset_id, actor="总监", reason="")
        with self.assertRaises(HubError):
            self.hub.annotate(asset_id, actor="总监", action="REJECT")  # 否决必须给理由

    def test_readoption_keeps_history_and_requires_reason(self):
        _, asset_a = self.run_success(shot="SH-01")
        first = self.approve_and_adopt("SH-01", asset_a, reason="首版通过")
        # 新图替换：旧采用进入历史，新采用留人和理由
        self.hub.revise_prompt_pack("costume-linxue", {"prompt": "调整构图"}, actor="导演")
        _, asset_b = self.run_success(shot="SH-01", pack_version=2)
        second = self.approve_and_adopt("SH-01", asset_b, reason="构图更稳，替换首版")
        history = self.store.all("SELECT * FROM adoptions WHERE shot_id = 'SH-01' ORDER BY adopted_at")
        self.assertEqual(len(history), 2)
        old, new = history
        self.assertEqual(old["active"], 0)
        self.assertEqual(old["superseded_by"], second["adoption_id"])
        self.assertEqual(new["active"], 1)
        self.assertEqual(new["adopted_by"], "总监")
        self.assertEqual(new["reason"], "构图更稳，替换首版")
        self.assertEqual(first["adoption_id"], old["adoption_id"])

    def test_composite_refuses_rejected_or_missing_assets(self):
        _, asset_id = self.run_success(shot="SH-01")
        self.approve_and_adopt("SH-01", asset_id)
        with self.assertRaises(HubError):
            self.hub.build_composite("EP01", "第一集", ["SH-01", "SH-03"], actor="合成师")  # SH-03 未采用
        self.hub.annotate(asset_id, actor="总监", action="REJECT", reason="重做")
        with self.assertRaises(HubError):
            self.hub.build_composite("EP01", "第一集", ["SH-01"], actor="合成师")  # 含被否决资产
