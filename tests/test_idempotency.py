"""幂等任务号与输入版本冻结。"""

from hub_factory import HubCase

from asset_hub import HubError


class IdempotencyTest(HubCase):
    def test_same_frozen_inputs_yield_same_task(self):
        first = self.submit()
        second = self.submit()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_id"], second["task_id"])
        # 不产生新尝试、不重复计费
        view = self.hub.task_view(first["task_id"])
        self.assertEqual(len(view["attempts"]), 1)
        self.assertEqual(self.provider.charges, [first["attempt_id"]])

    def test_changed_input_version_changes_task_id(self):
        self.hub.revise_prompt_pack(
            "costume-linxue", {"prompt": "蓝色风衣, 雨夜霓虹", "negative": "模糊"}, actor="导演"
        )
        t1 = self.submit(pack_version=1)
        t2 = self.submit(pack_version=2)
        self.assertNotEqual(t1["task_id"], t2["task_id"])

    def test_frozen_inputs_are_snapshot(self):
        task_id, _ = self.run_success()
        # 任务创建后再改提示词包，冻结快照不变
        self.hub.revise_prompt_pack("costume-linxue", {"prompt": "改版"}, actor="导演")
        view = self.hub.task_view(task_id)
        self.assertEqual(view["frozen"]["prompt_pack"], {"pack_id": "costume-linxue", "version": 1})
        self.assertEqual(view["frozen"]["references"][0]["version"], 1)

    def test_retired_pack_cannot_be_submitted(self):
        self.hub.retire_prompt_pack("costume-linxue", 1, actor="总监", reason="服装提示词废弃")
        with self.assertRaises(HubError):
            self.submit(pack_version=1)

    def test_superseded_reference_cannot_be_submitted(self):
        self.hub.revise_reference("char-linxue", "b" * 64, "image/png", actor="美术")
        self.hub.replace_reference("char-linxue", 1, 2, actor="总监", reason="角色设定更新")
        with self.assertRaises(HubError):
            self.submit(ref_version=1)

    def test_unknown_shot_or_pack_rejected(self):
        with self.assertRaises(HubError):
            self.hub.submit_generation("SH-99", "costume-linxue", 1, [], "fake")
        with self.assertRaises(HubError):
            self.hub.submit_generation("SH-01", "costume-linxue", 99, [], "fake")
