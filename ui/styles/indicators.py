"""
控制項指示圖示：以 PIL 動態產生 CheckBox 打勾與 RadioButton 圓點

Qt 樣式表無法直接畫抗鋸齒的勾與圓點，改為先產生 PNG（超取樣後縮小）
再以 ``image: url(...)`` 引用。依顏色快取，主題切換自動產生對應版本。
做法與 JR-Img-Compresser 相同。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_CACHE_DIR = Path.home() / ".jr_spineresize" / "cache"
_VERSION = "v1"
_SCALE = 4
_SIZE = 16


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def checkmark_icon(color: str) -> str:
    """打勾圖示（透明底），回傳正斜線路徑供 url() 使用"""
    key = color.lstrip("#")
    out = _CACHE_DIR / f"check_{_VERSION}_{key}.png"
    if not out.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        s = _SIZE * _SCALE
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(color)
        width = int(s * 0.14)
        p1 = (s * 0.24, s * 0.52)
        p2 = (s * 0.43, s * 0.70)
        p3 = (s * 0.76, s * 0.30)
        draw.line([p1, p2], fill=rgb, width=width)
        draw.line([p2, p3], fill=rgb, width=width)
        radius = width // 2
        for x, y in (p1, p2, p3):
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=rgb)
        img.resize((_SIZE, _SIZE), Image.Resampling.LANCZOS).save(out)
    return out.as_posix()


def radio_dot_icon(color: str) -> str:
    """RadioButton 圓點圖示（透明底）"""
    key = color.lstrip("#")
    out = _CACHE_DIR / f"dot_{_VERSION}_{key}.png"
    if not out.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        s = _SIZE * _SCALE
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        margin = s * 0.30
        draw.ellipse([margin, margin, s - margin, s - margin], fill=_hex_to_rgb(color))
        img.resize((_SIZE, _SIZE), Image.Resampling.LANCZOS).save(out)
    return out.as_posix()


def play_icon(color: str) -> str:
    """播放三角形圖示（透明底）"""
    key = color.lstrip("#")
    out = _CACHE_DIR / f"play_{_VERSION}_{key}.png"
    if not out.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        s = _SIZE * _SCALE
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(color)
        # 三角形視覺重心偏左，稍微右移置中
        left, right = s * 0.32, s * 0.78
        top, bottom = s * 0.22, s * 0.78
        draw.polygon([(left, top), (right, s / 2), (left, bottom)], fill=rgb)
        img.resize((_SIZE, _SIZE), Image.Resampling.LANCZOS).save(out)
    return out.as_posix()


def pause_icon(color: str) -> str:
    """暫停雙直條圖示（透明底）"""
    key = color.lstrip("#")
    out = _CACHE_DIR / f"pause_{_VERSION}_{key}.png"
    if not out.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        s = _SIZE * _SCALE
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(color)
        top, bottom = s * 0.22, s * 0.78
        bar_w = s * 0.16
        gap = s * 0.14
        cx = s / 2
        draw.rectangle([cx - gap / 2 - bar_w, top, cx - gap / 2, bottom], fill=rgb)
        draw.rectangle([cx + gap / 2, top, cx + gap / 2 + bar_w, bottom], fill=rgb)
        img.resize((_SIZE, _SIZE), Image.Resampling.LANCZOS).save(out)
    return out.as_posix()


def arrow_icon(color: str, direction: str) -> str:
    """下拉/上下箭頭圖示（direction: up / down）"""
    key = f"{direction}_{color.lstrip('#')}"
    out = _CACHE_DIR / f"arrow_{_VERSION}_{key}.png"
    if not out.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        s = _SIZE * _SCALE
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(color)
        w, h = s * 0.42, s * 0.24
        cx, cy = s / 2, s / 2
        if direction == "down":
            pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx, cy + h / 2)]
        else:
            pts = [(cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2), (cx, cy - h / 2)]
        draw.polygon(pts, fill=rgb)
        img.resize((_SIZE, _SIZE), Image.Resampling.LANCZOS).save(out)
    return out.as_posix()
