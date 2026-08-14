"""
壓縮引擎（移植自 JR-Img-Compresser，演算法與行為一致）

壓縮管線：
- PNG 無損: Pillow 編碼 → oxipng 無損最佳化（零品質損失）
- PNG 有損: imagequant（pngquant 核心，同 TinyPNG）量化 → oxipng 無損最佳化
- JPEG:    Pillow(libjpeg) 編碼 → mozjpeg 無損最佳化
- WebP:    Pillow(libwebp) 編碼（有損/無損 + alpha）

另支援「目標檔案大小」：二分搜尋符合目標大小的最高品質。
本模組不相依 Qt，可在背景執行緒與批次腳本中直接使用。
"""
from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from typing import Optional, Tuple

from PIL import Image, ImageChops

try:
    import imagequant
    HAS_IMAGEQUANT = True
except ImportError:
    HAS_IMAGEQUANT = False

try:
    import oxipng
    HAS_OXIPNG = True
except ImportError:
    HAS_OXIPNG = False

try:
    import mozjpeg_lossless_optimization
    HAS_MOZJPEG = True
except ImportError:
    HAS_MOZJPEG = False

from models.compression_options import (
    ChromaSubsampling,
    CompressionEffort,
    CompressionOptions,
    PngColorFormat,
    PngMode,
)

# Pillow 的 JPEG subsampling 參數對應
_SUBSAMPLING_MAP = {
    ChromaSubsampling.CS_444: 0,
    ChromaSubsampling.CS_422: 1,
    ChromaSubsampling.CS_420: 2,
}

# oxipng 最佳化等級對應
_OXIPNG_LEVEL = {
    CompressionEffort.FAST: 2,
    CompressionEffort.STANDARD: 4,
    CompressionEffort.MAX: 6,
}

# PNG 色彩格式 → (R, G, B, A) 各通道位元深度；A 為 0 表示不保留透明通道。
# RGBA8888 不在表中（不需量化）。
_PNG_FORMAT_BITS = {
    PngColorFormat.RGBA5551: (5, 5, 5, 1),
    PngColorFormat.RGBA4444: (4, 4, 4, 4),
    PngColorFormat.RGB565: (5, 6, 5, 0),
}

# 8x8 Bayer 有序抖動矩陣（值 0-63）。引擎把貼圖轉成 16-bit 時
# 慣用的就是有序抖動，用它模擬最接近實際結果。
_BAYER_8X8 = (
    0, 32, 8, 40, 2, 34, 10, 42,
    48, 16, 56, 24, 50, 18, 58, 26,
    12, 44, 4, 36, 14, 46, 6, 38,
    60, 28, 52, 20, 62, 30, 54, 22,
    3, 35, 11, 43, 1, 33, 9, 41,
    51, 19, 59, 27, 49, 17, 57, 25,
    15, 47, 7, 39, 13, 45, 5, 37,
    63, 31, 55, 23, 61, 29, 53, 21,
)


@lru_cache(maxsize=16)
def _quantize_lut(bits: int) -> bytes:
    """
    產生「截斷至 n bits 後再位元複製回 0-255」的 256 階查表

    位元複製（bit replication）與 GPU 展開 RGB565→RGB888 的做法一致：
    0 仍映射到 0、255 仍映射到 255，不會讓整張圖偏暗。
    """
    if bits >= 8:
        return bytes(range(256))
    if bits <= 0:
        # 不保留透明通道 → 全部視為不透明
        return bytes([255]) * 256

    mask = (0xFF << (8 - bits)) & 0xFF
    lut = bytearray(256)
    for v in range(256):
        top = v & mask
        out = top
        shift = bits
        while shift < 8:
            out |= top >> shift
            shift += bits
        lut[v] = out
    return bytes(lut)


