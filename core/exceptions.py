"""自訂例外"""


class SpineResizeError(Exception):
    """本工具所有例外的基底"""


class AtlasParseError(SpineResizeError):
    """.atlas 檔案格式無法解析"""


class SkeletonParseError(SpineResizeError):
    """.skel / .json 骨架檔無法解析"""


class PageImageError(SpineResizeError):
    """atlas 參照的貼圖頁面缺失或無法讀取"""


class ProcessError(SpineResizeError):
    """處理過程失敗"""
