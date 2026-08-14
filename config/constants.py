"""常數定義"""

APP_NAME = "JR-SpineResize"
APP_TITLE = "Spine 貼圖等比縮放工具"
ORG_NAME = "JR"

# ---------------------------------------------------------------- 檔案類型

ATLAS_EXTENSIONS = (".atlas", ".atlas.txt")
SKELETON_BINARY_EXTENSIONS = (".skel", ".skel.bytes")
SKELETON_JSON_EXTENSIONS = (".json",)
PAGE_EXTENSIONS = (".png", ".webp", ".jpg", ".jpeg")

# ---------------------------------------------------------------- Atlas 格式

# 頁面層級的屬性鍵（不縮排）
PAGE_KEYS = frozenset({"size", "format", "filter", "repeat", "pma", "scale"})

# 區塊層級的屬性鍵
REGION_KEYS_LEGACY = frozenset({"rotate", "xy", "size", "orig", "offset", "index", "split", "pad"})
REGION_KEYS_MODERN = frozenset({"bounds", "offsets", "rotate", "index", "split", "pad"})
REGION_KEYS = REGION_KEYS_LEGACY | REGION_KEYS_MODERN

ATLAS_STYLE_LEGACY = "legacy"   # Spine <= 4.0：rotate / xy / size / orig / offset
ATLAS_STYLE_MODERN = "modern"   # Spine >= 4.1：bounds / offsets / rotate(角度)

# ---------------------------------------------------------------- 處理選項

MODE_RESCALE = "rescale"        # 由本工具負責縮圖並重寫 atlas
MODE_REMAP_ONLY = "remap_only"  # 貼圖已在外部縮好，只重算 atlas 數值

RESAMPLE_FILTERS = {
    "lanczos": "Lanczos（縮小最佳，預設）",
    "bicubic": "Bicubic（較銳利）",
    "bilinear": "Bilinear（最快）",
    "box": "Box（純平均，等比整數倍最穩）",
    "nearest": "Nearest（Pixel Art 專用）",
}
DEFAULT_RESAMPLE = "lanczos"

ALPHA_MODE_PREMULTIPLY = "premultiply"  # 預乘 alpha 後再縮放，避免透明邊黑邊
ALPHA_MODE_NONE = "none"                # 直接縮放（與一般圖片工具行為一致）
DEFAULT_ALPHA_MODE = ALPHA_MODE_PREMULTIPLY

BLEED_NONE = "none"
BLEED_RGB = "rgb"    # 只把顏色滲進透明區（不改變輪廓）— 推薦
BLEED_FULL = "full"  # 連 alpha 一起外擴（部分引擎需要）
DEFAULT_BLEED = BLEED_RGB
DEFAULT_BLEED_PX = 2

PAGE_ALIGN_NONE = 1
PAGE_ALIGN_4 = 4
PAGE_ALIGN_POT = -1  # 補到 2 的次方（只補畫布、不改縮放比例）

# 貼圖輸出的色彩編碼。
#
# 這件事對檔案大小的影響比縮放本身還大：已經被 pngquant / TinyPNG 量化過的
# 素材是 8-bit 調色盤（每像素 1 byte），若讀進來後存成 32-bit RGBA（每像素
# 4 byte），就算尺寸砍半、像素數只剩 1/4，檔案大小也只是打平甚至變大。
PNG_FORMAT_MATCH = "match"      # 跟隨來源：來源是調色盤就輸出調色盤
PNG_FORMAT_RGBA = "rgba"        # 一律 32-bit RGBA（最高品質）
PNG_FORMAT_PALETTE = "palette"  # 一律 8-bit 調色盤（最小檔案）
DEFAULT_PNG_FORMAT = PNG_FORMAT_MATCH

PALETTE_COLORS = 256
DEFAULT_DITHERING = 0.0

OUTPUT_SUBFOLDER = "subfolder"
OUTPUT_CUSTOM = "custom"
OUTPUT_INPLACE = "inplace"
DEFAULT_SUBFOLDER_NAME = "resized"

# ---------------------------------------------------------------- 限制

MIN_SCALE_PERCENT = 1.0
MAX_SCALE_PERCENT = 400.0
DEFAULT_SCALE_PERCENT = 50.0
MAX_PAGE_SIZE = 16384
