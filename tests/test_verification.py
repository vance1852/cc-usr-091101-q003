"""摘要与声明格式核验：只有吻合的文件才能进入候选区。"""

from hub_factory import HubCase, PlannedResult, asset_bytes, manifest_entry

from asset_hub import AssetState, AttemptStatus, TaskStatus


class VerificationTest(HubCase):
    def test_matching_file_enters_candidates(self):
        task_id, asset_id = self.run_success(fixture="ok.png")
        asset = self.store.one("SELECT * FROM candidates WHERE asset_id = ?", (asset_id,))
        self.assertEqual(asset["state"], AssetState.CANDIDATE.value)
        self.assertEqual(asset["media_type"], "png")
        self.assertEqual(asset["valid_for_task"], 1)
        self.assertEqual(self.hub.task_view(task_id)["status"], TaskStatus.RESOLVED.value)

    def test_digest_mismatch_goes_to_quarantine(self):
        res = self.submit()
        entry = manifest_entry("corrupt.png")  # 声明摘要故意写错
        self.provider.plan(
            res["attempt_id"],
            PlannedResult(
                status="succeeded",
                file_bytes=asset_bytes("corrupt.png"),
                declared_sha256=entry["declared_sha256"],
                declared_format="png",
            ),
        )
        out = self.hub.ingest_callback(self.provider.make_callback(res["attempt_id"]))
        self.assertEqual(out["outcome"], "QUARANTINED")
        quarantine = self.store.all("SELECT * FROM quarantine")
        self.assertEqual(len(quarantine), 1)
        self.assertIn("摘要", quarantine[0]["reason"])
        # 候选区为空，任务仍 OPEN 可重试
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 0)
        self.assertEqual(self.hub.task_view(res["task_id"])["status"], TaskStatus.OPEN.value)
        attempt = self.store.one("SELECT * FROM attempts WHERE attempt_id = ?", (res["attempt_id"],))
        self.assertEqual(attempt["status"], AttemptStatus.FAILED_VERIFICATION.value)

    def test_format_mismatch_goes_to_quarantine(self):
        res = self.submit()
        entry = manifest_entry("mislabeled.png")  # JPEG 字节声明成 png
        self.provider.plan(
            res["attempt_id"],
            PlannedResult(
                status="succeeded",
                file_bytes=asset_bytes("mislabeled.png"),
                declared_sha256=entry["declared_sha256"],
                declared_format=entry["declared_format"],
            ),
        )
        out = self.hub.ingest_callback(self.provider.make_callback(res["attempt_id"]))
        self.assertEqual(out["outcome"], "QUARANTINED")
        quarantine = self.store.one("SELECT * FROM quarantine")
        self.assertEqual(quarantine["actual_format"], "jpeg")
        self.assertEqual(quarantine["declared_format"], "png")

    def test_retry_after_quarantine_can_resolve_task(self):
        res = self.submit()
        entry = manifest_entry("corrupt.png")
        self.provider.plan(
            res["attempt_id"],
            PlannedResult(
                status="succeeded",
                file_bytes=asset_bytes("corrupt.png"),
                declared_sha256=entry["declared_sha256"],
                declared_format="png",
            ),
        )
        self.hub.ingest_callback(self.provider.make_callback(res["attempt_id"]))
        try2 = self.hub.retry_task(res["task_id"], actor="分镜师", reason="供应商文件损坏，重跑")
        self.plan_ok(try2)
        out = self.hub.ingest_callback(self.provider.make_callback(try2))
        self.assertEqual(out["outcome"], "ACCEPTED")
        self.assertEqual(self.hub.task_view(res["task_id"])["winning_attempt_id"], try2)
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 1)

    def test_missing_declared_format_is_rejected(self):
        res = self.submit()
        ok = manifest_entry("ok.png")
        self.provider.plan(
            res["attempt_id"],
            PlannedResult(
                status="succeeded",
                file_bytes=asset_bytes("ok.png"),
                declared_sha256=ok["declared_sha256"],
                declared_format="png",
            ),
        )
        envelope = self.provider.make_callback(res["attempt_id"])
        del envelope["format"]  # 供应商漏报格式
        out = self.hub.ingest_callback(envelope)
        self.assertEqual(out["outcome"], "QUARANTINED")
        self.assertEqual(len(self.store.all("SELECT * FROM candidates")), 0)
