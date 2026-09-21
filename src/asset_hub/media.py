"""媒体摘要、文件签名嗅探与确定性样例生成。

真实媒体文件不进入仓库；演示与测试使用 :func:`synthetic_png` 生成的
确定性极小 PNG，其摘要可以被 fixtures 预先声明。
"""

from __future__ import annotations

import binascii
import hashlib
import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# 受支持的候选格式 -> 签名嗅探函数
SIGNATURES = {
    "image/png": lambda data: data.startswith(PNG_MAGIC),
    "image/jpeg": lambda data: data.startswith(JPEG_MAGIC),
    "video/mp4": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_media_type(data: bytes) -> str | None:
    for media_type, check in SIGNATURES.items():
        if check(data):
            return media_type
    return None


def judge_declaration(
    declared_media_type: str | None,
    declared_sha256: str | None,
    data: bytes,
) -> str | None:
    """返回不合规原因；None 表示字节与声明完全吻合。"""
    if not isinstance(declared_sha256, str) or not (
        len(declared_sha256) == 64
        and all(c in "0123456789abcdef" for c in declared_sha256)
    ):
        return "MALFORMED_DIGEST"
    if declared_media_type not in SIGNATURES:
        return "FORMAT_UNDECLARED"
    if sniff_media_type(data) != declared_media_type:
        return "FORMAT_MISMATCH"
    if sha256_hex(data) != declared_sha256:
        return "DIGEST_MISMATCH"
    return None


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", binascii.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def synthetic_png(width: int = 8, height: int = 16, rgb: tuple[int, int, int] = (33, 64, 120)) -> bytes:
    """生成确定性纯色 RGB PNG（无压缩参数随机性，同参数永远同字节、同摘要）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(scanline * height, 9)
    return PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
