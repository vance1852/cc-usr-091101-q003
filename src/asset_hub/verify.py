"""供应商文件的摘要与声明格式核验。

只有 ``sha256(文件字节) == 声明摘要`` 且 ``嗅探格式 == 声明格式``
的文件才允许进入候选区，其余进入隔离区。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ids import sha256_hex

#: 魔数嗅探表（按声明顺序匹配）
_MAGICS: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


def sniff_format(data: bytes) -> str | None:
    """按魔数识别媒体格式，无法识别返回 None。"""
    for magic, fmt in _MAGICS:
        if data.startswith(magic):
            return fmt
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "mp4"
    return None


@dataclass(frozen=True)
class Verification:
    ok: bool
    actual_sha256: str
    actual_format: str | None
    reason: str


def verify_payload(data: bytes, declared_sha256: str | None, declared_format: str | None) -> Verification:
    """核验文件字节与供应商声明是否吻合。"""
    actual_sha = sha256_hex(data)
    actual_fmt = sniff_format(data)
    if not declared_sha256:
        return Verification(False, actual_sha, actual_fmt, "回调未声明资产摘要")
    if actual_sha != declared_sha256:
        return Verification(False, actual_sha, actual_fmt, "文件摘要与声明摘要不符")
    if not declared_format:
        return Verification(False, actual_sha, actual_fmt, "回调未声明文件格式")
    if actual_fmt is None:
        return Verification(False, actual_sha, actual_fmt, "无法识别文件格式")
    if actual_fmt != declared_format:
        return Verification(False, actual_sha, actual_fmt, f"文件实际格式 {actual_fmt} 与声明格式 {declared_format} 不符")
    return Verification(True, actual_sha, actual_fmt, "摘要与声明格式吻合")