def _dither_tile(size: Tuple[int, int], step: int) -> Image.Image:
    """
    產生與圖片同尺寸的 Bayer 抖動偏移圖（值域 0 ~ step-1）

    以倍增貼上的方式平鋪 8x8 樣板，避免逐格 paste 的迴圈開銷。
    """
    cell = Image.new("L", (8, 8))
    cell.putdata([(v * step) // 64 for v in _BAYER_8X8])

    width, height = size
    tile = cell
    while tile.width < width or tile.height < height:
        tw, th = tile.size
        new_w = tw * 2 if tw < width else tw
        new_h = th * 2 if th < height else th
        grown = Image.new("L", (new_w, new_h))
        for offset_x in range(0, new_w, tw):
            for offset_y in range(0, new_h, th):
                grown.paste(tile, (offset_x, offset_y))
        tile = grown

    return tile.crop((0, 0, width, height))


def apply_png_color_format(
    image: Image.Image,
    options: CompressionOptions,
) -> Image.Image:
    """
    將像素量化至目標色彩格式的通道位元深度（模擬遊戲引擎貼圖轉檔）

    PNG 規格不允許 RGBA4444 / RGB565 這類位元深度，因此這裡只量化
    像素值，實際仍存成合法的 8-bit PNG。畫面與引擎轉檔後一致，
    且因色階數大減，後續 oxipng / imagequant 能壓得更小。
    """
    bits = _PNG_FORMAT_BITS.get(options.png_color_format)
    if bits is None:
        return image  # RGBA8888：維持原始像素

    red_bits, green_bits, blue_bits, alpha_bits = bits

    if alpha_bits == 0:
        # RGB565 沒有透明通道：先把透明區合成到白底再量化
        image = _flatten_rgba_to_rgb(image)
        channel_bits = (red_bits, green_bits, blue_bits)
    else:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        channel_bits = (red_bits, green_bits, blue_bits, alpha_bits)

    channels = []
    for index, (channel, channel_bit) in enumerate(zip(image.split(), channel_bits)):
        # 只對 RGB 抖動：alpha 抖動會讓半透明邊緣出現雜點
        if options.png_format_dither and index < 3 and 0 < channel_bit < 8:
            step = 256 >> channel_bit
            if step > 1:
                channel = ImageChops.add(
                    channel, _dither_tile(image.size, step), 1.0, -(step // 2)
                )
        channels.append(channel.point(_quantize_lut(channel_bit)))

    result = Image.merge(image.mode, channels)
    result.info = dict(image.info)
    return result


def snap_palette_to_color_format(
    image: Image.Image,
    options: CompressionOptions,
) -> Image.Image:
    """
    將 P 模式（調色盤）影像的調色盤對齊回目標色彩格式的色階

    imagequant / PIL quantize 產生調色盤時會「平均」相近顏色，
    算出來的色值不一定落在 4444 / 565 的格線上。調色盤只有 256 筆，
    修正成本可忽略。
    """
    bits = _PNG_FORMAT_BITS.get(options.png_color_format)
    if bits is None or image.mode != "P":
        return image

    palette = image.getpalette()
    transparency = image.info.get("transparency")

    if palette:
        rgb_luts = (
            _quantize_lut(bits[0]),
            _quantize_lut(bits[1]),
            _quantize_lut(bits[2]),
        )
        image.putpalette(
            [rgb_luts[i % 3][v] for i, v in enumerate(palette)]
        )

    # putpalette 可能清掉 transparency，這裡量化後再寫回
    if isinstance(transparency, (bytes, bytearray)):
        alpha_lut = _quantize_lut(bits[3])
        image.info["transparency"] = bytes(alpha_lut[v] for v in transparency)
    elif transparency is not None:
        image.info["transparency"] = transparency

    return image


def _flatten_rgba_to_rgb(image: Image.Image) -> Image.Image:
    """將圖片轉為 RGB（透明區域填充白色背景）"""
    if image.mode == "P":
        image = image.convert("RGBA")
    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        background.info = dict(image.info)
        return background
    if image.mode != "RGB":
        converted = image.convert("RGB")
        converted.info = dict(image.info)
        return converted
    return image


def format_for_suffix(suffix: str) -> str:
    """由貼圖副檔名決定壓縮格式（貼圖永遠保持原格式）"""
    suffix = suffix.lower().lstrip(".")
    if suffix in ("jpg", "jpeg"):
        return "JPEG"
    if suffix == "webp":
        return "WEBP"
    return "PNG"


def describe_encoding(options: CompressionOptions, format: str) -> str:
    """輸出編碼的人類可讀描述（報告與檔案清單顯示用）"""
    if format == "JPEG":
        return f"JPEG Q{options.quality}"
    if format == "WEBP":
        return "WebP 無損" if options.webp_lossless else f"WebP Q{options.webp_quality}"
    if options.target_size_enabled and options.png_mode == PngMode.LOSSY:
        return f"PNG 智慧有損・目標 {options.target_size_kb} KB"
    return f"PNG {options.describe_png()}"


class Compressor:
    """圖片壓縮引擎（JPG / PNG / WebP）"""

    def compress(
        self,
        image: Image.Image,
        options: CompressionOptions,
        format: str = "PNG",
        fast: bool = False,
    ) -> Tuple[Image.Image, bytes]:
        """
        壓縮圖片

        Args:
            image: PIL Image 物件
            options: 壓縮選項
            format: 輸出格式（JPEG / PNG / WEBP）
            fast: 快速模式（預覽/估算用，降低無損最佳化強度以加速）

        Returns:
            (處理後的 Image（供預覽）, 壓縮後的 bytes)
        """
        format = format.upper()
        if format == "JPG":
            format = "JPEG"

        # 目標檔案大小模式（僅適用於有損路徑）
        if options.target_size_enabled and self._supports_target_size(format, options):
            return self._compress_to_target(image, options, format, fast)

        return self._compress_once(image, options, format, fast)

    def _compress_once(
        self,
        image: Image.Image,
        options: CompressionOptions,
        format: str,
        fast: bool,
        quality_override: Optional[int] = None,
    ) -> Tuple[Image.Image, bytes]:
        """執行一次壓縮（quality_override 供目標大小搜尋使用）"""
        if format == "JPEG":
            return self._compress_jpeg(image, options, fast, quality_override)
        elif format == "PNG":
            return self._compress_png(image, options, fast, quality_override)
        elif format == "WEBP":
            return self._compress_webp(image, options, fast, quality_override)
        else:
            buffer = BytesIO()
            image.save(buffer, format=format)
            return image, buffer.getvalue()

    # ------------------------------------------------------------------
    # JPEG
    # ------------------------------------------------------------------

    def _compress_jpeg(
        self,
        image: Image.Image,
        options: CompressionOptions,
        fast: bool = False,
        quality_override: Optional[int] = None,
    ) -> Tuple[Image.Image, bytes]:
        """Pillow 編碼後，再以 mozjpeg 做無損最佳化（同品質再省 5~10%）"""
        image = _flatten_rgba_to_rgb(image)
        quality = quality_override if quality_override is not None else options.quality

        save_kwargs = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
        }

        subsampling = _SUBSAMPLING_MAP.get(options.chroma_subsampling)
        if subsampling is not None:
            save_kwargs["subsampling"] = subsampling

        if options.progressive:
            save_kwargs["progressive"] = True

        # metadata 處理：不傳 exif 即等於移除
        if not options.remove_exif:
            exif = image.info.get("exif")
            if exif:
                save_kwargs["exif"] = exif

        # 保留 ICC 色彩描述檔（影響顏色正確性，不佔多少空間）
        icc = image.info.get("icc_profile")
        if icc:
            save_kwargs["icc_profile"] = icc

        buffer = BytesIO()
        image.save(buffer, **save_kwargs)
        data = buffer.getvalue()

        if HAS_MOZJPEG:
            try:
                optimized = mozjpeg_lossless_optimization.optimize(data)
                if 0 < len(optimized) < len(data):
                    data = optimized
            except Exception:
                pass  # 最佳化失敗時保留原始編碼結果

        return image, data

    # ------------------------------------------------------------------
    # PNG
    # ------------------------------------------------------------------

    def _compress_png(
        self,
        image: Image.Image,
        options: CompressionOptions,
        fast: bool = False,
        quality_override: Optional[int] = None,
    ) -> Tuple[Image.Image, bytes]:
        """
        - 無損模式: Pillow 編碼 → oxipng 最佳化（零品質損失）
        - 有損模式: imagequant 量化 → oxipng 最佳化（類似 TinyPNG）

        若指定了非 RGBA8888 的色彩格式，會先做通道位元深度量化。
        """
        image = apply_png_color_format(image, options)

        if options.png_mode == PngMode.LOSSLESS:
            return self._compress_png_lossless(image, options, fast)

        quality = (
            quality_override if quality_override is not None else options.png_quality
        )
        if HAS_IMAGEQUANT:
            return self._compress_png_lossy(image, options, quality, fast)
        return self._compress_png_fallback(image, options, quality, fast)

    def _compress_png_lossless(
        self,
        image: Image.Image,
        options: CompressionOptions,
        fast: bool,
    ) -> Tuple[Image.Image, bytes]:
        """無損 PNG 壓縮（保留像素，僅最佳化編碼）"""
        buffer = BytesIO()
        # oxipng 會重新最佳化 DEFLATE，這裡不必用最慢的 level 9
        save_kwargs = {"format": "PNG", "compress_level": 6}
        if "transparency" in image.info:
            save_kwargs["transparency"] = image.info["transparency"]
        image.save(buffer, **save_kwargs)
        data = self._oxipng_pass(buffer.getvalue(), options, fast)
        preview = image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
        return preview, data

    def _compress_png_lossy(
        self,
        image: Image.Image,
        options: CompressionOptions,
        quality: int,
        fast: bool,
    ) -> Tuple[Image.Image, bytes]:
        """
        有損 PNG 壓縮（imagequant 量化 → oxipng 最佳化）

        min_quality 固定為 0：quality 滑桿即品質上限，
        引擎會盡力達到該品質，不會失敗中斷。
        """
        if image.mode != "RGBA":
            image = image.convert("RGBA")

        quantized = imagequant.quantize_pil_image(
            image,
            dithering_level=max(0.0, min(1.0, options.png_dithering)),
            max_colors=256,
            min_quality=0,
            max_quality=max(1, min(100, quality)),
        )

        # imagequant 的調色盤是平均出來的，需再對齊回目標色彩格式
        quantized = snap_palette_to_color_format(quantized, options)

        buffer = BytesIO()
        save_kwargs = {"format": "PNG", "compress_level": 6}
        if "transparency" in quantized.info:
            save_kwargs["transparency"] = quantized.info["transparency"]
        quantized.save(buffer, **save_kwargs)

        data = self._oxipng_pass(buffer.getvalue(), options, fast)
        preview_image = quantized.convert("RGBA")
        return preview_image, data

    def _oxipng_pass(
        self,
        data: bytes,
        options: CompressionOptions,
        fast: bool,
    ) -> bytes:
        """oxipng 無損最佳化（重新選擇 filter 與 DEFLATE 編碼）"""
        if not HAS_OXIPNG:
            return data
        try:
            level = 1 if fast else _OXIPNG_LEVEL.get(options.effort, 4)
            kwargs = {"level": level}
            if options.remove_exif:
                kwargs["strip"] = oxipng.StripChunks.safe()
            optimized = oxipng.optimize_from_memory(data, **kwargs)
            if 0 < len(optimized) < len(data):
                return optimized
        except Exception:
            pass  # 最佳化失敗時保留 Pillow 編碼結果
        return data

    def _compress_png_fallback(
        self,
        image: Image.Image,
        options: CompressionOptions,
        quality: int,
        fast: bool,
    ) -> Tuple[Image.Image, bytes]:
        """備用 PNG 有損壓縮（未安裝 imagequant 時使用 PIL 內建量化）"""
        colors = self._quality_to_colors(quality)

        alpha_channel = None
        if image.mode == "RGBA":
            alpha_channel = image.split()[-1]
            rgb_image = image.convert("RGB")
        elif image.mode == "P":
            rgb_image = image.convert("RGB")
        else:
            rgb_image = image

        dither = (
            Image.Dither.FLOYDSTEINBERG
            if options.png_dithering > 0
            else Image.Dither.NONE
        )
        quantized = rgb_image.quantize(
            colors=colors,
            method=Image.Quantize.MEDIANCUT,
            dither=dither,
        )
        quantized = snap_palette_to_color_format(quantized, options)

        if alpha_channel is not None:
            processed_image = quantized.convert("RGBA")
            processed_image.putalpha(alpha_channel)
            out_image = processed_image
        else:
            out_image = quantized

        buffer = BytesIO()
        out_image.save(buffer, format="PNG", compress_level=6)
        data = self._oxipng_pass(buffer.getvalue(), options, fast)

        preview = out_image.convert("RGBA") if alpha_channel is not None else out_image.convert("RGB")
        return preview, data

    def _quality_to_colors(self, quality: int) -> int:
        """將品質值轉換為色彩數量（備用方案用）"""
        if quality >= 90:
            return 256
        elif quality >= 70:
            return 128 + int((quality - 70) * 127 / 20)
        elif quality >= 40:
            return 64 + int((quality - 40) * 63 / 30)
        elif quality >= 10:
            return 16 + int((quality - 10) * 47 / 30)
        else:
            return 8 + int((quality - 1) * 7 / 8)

    # ------------------------------------------------------------------
    # WebP
    # ------------------------------------------------------------------

    def _compress_webp(
        self,
        image: Image.Image,
        options: CompressionOptions,
        fast: bool = False,
        quality_override: Optional[int] = None,
    ) -> Tuple[Image.Image, bytes]:
        """WebP 壓縮（有損/無損，自動保留透明度）"""
        if image.mode == "P":
            image = image.convert("RGBA")
        elif image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")

        quality = (
            quality_override if quality_override is not None else options.webp_quality
        )

        save_kwargs = {
            "format": "WEBP",
            # method 6 壓縮率最好；快速模式用 4 加速預覽
            "method": 4 if fast else 6,
        }
        if options.webp_lossless:
            save_kwargs["lossless"] = True
            save_kwargs["quality"] = 100
        else:
            save_kwargs["quality"] = max(1, min(100, quality))

        if options.remove_exif:
            save_kwargs["exif"] = b""
        else:
            exif = image.info.get("exif")
            if exif:
                save_kwargs["exif"] = exif

        icc = image.info.get("icc_profile")
        if icc:
            save_kwargs["icc_profile"] = icc

        buffer = BytesIO()
        image.save(buffer, **save_kwargs)
        return image, buffer.getvalue()

    # ------------------------------------------------------------------
    # 目標檔案大小
    # ------------------------------------------------------------------

    def _supports_target_size(self, format: str, options: CompressionOptions) -> bool:
        """檢查此格式/模式是否支援目標檔案大小搜尋"""
        if format == "JPEG":
            return True
        if format == "WEBP":
            return not options.webp_lossless
        if format == "PNG":
            return options.png_mode == PngMode.LOSSY and HAS_IMAGEQUANT
        return False

    # 目標大小搜尋的品質下限（設為 1 才能逼近激進目標）
    _TARGET_MIN_QUALITY = 1
    _TARGET_MAX_QUALITY = 100

    def _compress_to_target(
        self,
        image: Image.Image,
        options: CompressionOptions,
        format: str,
        fast: bool,
    ) -> Tuple[Image.Image, bytes]:
        """二分搜尋「檔案大小 <= 目標」的最高品質"""
        target_bytes = max(1, options.target_size_kb) * 1024

        lo = self._TARGET_MIN_QUALITY
        hi = self._TARGET_MAX_QUALITY

        # 先壓最低品質，作為保底結果（無論能否達標都是最小可得）
        result_min = self._compress_once(
            image, options, format, True, quality_override=lo
        )
        best = result_min
        best_quality = lo

        if len(result_min[1]) <= target_bytes:
            while lo <= hi:
                mid = (lo + hi) // 2
                result = self._compress_once(
                    image, options, format, True, quality_override=mid
                )
                if len(result[1]) <= target_bytes:
                    best = result
                    best_quality = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
        # else: 連最低品質都超標，best 維持最低品質結果（已是最小）

        # 以正常強度做最終壓縮（搜尋過程用快速模式加速）
        if not fast:
            return self._compress_once(
                image, options, format, False, quality_override=best_quality
            )
        return best
