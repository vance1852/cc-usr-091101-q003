"""进程中断后的恢复：不重复计费、不丢失回调。"""

import tempfile
import unittest
from pathlib import Path

from hub_factory import FakeClock, HubCase, PlannedResult, load_shots

from asset_hub import AssetHub, FakeProvider, Store


class CrashyProvider(FakeProvider):
    """第一次提交在供应商受理并计费后、响应返回前模拟进程崩溃。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.crashed = False

    def submit(self, request):
        job = super().submit(request)  # 供应商已受理、已计费
        if not self.crashed:
            self.crashed = True
            raise ConnectionError("进程崩溃：响应丢失")
        return job


class RecoveryTest(HubCase):
    def _restart(self, db_path: Path, provider, clock) -> AssetHub:
        """模拟进程重启：同一数据库文件、同一（远端）供应商、新中枢实例。"""
        store = Store(db_path)
        self.addCleanup(store.close)
        return AssetHub(store, {"fake": provider}, clock=clock, default_timeout_seconds=60)

    def test_crash_between_submit_and_response_no_double_billing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "hub.db"
            clock = FakeClock()
            provider = CrashyProvider(clock=clock)
            hub = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
            self.addCleanup(hub.store.close)
            hub.import_shots(load_shots())
            hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
            hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
            # 提交瞬间崩溃：供应商已计费，中枢没来得及记录任务句柄
            with self.assertRaises(ConnectionError):
                hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
            self.assertEqual(len(provider.charges), 1)

            hub2 = self._restart(db, provider, clock)
            report = hub2.recover()
            # 按幂等键重提：供应商去重，不重复计费
            self.assertEqual(len(report["resubmitted"]), 1)
            self.assertEqual(len(provider.charges), 1)
            self.assertEqual(provider.submit_calls, 2)
            attempt = hub2.store.one("SELECT * FROM attempts")
            self.assertEqual(attempt["status"], "SUBMITTED")
            self.assertEqual(attempt["billed"], 1)

    def test_crash_before_callback_recovers_via_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "hub.db"
            clock = FakeClock()
            provider = FakeProvider(clock=clock)
            hub = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
            self.addCleanup(hub.store.close)
            hub.import_shots(load_shots())
            hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
            hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
            res = hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
            provider.plan(res["attempt_id"], PlannedResult(status="succeeded", file_bytes=b"\x89PNG\r\n\x1a\n" + b"img"))
            # 回调尚未到达，进程崩溃
            hub2 = self._restart(db, provider, clock)
            report = hub2.recover()
            self.assertEqual(report["reconciled"], [res["attempt_id"]])
            self.assertEqual(len(provider.charges), 1)
            # 结果正常进入候选区，任务闭环
            self.assertEqual(len(hub2.store.all("SELECT * FROM candidates")), 1)
            self.assertEqual(hub2.task_view(res["task_id"])["status"], "RESOLVED")
            # 随后真实回调到达：同 event_id 判重，不重复处理
            dup = hub2.ingest_callback(provider.make_callback(res["attempt_id"]))
            self.assertTrue(dup["duplicate"])
            self.assertEqual(len(hub2.store.all("SELECT * FROM candidates")), 1)

    def test_unprocessed_callback_survives_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "hub.db"
            clock = FakeClock()
            provider = FakeProvider(clock=clock)
            hub = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
            self.addCleanup(hub.store.close)
            hub.import_shots(load_shots())
            hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
            hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
            res = hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
            provider.plan(res["attempt_id"], PlannedResult(status="succeeded", file_bytes=b"\x89PNG\r\n\x1a\n" + b"img"))
            # 回调已接收落库，但还没来得及处理就崩溃
            receipt = hub.receive_callback(provider.make_callback(res["attempt_id"]))
            self.assertFalse(receipt["duplicate"])

            hub2 = self._restart(db, provider, clock)
            report = hub2.recover()
            self.assertEqual(report["events_processed"], 1)
            self.assertEqual(len(hub2.store.all("SELECT * FROM candidates")), 1)
            # 再恢复一次：没有遗留事件，也不重复产生结果
            report2 = hub2.recover()
            self.assertEqual(report2["events_processed"], 0)
            self.assertEqual(len(hub2.store.all("SELECT * FROM candidates")), 1)
            self.assertEqual(len(provider.charges), 1)

    def test_recover_times_out_silent_attempt_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "hub.db"
            clock = FakeClock()
            provider = FakeProvider(clock=clock)
            hub = AssetHub(Store(db), {"fake": provider}, clock=clock, default_timeout_seconds=60)
            self.addCleanup(hub.store.close)
            hub.import_shots(load_shots())
            hub.create_prompt_pack("p", {"prompt": "x"}, actor="导演")
            hub.register_reference("r", "character:林雪", "a" * 64, "image/png", actor="美术")
            res = hub.submit_generation("SH-01", "p", 1, [("r", 1)], "fake")
            # 供应商静默（无剧本），时钟越过截止时间后崩溃重启
            clock.advance(120)
            hub2 = self._restart(db, provider, clock)
            report = hub2.recover()
            self.assertEqual(report["timed_out"], [res["attempt_id"]])
            # 自动重试在同一任务号下开出第二次尝试，各计一次费
            attempts = hub2.store.all("SELECT * FROM attempts ORDER BY seq")
            self.assertEqual([a["status"] for a in attempts], ["TIMED_OUT", "SUBMITTED"])
            self.assertEqual(len(provider.charges), 2)
            self.assertEqual(len({c for c in provider.charges}), 2)


if __name__ == "__main__":
    unittest.main()
