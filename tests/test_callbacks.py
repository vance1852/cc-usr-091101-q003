"""回调乱序、重复与超时重试：任何情况下任务至多一份有效结果。"""

from hub_factory import HubCase, PlannedResult

from asset_hub import AttemptStatus, TaskStatus


class CallbackOrderingTest(HubCase):
    def test_timeout_retry_then_late_failure_is_ignored(self):
        """try-1 超时重试出 try-2；try-2 成功后，try-1 迟到的失败通知不得破坏结果。"""
        res = self.submit()
        task_id, try1 = res["task_id"], res["attempt_id"]
        # try-1 静默超时 → 自动重试出 try-2
        self.clock.advance(120)
        timed_out = self.hub.sweep_timeouts()
        self.assertEqual(timed_out, [try1])
        try2 = f"{task_id}-a2"
        self.plan_ok(try2)
        # try-2 成功回调先处理
        out = self.hub.ingest_callback(self.provider.make_callback(try2))
        self.assertEqual(out["outcome"], "ACCEPTED")
        # try-1 的失败通知后到（occurred_at 甚至更晚），只被记录
        self.provider.plan(try1, PlannedResult(status="failed", error_code="TIMEOUT"))
        late = self.hub.ingest_callback(self.provider.make_callback(try1))
        self.assertEqual(late["outcome"], f"IGNORED_{AttemptStatus.TIMED_OUT.value}")
        view = self.hub.task_view(task_id)
        self.assertEqual(view["status"], TaskStatus.RESOLVED.value)
        self.assertEqual(view["winning_attempt_id"], try2)

    def test_early_failure_does_not_block_later_success(self):
        """先到的失败通知只终结它自己的尝试，任务仍等待在途尝试。"""
        res = self.submit()
        task_id, try1 = res["task_id"], res["attempt_id"]
        self.provider.plan(try1, PlannedResult(status="failed", error_code="CONTENT_POLICY"))
        out = self.hub.ingest_callback(self.provider.make_callback(try1))
        self.assertEqual(out["outcome"], "ATTEMPT_FAILED")
        self.assertEqual(self.hub.task_view(task_id)["status"], TaskStatus.OPEN.value)
        # 手动重试后成功
        try2 = self.hub.retry_task(task_id, actor="分镜师", reason="失败重试")
        self.plan_ok(try2)
        out = self.hub.ingest_callback(self.provider.make_callback(try2))
        self.assertEqual(out["outcome"], "ACCEPTED")
        self.assertEqual(self.hub.task_view(task_id)["status"], TaskStatus.RESOLVED.value)

    def test_duplicate_callback_is_deduped(self):
        res = self.submit()
        self.plan_ok(res["attempt_id"])
        envelope = self.provider.make_callback(res["attempt_id"])
        first = self.hub.ingest_callback(envelope)
        second = self.hub.ingest_callback(dict(envelope))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        candidates = self.store.all("SELECT * FROM candidates")
        self.assertEqual(len(candidates), 1)

    def test_repeated_success_for_same_attempt_is_ignored(self):
        """不同 event_id 的重复成功回调（供应商重发）不产生第二份结果。"""
        res = self.submit()
        self.plan_ok(res["attempt_id"])
        self.hub.ingest_callback(self.provider.make_callback(res["attempt_id"]))
        replay = self.provider.make_callback(res["attempt_id"])
        replay["event_id"] = "evt-replay-1"  # 供应商换号重发
        out = self.hub.ingest_callback(replay)
        self.assertEqual(out["outcome"], f"IGNORED_{AttemptStatus.SUCCEEDED.value}")
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 1)

    def test_late_success_from_timed_out_attempt_cannot_displace_winner(self):
        """超时尝试迟到的成功不得顶掉已确认的有效结果。"""
        res = self.submit()
        task_id, try1 = res["task_id"], res["attempt_id"]
        self.clock.advance(120)
        self.hub.sweep_timeouts()
        try2 = f"{task_id}-a2"
        self.plan_ok(try2)
        self.hub.ingest_callback(self.provider.make_callback(try2))
        # try-1 其实也出图了（供应商迟到），其成功回调不得制造第二份有效结果
        self.provider.plan(try1, PlannedResult(status="succeeded", file_bytes=b"late-bytes"))
        late = self.hub.ingest_callback(self.provider.make_callback(try1))
        self.assertEqual(late["outcome"], f"IGNORED_{AttemptStatus.TIMED_OUT.value}")
        view = self.hub.task_view(task_id)
        self.assertEqual(view["winning_attempt_id"], try2)
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 1)

    def test_success_for_unknown_attempt_is_recorded_not_applied(self):
        out = self.hub.ingest_callback(
            {
                "event_id": "evt-ghost",
                "attempt_id": "task-nonexistent-a1",
                "status": "succeeded",
                "occurred_at": "2026-09-10T05:00:00Z",
                "asset_sha256": "a" * 64,
                "format": "png",
            }
        )
        self.assertEqual(out["outcome"], "UNKNOWN_ATTEMPT")
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 0)

    def test_retry_requires_terminal_attempt(self):
        res = self.submit()
        with self.assertRaises(Exception):
            self.hub.retry_task(res["task_id"], actor="分镜师", reason="在途不可重试")
