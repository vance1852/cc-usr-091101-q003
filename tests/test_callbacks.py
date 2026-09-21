"""回调幂等、乱序终态与文件校验隔离。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import AttemptStatus
from asset_hub.media import sha256_hex, synthetic_png

from _base import HubCase


def envelope(event_id, attempt_id, status, at, sha=None, **attrs):
    row = {"event_id": event_id, "attempt_id": attempt_id,
           "status": status, "occurred_at": at}
    if sha is not None:
        row["asset_sha256"] = sha
    row.update(attrs)
    return row


class CallbackStateMachineTest(HubCase):
    def test_duplicate_callback_has_no_effect_but_is_auditable(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png()
        digest = sha256_hex(png)
        cb = envelope("ev-1", attempt_id, "succeeded", "2026-09-10T05:00:58Z", digest, width=1080)
        r1 = self.hub.receive_callback(cb)
        r2 = self.hub.receive_callback(dict(cb))
        self.assertTrue(r1["applied"])
        self.assertFalse(r2["applied"])
        self.assertEqual(r2["note"], "duplicate_event")
        deliveries = self.store.query_all(
            "SELECT dedup FROM callback_deliveries WHERE event_id=:e", e="ev-1"
        )
        self.assertEqual([d["dedup"] for d in deliveries],
                         ["applied:success_claimed", "duplicate_event"])

    def test_failure_arrives_first_then_success_is_retained_but_ignored(self):
        """失败通知先到即终态；后到的成功（fixtures 的乱序场景）不得翻案。"""
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png()
        digest = sha256_hex(png)
        r_fail = self.hub.receive_callback(
            envelope("cb-301", attempt_id, "failed", "2026-09-10T05:01:00Z", error_code="TIMEOUT")
        )
        self.assertEqual(r_fail["attempt_status"], AttemptStatus.FAILED.value)
        r_ok = self.hub.receive_callback(
            envelope("cb-302", attempt_id, "succeeded", "2026-09-10T05:00:58Z", digest)
        )
        self.assertFalse(r_ok["applied"])
        self.assertTrue(r_ok["note"].startswith("late_succeeded"))
        # 成功文件即使到达也只能隔离，不能制造有效结果
        d = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png"
        )
        self.assertFalse(d["accepted"])
        self.assertEqual(d["quarantine_reason"], "LATE_AFTER_TERMINAL")
        self.assertEqual(
            self.store.query_one("SELECT status FROM attempts WHERE attempt_id=:a",
                                 a=attempt_id)["status"],
            AttemptStatus.FAILED.value,
        )
        # 显式重开下一代尝试才能继续
        new_id = self.hub.retry_terminal(attempt_id)
        self.assertNotEqual(new_id, attempt_id)

    def test_success_then_bytes_settle_and_duplicate_bytes_no_new_candidate(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(4, 4, (1, 2, 3))
        digest = sha256_hex(png)
        r = self.hub.receive_callback(
            envelope("ev-9", attempt_id, "succeeded", "2026-09-10T06:00:00Z", digest)
        )
        self.assertEqual(r["note"], "awaiting_validated_bytes")
        d1 = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png"
        )
        self.assertTrue(d1["accepted"])
        self.assertTrue(d1["settled"])
        d2 = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png"
        )
        self.assertTrue(d2["accepted"])  # 同一字节重复投递不报错
        cands = self.store.query_all(
            "SELECT * FROM candidates WHERE attempt_id=:a", a=attempt_id
        )
        self.assertEqual(len(cands), 1)
        self.assertTrue(self.store.object_path(digest).exists())
        self.assertFalse(self.store.quarantine_path(digest).exists())

    def test_bytes_arrive_before_callback(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(2, 2)
        digest = sha256_hex(png)
        d = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png"
        )
        self.assertTrue(d["accepted"])
        self.assertFalse(d.get("settled"))
        r = self.hub.receive_callback(
            envelope("ev-10", attempt_id, "succeeded", "2026-09-10T06:01:00Z", digest)
        )
        self.assertEqual(r["note"], "settled_success")

    def test_digest_mismatch_is_quarantined_and_attempt_dead_letters(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(5, 5)
        claimed = "b" * 64
        self.hub.receive_callback(
            envelope("ev-11", attempt_id, "succeeded", "2026-09-10T06:02:00Z", claimed)
        )
        d = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=claimed, declared_media_type="image/png"
        )
        self.assertFalse(d["accepted"])
        self.assertEqual(d["quarantine_reason"], "DIGEST_MISMATCH")
        actual = sha256_hex(png)
        self.assertTrue(self.store.quarantine_path(actual).exists())
        self.assertFalse(self.store.object_path(actual).exists())
        self.assertEqual(
            self.store.query_one("SELECT status FROM attempts WHERE attempt_id=:a",
                                 a=attempt_id)["status"],
            AttemptStatus.DEAD_LETTER.value,
        )

    def test_format_mismatch_is_quarantined(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(3, 3)
        digest = sha256_hex(png)
        self.hub.receive_callback(
            envelope("ev-12", attempt_id, "succeeded", "2026-09-10T06:03:00Z", digest)
        )
        d = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="video/mp4"
        )
        self.assertEqual(d["quarantine_reason"], "FORMAT_MISMATCH")
        self.assertFalse(d["accepted"])

    def test_unexpected_media_type_vs_attempt_contract(self):
        self.seed_shot()
        prep = self.hub.prepare_generation("shot-1", "echo-image", {"steps": 20}, "video/mp4")
        png = synthetic_png(3, 3)
        digest = sha256_hex(png)
        d = self.hub.deliver_result_bytes(
            prep["attempt_id"], png, declared_sha256=digest, declared_media_type="image/png"
        )
        self.assertEqual(d["quarantine_reason"], "FORMAT_UNEXPECTED")

    def test_late_failure_after_success_claim_does_not_flip_result(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(4, 8, (8, 8, 8))
        digest = sha256_hex(png)
        self.hub.receive_callback(
            envelope("ev-30", attempt_id, "succeeded", "2026-09-10T09:00:00Z", digest))
        r = self.hub.receive_callback(
            envelope("ev-31", attempt_id, "failed", "2026-09-10T09:00:05Z", error_code="WORKER_DIED"))
        self.assertFalse(r["applied"])
        self.assertEqual(r["note"], "late_failure_after_success_claim")
        d = self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png")
        self.assertTrue(d["settled"])
        self.assertEqual(
            self.store.query_one("SELECT status FROM attempts WHERE attempt_id=:a",
                                 a=attempt_id)["status"],
            AttemptStatus.SUCCEEDED.value)

    def test_failure_after_validated_bytes_is_retained_not_applied(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(4, 8, (9, 9, 9))
        digest = sha256_hex(png)
        self.hub.deliver_result_bytes(
            attempt_id, png, declared_sha256=digest, declared_media_type="image/png")
        r = self.hub.receive_callback(
            envelope("ev-32", attempt_id, "failed", "2026-09-10T09:01:00Z", error_code="TIMEOUT"))
        self.assertEqual(r["note"], "late_failure_after_success_claim")
        self.assertNotEqual(r["attempt_status"], AttemptStatus.FAILED.value)

    def test_conflicting_success_claims_do_not_create_two_valid_results(self):
        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png_a = synthetic_png(4, 8, (1, 1, 1))
        png_b = synthetic_png(4, 8, (2, 2, 2))
        digest_a, digest_b = sha256_hex(png_a), sha256_hex(png_b)
        self.hub.receive_callback(
            envelope("ev-40", attempt_id, "succeeded", "2026-09-10T09:02:00Z", digest_a))
        r = self.hub.receive_callback(
            envelope("ev-41", attempt_id, "succeeded", "2026-09-10T09:02:01Z", digest_b))
        self.assertFalse(r["applied"])
        self.assertEqual(r["note"], "conflicting_success_claim")
        # 声明 B 的字节本身合法，可进候选区，但不会让尝试终态化
        db = self.hub.deliver_result_bytes(
            attempt_id, png_b, declared_sha256=digest_b, declared_media_type="image/png")
        self.assertTrue(db["accepted"])
        self.assertFalse(db.get("settled"))
        # 首封认领 A 的字节到达才终态
        da = self.hub.deliver_result_bytes(
            attempt_id, png_a, declared_sha256=digest_a, declared_media_type="image/png")
        self.assertTrue(da["settled"])
        self.assertEqual(
            self.store.query_one("SELECT success_event_id, declared_sha FROM attempts WHERE attempt_id=:a",
                                 a=attempt_id)["success_event_id"], "ev-40")

    def test_inbox_replay_after_crash_is_idempotent(self):
        from asset_hub import Worker

        self.seed_shot()
        attempt_id, _ = self.prepare_dispatch_ok()
        png = synthetic_png(6, 6)
        digest = sha256_hex(png)
        self.inbox.enqueue({"kind": "callback", "envelope":
            envelope("ev-20", attempt_id, "succeeded", "2026-09-10T07:00:00Z", digest)})
        self.inbox.enqueue({"kind": "callback", "envelope":
            envelope("ev-20", attempt_id, "succeeded", "2026-09-10T07:00:00Z", digest)})
        self.inbox.enqueue({"kind": "bytes", "attempt_id": attempt_id,
                            "declared_sha256": digest, "declared_media_type": "image/png",
                            "data_b64": __import__("base64").b64encode(png).decode()})
        worker = Worker(self.hub, self.inbox)
        first = worker.run_once()
        self.assertEqual(len(list(self.inbox.pending())), 0)
        # 模拟“已处理文件被错误地重新放回 spool”：重放不产生副作用
        for f in list(self.inbox.processed.glob("*.json")):
            f.replace(self.inbox.spool / f.name)
        second = worker.run_once()
        notes = sorted(r.get("note", "") for r in second)
        self.assertIn("duplicate_event", notes)
        self.assertEqual(
            self.store.query_all(
                "SELECT COUNT(*) AS c FROM candidates WHERE attempt_id=:a", a=attempt_id
            )[0]["c"], 1,
        )
        self.assertEqual(
            self.store.query_all(
                "SELECT COUNT(*) AS c FROM callbacks WHERE event_id='ev-20'"
            )[0]["c"], 1,
        )
