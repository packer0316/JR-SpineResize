"""
.atlas 解析器

同時支援兩種官方格式：

* legacy（Spine <= 4.0）：區塊屬性為 ``rotate / xy / size / orig / offset``，且有縮排
* modern（Spine >= 4.1）：區塊屬性為 ``bounds / offsets / rotate(角度)``，沒有縮排

判斷「這一行是頁面名稱、區塊名稱、還是屬性」不能靠副檔名——區塊名稱本身
就可能長得像檔名（實際素材裡出現過 ``tmp/crystal.png0001``）。這裡改用
「有沒有冒號 + 鍵名是否為已知屬性」來判斷，並用空行切分頁面。
"""
from __future__ import annotations

import re
from pathlib import Path

from config.constants import (
    ATLAS_STYLE_LEGACY,
    ATLAS_STYLE_MODERN,
    PAGE_KEYS,
    REGION_KEYS,
)
from core.exceptions import AtlasParseError
from models.atlas_data import AtlasFile, AtlasPage, AtlasProp, AtlasRegion

_PROP_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<sep>[ \t]*)(?P<raw>.*)$")
_KNOWN_KEYS = PAGE_KEYS | REGION_KEYS
_MODERN_MARKERS = frozenset({"bounds", "offsets"})


def _parse_prop(match: re.Match[str]) -> AtlasProp:
    raw = match.group("raw").rstrip()
    parts = raw.split(",")
    if len(parts) > 1:
        # 記下實際使用的分隔符（"," 或 ", "），輸出時原樣還原
        after_comma = parts[1]
        spaces = len(after_comma) - len(after_comma.lstrip(" "))
        delim = "," + " " * spaces
    else:
        delim = ", "
    return AtlasProp(
        key=match.group("key"),
        values=[p.strip() for p in parts],
        indent=match.group("indent"),
        sep=match.group("sep"),
        delim=delim,
    )


def parse_atlas_text(text: str) -> AtlasFile:
    """把 .atlas 內容解析成 AtlasFile。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()

    atlas = AtlasFile(newline=newline, trailing_newline=trailing_newline)
    page: AtlasPage | None = None
    region: AtlasRegion | None = None
    in_page_header = False
    expect_page = True
    saw_modern_marker = False
    pending_blank = False

    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            expect_page = True
            pending_blank = True
            continue

        if expect_page:
            page = AtlasPage(name=line.strip(), leading_blank=pending_blank)
            atlas.pages.append(page)
            region = None
            in_page_header = True
            expect_page = False
            pending_blank = False
            continue

        if page is None:  # pragma: no cover - 由 expect_page 保證不會發生
            raise AtlasParseError(f"第 {lineno} 行出現在任何頁面之前：{line!r}")

        match = _PROP_RE.match(line)
        is_prop = match is not None and match.group("key") in _KNOWN_KEYS

        if is_prop:
            assert match is not None
            prop = _parse_prop(match)
            if prop.key in _MODERN_MARKERS:
                saw_modern_marker = True

            if in_page_header and not prop.indent and prop.key in PAGE_KEYS:
                page.props.append(prop)
            elif region is not None:
                region.props.append(prop)
            else:
                # 頁面標頭還沒結束，卻出現非頁面鍵（例如缺頁面屬性的極簡 atlas）
                page.props.append(prop)
            continue

        # 不是屬性 → 這一行是區塊名稱
        region = AtlasRegion(name=line.strip())
        page.regions.append(region)
        in_page_header = False

    if not atlas.pages:
        raise AtlasParseError("atlas 內容為空，找不到任何頁面")

    atlas.style = ATLAS_STYLE_MODERN if saw_modern_marker else ATLAS_STYLE_LEGACY
    for page in atlas.pages:
        for region in page.regions:
            region.style = atlas.style

    return atlas


def parse_atlas_file(path: Path) -> AtlasFile:
    """讀取並解析 .atlas 檔案。"""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AtlasParseError(f"無法讀取 {path.name}：{exc}") from exc

    try:
        return parse_atlas_text(text)
    except AtlasParseError as exc:
        raise AtlasParseError(f"{path.name}：{exc}") from exc


def write_atlas_file(atlas: AtlasFile, path: Path) -> None:
    """輸出 .atlas（維持原本的換行符）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(atlas.to_text(), encoding="utf-8", newline="")
