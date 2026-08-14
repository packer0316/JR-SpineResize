"""
骨架檔讀取（唯讀）

**本工具永遠不會修改 .skel / .json。** 等比縮貼圖時骨架資料必須保持原樣，
理由見 :mod:`core.rect_mapper` 的說明。這裡讀取骨架只有兩個目的：

1. 取得 Spine 版本與畫布尺寸，顯示給使用者確認；
2. 交叉比對 atlas 區塊是否真的有被骨架用到。

支援 binary（.skel）與 JSON 兩種格式。binary 的標頭在 3.8 與 4.1+ 之間換過
一次（hash 從字串改成 8 位元組整數），這裡兩種都試，取能解出合法版號的那個。
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from core.exceptions import SkeletonParseError

_VERSION_RE = re.compile(r"^\d+\.\d+")

# 會參照 atlas 區塊的 attachment 型別（其餘型別純粹是幾何資料）
_ATLAS_ATTACHMENT_TYPES = frozenset({"region", "mesh", "linkedmesh", "skinnedmesh", ""})


@dataclass
class SkeletonInfo:
    path: Path
    fmt: str = "binary"            # binary | json
    version: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    nonessential: bool = False
    images_path: str = ""
    # 骨架實際用到的 atlas 區塊名稱；None 代表這個格式無法可靠取得
    region_names: set[str] | None = None
    # binary 3.8 的字串池，只能當作「有沒有出現過」的弱參考
    string_pool: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def major(self) -> int:
        try:
            return int(self.version.split(".")[0])
        except (ValueError, IndexError):
            return 0

    @property
    def display_size(self) -> str:
        return f"{self.width:g} x {self.height:g}"


class _BinaryReader:
    """Spine binary 格式讀取器（big-endian）"""

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.pos = offset

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise SkeletonParseError("檔案結尾提前出現")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def boolean(self) -> bool:
        return self.byte() != 0

    def float(self) -> float:
        if self.pos + 4 > len(self.data):
            raise SkeletonParseError("檔案結尾提前出現")
        value = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return value

    def var_int(self) -> int:
        """Spine 的 optimizePositive 變長整數"""
        result = 0
        for shift in (0, 7, 14, 21, 28):
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                break
        return result

    def string(self) -> str | None:
        count = self.var_int()
        if count == 0:
            return None
        if count == 1:
            return ""
        count -= 1
        if self.pos + count > len(self.data):
            raise SkeletonParseError("字串長度超出檔案範圍")
        raw = self.data[self.pos : self.pos + count]
        self.pos += count
        return raw.decode("utf-8", errors="replace")


def _try_header(data: bytes, hash_is_long: bool) -> tuple[str, _BinaryReader] | None:
    """嘗試以指定的 hash 形式讀取標頭，成功則回傳版號與接續的讀取器。"""
    try:
        reader = _BinaryReader(data, offset=8 if hash_is_long else 0)
        if not hash_is_long:
            reader.string()  # hash
        version = reader.string() or ""
    except SkeletonParseError:
        return None
    if not _VERSION_RE.match(version):
        return None
    return version, reader


def read_binary_skeleton(path: Path) -> SkeletonInfo:
    data = path.read_bytes()
    if len(data) < 16:
        raise SkeletonParseError(f"{path.name}：檔案過小，不像是 Spine 骨架檔")

    # 3.8 / 4.0 用字串 hash；4.1+ 改成 8 位元組整數
    header = _try_header(data, hash_is_long=False) or _try_header(data, hash_is_long=True)
    if header is None:
        raise SkeletonParseError(f"{path.name}：無法辨識為 Spine binary 骨架檔")

    version, reader = header
    info = SkeletonInfo(path=path, fmt="binary", version=version)

    try:
        info.x = reader.float()
        info.y = reader.float()
        info.width = reader.float()
        info.height = reader.float()
        if version.startswith("4.2") or version.startswith("4.3"):
            reader.float()  # referenceScale
        info.nonessential = reader.boolean()
        if info.nonessential:
            reader.float()  # fps
            info.images_path = reader.string() or ""
            reader.string()  # audioPath
    except SkeletonParseError:
        info.notes.append("標頭資訊不完整，僅能取得版本號")
        return info

    if version.startswith("3."):
        # 3.8 在標頭之後有一個字串池，可用來判斷 atlas 區塊有沒有被用到
        try:
            count = reader.var_int()
            if 0 <= count <= 100000:
                for _ in range(count):
                    value = reader.string()
                    if value:
                        info.string_pool.add(value)
        except SkeletonParseError:
            info.string_pool.clear()

    info.notes.append("binary 骨架只讀取標頭，內容不會被修改")
    return info


def read_json_skeleton(path: Path) -> SkeletonInfo:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SkeletonParseError(f"{path.name}：JSON 無法解析（{exc}）") from exc
    if not isinstance(raw, dict):
        raise SkeletonParseError(f"{path.name}：JSON 根節點不是物件")

    header = raw.get("skeleton") or {}
    info = SkeletonInfo(
        path=path,
        fmt="json",
        version=str(header.get("spine", "")),
        x=float(header.get("x", 0) or 0),
        y=float(header.get("y", 0) or 0),
        width=float(header.get("width", 0) or 0),
        height=float(header.get("height", 0) or 0),
        images_path=str(header.get("images", "") or ""),
    )
    info.region_names = _collect_json_regions(raw)
    return info


def _collect_json_regions(raw: dict) -> set[str]:
    """
    走訪所有 skin 收集會用到 atlas 的 attachment 名稱。

    區塊名稱優先取 ``path``；沒有 ``path`` 時才用 attachment 的鍵名。
    """
    names: set[str] = set()
    skins = raw.get("skins")

    # 3.x 是 {skinName: {...}}，4.x 是 [{name:..., attachments:{...}}]
    if isinstance(skins, dict):
        skin_iter = skins.values()
    elif isinstance(skins, list):
        skin_iter = [s.get("attachments", {}) for s in skins if isinstance(s, dict)]
    else:
        skin_iter = []

    for slots in skin_iter:
        if not isinstance(slots, dict):
            continue
        for attachments in slots.values():
            if not isinstance(attachments, dict):
                continue
            for key, attachment in attachments.items():
                if not isinstance(attachment, dict):
                    continue
                if str(attachment.get("type", "region")).lower() not in _ATLAS_ATTACHMENT_TYPES:
                    continue
                names.add(str(attachment.get("path") or key))
    return names


def read_skeleton(path: Path) -> SkeletonInfo:
    """依副檔名選擇讀取方式。"""
    if path.suffix.lower() == ".json":
        return read_json_skeleton(path)
    return read_binary_skeleton(path)
