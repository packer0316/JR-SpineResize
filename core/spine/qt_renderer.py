"""
QPainter 骨架渲染器

* RegionAttachment：整張區塊影像做一次仿射變換（骨骼世界矩陣是仿射，精確）
* MeshAttachment：逐三角形——以三角形裁切 + 仿射貼圖（經典 2D 軟體貼圖法）
* ClippingAttachment：轉成 QPainterPath 裁切，效果等同 runtime 的多邊形裁切
* Blend mode：normal / additive / multiply / screen 對應 QPainter 合成模式
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QPainter, QPainterPath, QTransform

from core.spine.runtime import Skeleton
from core.spine.skeleton_data import (
    BLEND_ADDITIVE,
    BLEND_MULTIPLY,
    BLEND_SCREEN,
    ClippingAttachment,
    MeshAttachment,
    RegionAttachment,
)
from core.spine.texture_store import AtlasTextureStore

_COMPOSITION = {
    BLEND_ADDITIVE: QPainter.CompositionMode.CompositionMode_Plus,
    BLEND_MULTIPLY: QPainter.CompositionMode.CompositionMode_Multiply,
    BLEND_SCREEN: QPainter.CompositionMode.CompositionMode_Screen,
}


def _affine_from_triangles(sx0, sy0, sx1, sy1, sx2, sy2,
                           dx0, dy0, dx1, dy1, dx2, dy2) -> QTransform | None:
    """求把來源三角形映到目標三角形的仿射矩陣（Qt row-vector 慣例）"""
    ax, ay = sx1 - sx0, sy1 - sy0
    bx, by = sx2 - sx0, sy2 - sy0
    det = ax * by - bx * ay
    if abs(det) < 1e-9:
        return None
    ux, uy = dx1 - dx0, dy1 - dy0
    vx, vy = dx2 - dx0, dy2 - dy0
    inv = 1.0 / det
    m11 = (ux * by - vx * ay) * inv
    m12 = (uy * by - vy * ay) * inv
    m21 = (vx * ax - ux * bx) * inv
    m22 = (vy * ax - uy * bx) * inv
    dx = dx0 - (m11 * sx0 + m21 * sy0)
    dy = dy0 - (m12 * sx0 + m22 * sy0)
    return QTransform(m11, m12, m21, m22, dx, dy)


class SkeletonRenderer:
    """把 Skeleton 目前姿勢畫到 QPainter 上（painter 需已套好視圖變換）"""

    def __init__(self, textures: AtlasTextureStore) -> None:
        self.textures = textures
        # 每影格追蹤繪製範圍，供播放器自動取景
        self.last_bounds: tuple[float, float, float, float] | None = None

    def render(self, painter: QPainter, skeleton: Skeleton) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        base_transform = painter.transform()
        base_clip = painter.clipPath() if painter.hasClipping() else None

        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        clip_path: QPainterPath | None = None
        clip_end_slot = None

        for slot in skeleton.draw_order:
            attachment = slot.attachment

            # 裁切結束
            if clip_end_slot is not None and slot.data is clip_end_slot:
                clip_path = None
                clip_end_slot = None
                painter.setTransform(base_transform)
                if base_clip is not None:
                    painter.setClipPath(base_clip)
                else:
                    painter.setClipping(False)

            if isinstance(attachment, ClippingAttachment):
                verts = skeleton.compute_world_vertices(slot, attachment)
                path = QPainterPath()
                if len(verts) >= 6:
                    path.moveTo(verts[0], verts[1])
                    for i in range(2, len(verts), 2):
                        path.lineTo(verts[i], verts[i + 1])
                    path.closeSubpath()
                    clip_path = path
                    clip_end_slot = attachment.end_slot
                continue

            alpha = slot.color[3] * (attachment.color[3] if isinstance(attachment, (RegionAttachment, MeshAttachment)) else 1.0)
            if alpha <= 0.003:
                continue

            if isinstance(attachment, RegionAttachment):
                bounds = self._draw_region(painter, skeleton, slot, attachment, alpha,
                                           base_transform, base_clip, clip_path)
            elif isinstance(attachment, MeshAttachment):
                bounds = self._draw_mesh(painter, skeleton, slot, attachment, alpha,
                                         base_transform, base_clip, clip_path)
            else:
                continue
            if bounds is not None:
                min_x = min(min_x, bounds[0])
                min_y = min(min_y, bounds[1])
                max_x = max(max_x, bounds[2])
                max_y = max(max_y, bounds[3])

        painter.setTransform(base_transform)
        if base_clip is not None:
            painter.setClipPath(base_clip)
        else:
            painter.setClipping(False)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setOpacity(1.0)

        if min_x < max_x:
            self.last_bounds = (min_x, min_y, max_x, max_y)

    # ------------------------------------------------------------ region

    def _tinted_image(self, slot, attachment_color, path: str):
        r = slot.color[0] * attachment_color[0]
        g = slot.color[1] * attachment_color[1]
        b = slot.color[2] * attachment_color[2]
        return self.textures.get_tinted(path, (r, g, b))

    def _setup_painter(self, painter, slot, alpha, base_transform, base_clip, clip_path) -> None:
        painter.setTransform(base_transform)
        if clip_path is not None:
            painter.setClipPath(clip_path if base_clip is None else clip_path.intersected(base_clip))
        elif base_clip is not None:
            painter.setClipPath(base_clip)
        else:
            painter.setClipping(False)
        painter.setOpacity(alpha)
        painter.setCompositionMode(
            _COMPOSITION.get(slot.data.blend_mode, QPainter.CompositionMode.CompositionMode_SourceOver)
        )

    def _draw_region(self, painter, skeleton, slot, attachment, alpha,
                     base_transform, base_clip, clip_path):
        texture = self.textures.get(attachment.path)
        if texture is None:
            return None
        image = self._tinted_image(slot, attachment.color, attachment.path)
        if image is None:
            return None
        ow, oh = texture.orig_w, texture.orig_h
        if ow <= 0 or oh <= 0:
            return None

        # 影像像素 -> attachment local（y 翻轉 + 置中 + 縮放到 attachment 尺寸 + 旋轉平移）
        sx = attachment.width / ow * attachment.scale_x if attachment.width else attachment.scale_x
        sy = attachment.height / oh * attachment.scale_y if attachment.height else attachment.scale_y
        local = QTransform()
        local.translate(attachment.x, attachment.y)
        local.rotate(attachment.rotation)
        local.scale(sx, -sy)
        local.translate(-ow / 2.0, -oh / 2.0)

        bone = slot.bone
        world = QTransform(bone.a, bone.c, bone.b, bone.d, bone.world_x, bone.world_y)
        img_to_world = local * world

        self._setup_painter(painter, slot, alpha, base_transform, base_clip, clip_path)
        painter.setTransform(img_to_world * base_transform)
        painter.drawImage(0, 0, image)

        # bounds：四角映射
        corners = [img_to_world.map(QPointF(px, py))
                   for px, py in ((0, 0), (ow, 0), (0, oh), (ow, oh))]
        xs = [p.x() for p in corners]
        ys = [p.y() for p in corners]
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------ mesh

    def _draw_mesh(self, painter, skeleton, slot, attachment, alpha,
                   base_transform, base_clip, clip_path):
        texture = self.textures.get(attachment.path)
        if texture is None or not attachment.triangles:
            return None
        image = self._tinted_image(slot, attachment.color, attachment.path)
        if image is None:
            return None

        verts = skeleton.compute_world_vertices(slot, attachment)
        uvs = attachment.uvs
        ow, oh = texture.orig_w, texture.orig_h

        self._setup_painter(painter, slot, alpha, base_transform, base_clip, clip_path)

        min_x = min(verts[0::2])
        max_x = max(verts[0::2])
        min_y = min(verts[1::2])
        max_y = max(verts[1::2])

        triangles = attachment.triangles
        combined_clip = clip_path if base_clip is None or clip_path is None else clip_path.intersected(base_clip)
        if combined_clip is None:
            combined_clip = base_clip
        for t in range(0, len(triangles), 3):
            i0, i1, i2 = triangles[t], triangles[t + 1], triangles[t + 2]
            dx0, dy0 = verts[i0 * 2], verts[i0 * 2 + 1]
            dx1, dy1 = verts[i1 * 2], verts[i1 * 2 + 1]
            dx2, dy2 = verts[i2 * 2], verts[i2 * 2 + 1]
            sx0, sy0 = uvs[i0 * 2] * ow, uvs[i0 * 2 + 1] * oh
            sx1, sy1 = uvs[i1 * 2] * ow, uvs[i1 * 2 + 1] * oh
            sx2, sy2 = uvs[i2 * 2] * ow, uvs[i2 * 2 + 1] * oh
            matrix = _affine_from_triangles(sx0, sy0, sx1, sy1, sx2, sy2,
                                            dx0, dy0, dx1, dy1, dx2, dy2)
            if matrix is None:
                continue
            tri = QPainterPath()
            tri.moveTo(dx0, dy0)
            tri.lineTo(dx1, dy1)
            tri.lineTo(dx2, dy2)
            tri.closeSubpath()
            painter.setTransform(base_transform)
            if combined_clip is not None:
                painter.setClipPath(combined_clip.intersected(tri))
            else:
                painter.setClipPath(tri)
            painter.setTransform(matrix * base_transform)
            # 只畫三角形涵蓋的來源範圍，避免整張圖走一次合成
            src_min_x = max(0, int(min(sx0, sx1, sx2)) - 1)
            src_min_y = max(0, int(min(sy0, sy1, sy2)) - 1)
            src_max_x = min(ow, int(max(sx0, sx1, sx2)) + 2)
            src_max_y = min(oh, int(max(sy0, sy1, sy2)) + 2)
            rect = QRectF(src_min_x, src_min_y, src_max_x - src_min_x, src_max_y - src_min_y)
            painter.drawImage(rect, image, rect)
        return min_x, min_y, max_x, max_y
