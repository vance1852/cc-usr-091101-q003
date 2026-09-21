#!/usr/bin/env python3
"""生成 fixtures/assets/ 下的资产摘要示例（可重复执行，结果确定）。

- ok.png         真实 PNG，声明摘要与格式均吻合 → 应进入候选区
- corrupt.png    真实 PNG，但清单声明了错误的摘要 → 应进隔离区（摘要不符）
- mislabeled.png 实为 JPEG 字节，清单声明为 png → 应进隔离区（格式不符）
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).parents[1] / "fixtures" / "assets"


def png_1x1(rgb: tuple[int, int, int]) -> bytes:
    """手工构造 1x1 RGB PNG（仅标准库）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + bytes(rgb))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# 仅供格式嗅探的最小 JPEG 字节流（魔数 + JFIF 头 + 结束标记）
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + bytes(64) + b"\xff\xd9"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    files = {
        "ok.png": png_1x1((0x2E, 0x7D, 0xD1)),
        "corrupt.png": png_1x1((0xC0, 0x39, 0x2B)),
        "mislabeled.png": JPEG_BYTES,
    }
    for name, data in files.items():
        (ASSETS / name).write_bytes(data)

    manifest = {
        "declared_by": "fixture-vendor",
        "note": "声明格式与摘要即供应商回调中应携带的声明；expect 为中枢应有的处置",
        "files": [
            {
                "file": "ok.png",
                "declared_sha256": sha256(files["ok.png"]),
                "declared_format": "png",
                "width": 1,
                "height": 1,
                "expect": "candidate",
                "note": "摘要与声明格式均吻合",
            },
            {
                "file": "corrupt.png",
                "declared_sha256": "c" * 64,
                "declared_format": "png",
                "width": 1,
                "height": 1,
                "expect": "quarantine_digest",
                "note": "声明摘要与实际文件摘要不符",
            },
            {
                "file": "mislabeled.png",
                "declared_sha256": sha256(files["mislabeled.png"]),
                "declared_format": "png",
                "width": 1,
                "height": 1,
                "expect": "quarantine_format",
                "note": "实际为 JPEG，声明却是 png",
            },
        ],
    }
    (ASSETS / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for entry in manifest["files"]:
        print(f"{entry['file']}: expect={entry['expect']} sha256={entry['declared_sha256'][:16]}…")


if __name__ == "__main__":
    main()
