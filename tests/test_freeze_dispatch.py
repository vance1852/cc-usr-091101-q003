"""冻结输入、幂等任务号、超时重试与崩溃恢复（计费语义）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import FlakyVideoAdapter, RetriesExhausted

from _base import HubCase


class FreezeDispatchTest(HubCase):
    def test_inputs_are_frozen_and_digest_covers_all_inputs(self):
        self.seed_shot()
        prep1 = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 20}, "image/png")
        prep2 = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 20}, "image/png")
        self.assertEqual(prep1["attempt_id"], prep2["attempt_id"])
        self.assertFalse(prep2["created"])

        # 参数变化 → 不同冻结摘要 → 新尝试
        prep3 = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 30}, "image/png")
        self.assertNotEqual(prep1["attempt_id"], prep3["attempt_id"])

        # 事后修订提示词不会改写历史尝试的冻结快照
        self.hub.revise_prompt(
            "shot-1", "pkg-shot-1",
            "电影感特写，角色服装：{costume}；情绪：{mood}",
            {"costume": "墨蓝工装", "mood": "克制"},
            deprecated=["costume:红色风衣"], reviewer="director-lin",
            reason="废弃红风衣设定，改用墨蓝工装",
        )
        trace = self.lineage.trace_shot("shot-1")
        first = next(a for a in trace["attempts"] if a["attempt_id"] == prep1["attempt_id"])
        self.assertEqual(first["frozen_input"]["prompt_version"], 1)

    def test_dispatch_id_is_stable_and_billed_once(self):
        self.seed_shot()
        attempt_id, d1 = self.prepare_dispatch_ok()
        self.assertTrue(d1["new_call"])
        d2 = self.hub.dispatch(attempt_id)
        d3 = self.hub.dispatch(attempt_id)
        self.assertFalse(d2["new_call"])
        self.assertEqual(d1["dispatch_id"], d2["dispatch_id"])
        self.assertEqual(d2["dispatch_id"], d3["dispatch_id"])
        rows = self.store.query_all("SELECT * FROM dispatches WHERE attempt_id=:a", a=attempt_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls_made"], 1)

    def test_timeout_retries_keep_same_key_and_one_task_number(self):
        self.seed_shot()
        flaky = FlakyVideoAdapter(fail_times=1)
        self.hub.set_adapter_instance("flaky-video", flaky)
        prep = self.hub.prepare_generation("shot-1", "flaky-video", {"fps": 24}, "video/mp4")
        result = self.hub.dispatch(prep["attempt_id"], max_transport_attempts=3)
        self.assertTrue(result["new_call"])
        self.assertEqual(result["calls_made"], 2)  # 1 次超时 + 1 次成功
        rows = self.store.query_all(
            "SELECT * FROM dispatches WHERE attempt_id=:a", a=prep["attempt_id"]
        )
        self.assertEqual(len(rows), 1)  # 只有一个计费任务
        self.assertEqual(rows[0]["state"], "recorded")
        self.assertEqual(flaky._new_calls, 2)

    def test_restart_with_fresh_adapter_reuses_task_number(self):
        """模拟进程重启：内存适配器换成全新实例，任务号仍由同一幂等键解析。"""
        self.seed_shot()
        self.hub.set_adapter_instance("flaky-video", FlakyVideoAdapter(fail_times=5))
        prep = self.hub.prepare_generation("shot-1", "flaky-video", {"fps": 24}, "video/mp4")
        with self.assertRaises(RetriesExhausted):
            self.hub.dispatch(prep["attempt_id"], max_transport_attempts=2)
        row = self.store.query_one(
            "SELECT * FROM dispatches WHERE attempt_id=:a", a=prep["attempt_id"]
        )
        self.assertEqual(row["state"], "in_flight")
        key_before = row["idempotency_key"]

        # “重启”：新中枢实例挂全新适配器（不再故障）
        self.hub.set_adapter_instance("flaky-video", FlakyVideoAdapter(fail_times=0))
        from asset_hub import recover_in_flight

        report = recover_in_flight(self.hub)
        self.assertEqual(len(report), 1)
        self.assertTrue(report[0]["recovered"])
        row2 = self.store.query_one(
            "SELECT * FROM dispatches WHERE attempt_id=:a", a=prep["attempt_id"]
        )
        self.assertEqual(row2["idempotency_key"], key_before)
        self.assertEqual(row2["state"], "recorded")
        self.assertEqual(row2["calls_made"], 3)  # 2 次崩溃前 + 1 次恢复

    def test_identical_frozen_input_after_restart_returns_same_attempt(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        again = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 20}, "image/png")
        self.assertEqual(again["attempt_id"], attempt_id)
