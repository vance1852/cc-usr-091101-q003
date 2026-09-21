"""血缘反查：从镜头/成片反查全部输入、尝试与审批。"""

from hub_factory import HubCase, PlannedResult


class LineageTest(HubCase):
    def _produce(self, shot: str) -> tuple[str, str]:
        """一次超时重试后成功，批准并采用，返回 (task_id, asset_id)。"""
        res = self.submit(shot)
        task_id, try1 = res["task_id"], res["attempt_id"]
        self.provider.plan(try1, PlannedResult(status="failed", error_code="TIMEOUT"))
        self.hub.ingest_callback(self.provider.make_callback(try1))
        try2 = self.hub.retry_task(task_id, actor="分镜师", reason="超时重跑")
        asset_id = self.plan_ok(try2)
        self.hub.ingest_callback(self.provider.make_callback(try2))
        self.approve_and_adopt(shot, asset_id, reason="二版通过")
        return task_id, asset_id

    def test_shot_lineage_traces_inputs_attempts_and_approvals(self):
        task_id, asset_id = self._produce("SH-01")
        lineage = self.hub.shot_lineage("SH-01")
        self.assertEqual(lineage["shot"]["shot_id"], "SH-01")
        # 输入：冻结的提示词包与参考图版本
        task = next(t for t in lineage["tasks"] if t["task_id"] == task_id)
        self.assertEqual(task["frozen"]["prompt_pack"], {"pack_id": "costume-linxue", "version": 1})
        self.assertEqual(task["frozen"]["references"][0]["ref_id"], "char-linxue")
        self.assertEqual(task["winning_attempt_id"], f"{task_id}-a2")
        # 尝试：两次，第一次失败、第二次成功，各计一次费
        self.assertEqual([a["seq"] for a in task["attempts"]], [1, 2])
        self.assertEqual(task["attempts"][0]["status"], "FAILED")
        self.assertEqual(task["attempts"][1]["status"], "SUCCEEDED")
        self.assertTrue(all(a["billed"] == 1 for a in task["attempts"]))
        # 每次尝试的回调事件可追溯
        self.assertEqual(task["attempts"][0]["events"][0]["outcome"], "ATTEMPT_FAILED")
        self.assertEqual(task["attempts"][1]["events"][0]["outcome"], "ACCEPTED")
        # 审批：批准人与理由、采用人与理由
        approvals = [a for a in lineage["annotations"] if a["action"] == "APPROVE"]
        self.assertEqual(approvals[0]["author"], "总监")
        self.assertEqual(approvals[0]["reason"], "二版通过")
        adoption = lineage["adoptions"][-1]
        self.assertEqual(adoption["asset_id"], asset_id)
        self.assertEqual(adoption["adopted_by"], "总监")
        self.assertEqual(adoption["reason"], "二版通过")

    def test_composite_lineage_traces_every_ingredient(self):
        t1, asset1 = self._produce("SH-01")
        t3, asset3 = self._produce("SH-03")
        self.hub.build_composite("EP01", "第一集成片", ["SH-01", "SH-03"], actor="合成师")
        lineage = self.hub.composite_lineage("EP01")
        self.assertEqual(lineage["current_version"], 1)
        built_from = lineage["versions"][0]["built_from"]
        self.assertEqual({e["shot_id"] for e in built_from}, {"SH-01", "SH-03"})
        # 每个成分资产都能反查到冻结输入、尝试与审批
        for asset_id, task_id in ((asset1, t1), (asset3, t3)):
            node = lineage["assets"][asset_id]
            self.assertEqual(node["task"]["task_id"], task_id)
            self.assertEqual(node["task"]["frozen"]["prompt_pack"]["version"], 1)
            self.assertEqual(len(node["attempts"]), 2)
            self.assertEqual(node["annotations"][0]["action"], "APPROVE")
            self.assertEqual(node["adoption"]["adopted_by"], "总监")

    def test_lineage_marks_stale_shots_after_change(self):
        self._produce("SH-01")
        self.hub.revise_reference("char-linxue", "b" * 64, "image/png", actor="美术")
        self.hub.replace_reference("char-linxue", 1, 2, actor="总监", reason="设定更新")
        lineage = self.hub.shot_lineage("SH-01")
        self.assertEqual(len(lineage["invalidations"]), 1)
        self.assertFalse(lineage["invalidations"][0]["cleared"])
