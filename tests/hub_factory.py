"""测试共享工厂：确定性时钟、剧本化供应商、样例加载。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import AssetHub, FakeProvider, PlannedResult, Store  # noqa: E402

FIXTURES = Path(__file__).parents[1] / "fixtures"
START = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """单调递增时钟：每次调用前进 1 微秒，保证事件顺序可比较。"""

    def __init__(self, start: datetime = START):
        self.t = start

    def __call__(self) -> datetime:
        self.t += timedelta(microseconds=1)
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def load_shots() -> list[dict]:
    return json.loads((FIXTURES / "shots.json").read_text(encoding="utf-8"))


def manifest_entry(name: str) -> dict:
    manifest = json.loads((FIXTURES / "assets" / "manifest.json").read_text(encoding="utf-8"))
    return next(e for e in manifest["files"] if e["file"] == name)


def asset_bytes(name: str) -> bytes:
    return (FIXTURES / "assets" / name).read_bytes()


class HubCase(unittest.TestCase):
    """每个用例一套干净环境，并预置镜头清单与版本化输入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.provider = FakeProvider(clock=self.clock)
        self.store = Store(":memory:")
        self.hub = AssetHub(
            self.store,
            {"fake": self.provider},
            clock=self.clock,
            default_timeout_seconds=60,
        )
        self.hub.import_shots(load_shots())
        self.hub.create_prompt_pack(
            "costume-linxue", {"prompt": "红色风衣, 雨夜霓虹", "negative": "模糊"}, actor="导演"
        )
        self.hub.register_reference(
            "char-linxue", "character:林雪", "a" * 64, "image/png", actor="美术"
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    # -- 常用流程 ----------------------------------------------------------
    def submit(self, shot: str = "SH-01", pack_version: int = 1, ref_version: int = 1) -> dict:
        return self.hub.submit_generation(
            shot,
            "costume-linxue",
            pack_version,
            [("char-linxue", ref_version)],
            "fake",
            actor="分镜师",
        )

    def plan_ok(self, attempt_id: str, fixture: str = "ok.png", salt: bool = True) -> str:
        """为尝试安排成功结局，返回将产生的 asset_id。

        默认按 attempt_id 加盐：不同任务产出不同字节（贴近真实），
        内容寻址的候选资产因此各自独立。
        """
        entry = manifest_entry(fixture)
        data = asset_bytes(fixture)
        if salt:
            data = data + attempt_id.encode("utf-8")
            declared_sha = hashlib.sha256(data).hexdigest()
        else:
            declared_sha = entry["declared_sha256"]
        self.provider.plan(
            attempt_id,
            PlannedResult(
                status="succeeded",
                file_bytes=data,
                declared_sha256=declared_sha,
                declared_format=entry["declared_format"],
            ),
        )
        return "asset-" + declared_sha[:16]

    def run_success(self, shot: str = "SH-01", fixture: str = "ok.png", **submit_kw) -> tuple[str, str]:
        """提交 → 成功回调 → 返回 (task_id, asset_id)。"""
        res = self.submit(shot, **submit_kw)
        asset_id = self.plan_ok(res["attempt_id"], fixture)
        out = self.hub.ingest_callback(self.provider.make_callback(res["attempt_id"]))
        assert out["outcome"] == "ACCEPTED", out
        return res["task_id"], asset_id

    def approve_and_adopt(self, shot: str, asset_id: str, reason: str = "通过") -> dict:
        self.hub.annotate(asset_id, actor="总监", action="APPROVE", reason=reason)
        return self.hub.adopt(shot, asset_id, actor="总监", reason=reason)
