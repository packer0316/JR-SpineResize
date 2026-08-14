"""
Spine 3.8 binary（.skel）完整解析器

格式對應 spine-runtimes 3.8 的 SkeletonBinary.java。僅供預覽播放使用，
解析結果永遠不會寫回檔案。

正確性驗證方式：解析必須「剛好」消耗到檔案結尾——任何欄位順序或型別
讀錯都會讓後續資料流錯位，幾乎不可能剛好停在 EOF，因此 EOF 檢查是
非常強的完整性訊號（以 28 份真實素材驗證）。
"""
from __future__ import annotations

import struct
from pathlib import Path

from core.exceptions import SkeletonParseError
from core.spine.animation import (
    AttachmentTimeline,
    ColorTimeline,
    CurveSet,
    DeformTimeline,
    DrawOrderTimeline,
    EventTimeline,
    IkConstraintTimeline,
    PathConstraintMixTimeline,
    PathConstraintValueTimeline,
    RotateTimeline,
    ScaleTimeline,
    ShearTimeline,
    TransformConstraintTimeline,
    TranslateTimeline,
)
from core.spine.skeleton_data import (
    Animation,
    BoneData,
    BoundingBoxAttachment,
    ClippingAttachment,
    EventData,
    IkConstraintData,
    MeshAttachment,
    PathAttachment,
    PathConstraintData,
    PointAttachment,
    RegionAttachment,
    Skin,
    SkeletonData,
    SlotData,
    TransformConstraintData,
    VertexAttachment,
)

# attachment 型別
_AT_REGION = 0
_AT_BOUNDINGBOX = 1
_AT_MESH = 2
_AT_LINKEDMESH = 3
_AT_PATH = 4
_AT_POINT = 5
_AT_CLIPPING = 6

# timeline 型別
_SLOT_ATTACHMENT = 0
_SLOT_COLOR = 1
_SLOT_TWO_COLOR = 2
_BONE_ROTATE = 0
_BONE_TRANSLATE = 1
_BONE_SCALE = 2
_BONE_SHEAR = 3
_PATH_POSITION = 0
_PATH_SPACING = 1
_PATH_MIX = 2

_CURVE_STEPPED = 1
_CURVE_BEZIER = 2


class _Reader:
    """big-endian 二進位讀取器，介面對應 spine 的 DataInput"""

    __slots__ = ("data", "pos", "strings")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.strings: list[str] = []

    def _need(self, count: int) -> None:
        if self.pos + count > len(self.data):
            raise SkeletonParseError(f"資料流在位移 {self.pos} 提前結束")

    def byte(self) -> int:
        self._need(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def sbyte(self) -> int:
        value = self.byte()
        return value - 256 if value >= 128 else value

    def boolean(self) -> bool:
        return self.byte() != 0

    def int32(self) -> int:
        self._need(4)
        value = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def float32(self) -> float:
        self._need(4)
        value = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return value

    def short(self) -> int:
        self._need(2)
        value = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def varint(self, optimize_positive: bool = True) -> int:
        result = 0
        for shift in (0, 7, 14, 21, 28):
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                break
        # 對齊 Java 的 32-bit int 溢位語義：負數（例如 draw order 的負位移）
        # 以 5 bytes 寫入，最高位溢入符號位
        result &= 0xFFFFFFFF
        if optimize_positive:
            if result >= 0x80000000:
                result -= 0x100000000
            return result
        return (result >> 1) ^ -(result & 1)

    def string(self) -> str | None:
        count = self.varint()
        if count == 0:
            return None
        if count == 1:
            return ""
        count -= 1
        self._need(count)
        raw = self.data[self.pos : self.pos + count]
        self.pos += count
        return raw.decode("utf-8", errors="replace")

    def string_ref(self) -> str | None:
        index = self.varint()
        if index == 0:
            return None
        return self.strings[index - 1]

    def floats(self, count: int) -> list[float]:
        self._need(count * 4)
        values = list(struct.unpack_from(f">{count}f", self.data, self.pos))
        self.pos += count * 4
        return values

    def short_array(self) -> list[int]:
        return [self.short() for _ in range(self.varint())]


def _rgba8888(value: int) -> tuple[float, float, float, float]:
    value &= 0xFFFFFFFF
    return (
        ((value >> 24) & 0xFF) / 255.0,
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )


def _read_curve(reader: _Reader, curves: CurveSet, frame: int) -> None:
    kind = reader.byte()
    if kind == _CURVE_STEPPED:
        curves.set_stepped(frame)
    elif kind == _CURVE_BEZIER:
        curves.set_bezier(frame, reader.float32(), reader.float32(), reader.float32(), reader.float32())


def parse_skel(path: Path) -> SkeletonData:
    """解析 3.8 binary .skel。非 3.8 版本會拋出 SkeletonParseError。"""
    data = path.read_bytes()
    reader = _Reader(data)
    skel = SkeletonData()

    reader.string()  # hash
    skel.version = reader.string() or ""
    if not skel.version.startswith("3.8"):
        raise SkeletonParseError(f"{path.name}：預覽播放僅支援 Spine 3.8 骨架（此檔為 {skel.version or '未知'}）")

    skel.x = reader.float32()
    skel.y = reader.float32()
    skel.width = reader.float32()
    skel.height = reader.float32()
    nonessential = reader.boolean()
    if nonessential:
        reader.float32()  # fps
        reader.string()   # images path
        reader.string()   # audio path

    # 字串池
    for _ in range(reader.varint()):
        reader.strings.append(reader.string() or "")

    # ---------------- 骨骼
    for i in range(reader.varint()):
        name = reader.string() or f"bone{i}"
        parent = None if i == 0 else skel.bones[reader.varint()]
        bone = BoneData(index=i, name=name, parent=parent)
        bone.rotation = reader.float32()
        bone.x = reader.float32()
        bone.y = reader.float32()
        bone.scale_x = reader.float32()
        bone.scale_y = reader.float32()
        bone.shear_x = reader.float32()
        bone.shear_y = reader.float32()
        bone.length = reader.float32()
        bone.transform_mode = reader.varint()
        bone.skin_required = reader.boolean()
        if nonessential:
            reader.int32()  # 編輯器顯示顏色
        skel.bones.append(bone)

    # ---------------- Slot
    for i in range(reader.varint()):
        name = reader.string() or f"slot{i}"
        bone = skel.bones[reader.varint()]
        color = _rgba8888(reader.int32())
        dark = reader.int32()
        dark_color = None
        if dark != -1:
            dark_color = _rgba8888(dark)[:3]
        attachment_name = reader.string_ref()
        blend_mode = reader.varint()
        skel.slots.append(SlotData(index=i, name=name, bone=bone, color=color,
                                   dark_color=dark_color, attachment_name=attachment_name,
                                   blend_mode=blend_mode))

    # ---------------- IK 約束
    for _ in range(reader.varint()):
        constraint = IkConstraintData(name=reader.string() or "")
        constraint.order = reader.varint()
        constraint.skin_required = reader.boolean()
        constraint.bones = [skel.bones[reader.varint()] for _ in range(reader.varint())]
        constraint.target = skel.bones[reader.varint()]
        constraint.mix = reader.float32()
        constraint.softness = reader.float32()
        constraint.bend_direction = reader.sbyte()
        constraint.compress = reader.boolean()
        constraint.stretch = reader.boolean()
        constraint.uniform = reader.boolean()
        skel.ik_constraints.append(constraint)

    # ---------------- Transform 約束
    for _ in range(reader.varint()):
        constraint = TransformConstraintData(name=reader.string() or "")
        constraint.order = reader.varint()
        constraint.skin_required = reader.boolean()
        constraint.bones = [skel.bones[reader.varint()] for _ in range(reader.varint())]
        constraint.target = skel.bones[reader.varint()]
        constraint.local = reader.boolean()
        constraint.relative = reader.boolean()
        constraint.offset_rotation = reader.float32()
        constraint.offset_x = reader.float32()
        constraint.offset_y = reader.float32()
        constraint.offset_scale_x = reader.float32()
        constraint.offset_scale_y = reader.float32()
        constraint.offset_shear_y = reader.float32()
        constraint.rotate_mix = reader.float32()
        constraint.translate_mix = reader.float32()
        constraint.scale_mix = reader.float32()
        constraint.shear_mix = reader.float32()
        skel.transform_constraints.append(constraint)

    # ---------------- Path 約束
    for _ in range(reader.varint()):
        constraint = PathConstraintData(name=reader.string() or "")
        constraint.order = reader.varint()
        constraint.skin_required = reader.boolean()
        constraint.bones = [skel.bones[reader.varint()] for _ in range(reader.varint())]
        constraint.target = skel.slots[reader.varint()]
        constraint.position_mode = reader.varint()
        constraint.spacing_mode = reader.varint()
        constraint.rotate_mode = reader.varint()
        constraint.offset_rotation = reader.float32()
        constraint.position = reader.float32()
        constraint.spacing = reader.float32()
        constraint.rotate_mix = reader.float32()
        constraint.translate_mix = reader.float32()
        skel.path_constraints.append(constraint)

    # ---------------- Skins（linked mesh 延後解析）
    linked_meshes: list[tuple[MeshAttachment, str | None, int, str, bool]] = []

    default_skin = _read_skin(reader, skel, True, nonessential, linked_meshes)
    if default_skin is not None:
        skel.default_skin = default_skin
        skel.skins.append(default_skin)
    for _ in range(reader.varint()):
        skel.skins.append(_read_skin(reader, skel, False, nonessential, linked_meshes))

    # linked mesh 綁定到父 mesh
    for mesh, skin_name, slot_index, parent_name, inherit_deform in linked_meshes:
        source = None
        if skin_name is None:
            source = skel.default_skin
        else:
            source = next((s for s in skel.skins if s.name == skin_name), None)
        parent = source.get(slot_index, parent_name) if source else None
        if isinstance(parent, MeshAttachment):
            mesh.parent_mesh = parent
            mesh.uvs = parent.uvs
            mesh.triangles = parent.triangles
            mesh.vertices = parent.vertices
            mesh.bones = parent.bones
            mesh.world_vertices_length = parent.world_vertices_length
            mesh.hull_length = parent.hull_length
            mesh.deform_attachment = parent if inherit_deform else mesh

    # ---------------- 事件
    for _ in range(reader.varint()):
        event = EventData(name=reader.string_ref() or "")
        event.int_value = reader.varint(optimize_positive=False)
        event.float_value = reader.float32()
        event.string_value = reader.string()
        event.audio_path = reader.string()
        if event.audio_path is not None:
            reader.float32()  # volume
            reader.float32()  # balance
        skel.events.append(event)

    # ---------------- 動畫
    for _ in range(reader.varint()):
        name = reader.string() or ""
        skel.animations.append(_read_animation(reader, skel, name))

    if reader.pos != len(data):
        raise SkeletonParseError(
            f"{path.name}：解析後仍有 {len(data) - reader.pos} bytes 未消耗，格式判讀有誤"
        )
    return skel


# ---------------------------------------------------------------- Skin

def _read_skin(reader: _Reader, skel: SkeletonData, default_skin: bool,
               nonessential: bool, linked_meshes: list) -> Skin | None:
    if default_skin:
        slot_count = reader.varint()
        if slot_count == 0:
            return None
        skin = Skin(name="default")
    else:
        skin = Skin(name=reader.string_ref() or "")
        for _ in range(reader.varint()):  # bones
            reader.varint()
        for _ in range(3):                # ik / transform / path 約束索引
            for _ in range(reader.varint()):
                reader.varint()
        slot_count = reader.varint()

    for _ in range(slot_count):
        slot_index = reader.varint()
        for _ in range(reader.varint()):
            name = reader.string_ref() or ""
            attachment = _read_attachment(reader, skel, slot_index, name, nonessential, linked_meshes)
            if attachment is not None:
                skin.attachments[(slot_index, name)] = attachment
    return skin


def _read_vertices(reader: _Reader, vertex_count: int) -> tuple[list[float], list[int] | None]:
    """回傳 (vertices, bones)。加權時 vertices 為 x,y,weight 三元組展平。"""
    if not reader.boolean():
        return reader.floats(vertex_count * 2), None
    vertices: list[float] = []
    bones: list[int] = []
    for _ in range(vertex_count):
        bone_count = reader.varint()
        bones.append(bone_count)
        for _ in range(bone_count):
            bones.append(reader.varint())
            vertices.append(reader.float32())  # x
            vertices.append(reader.float32())  # y
            vertices.append(reader.float32())  # weight
    return vertices, bones


def _read_attachment(reader: _Reader, skel: SkeletonData, slot_index: int,
                     attachment_name: str, nonessential: bool, linked_meshes: list):
    name = reader.string_ref() or attachment_name
    kind = reader.byte()

    if kind == _AT_REGION:
        path = reader.string_ref()
        attachment = RegionAttachment(name=name, path=path or name)
        attachment.rotation = reader.float32()
        attachment.x = reader.float32()
        attachment.y = reader.float32()
        attachment.scale_x = reader.float32()
        attachment.scale_y = reader.float32()
        attachment.width = reader.float32()
        attachment.height = reader.float32()
        attachment.color = _rgba8888(reader.int32())
        return attachment

    if kind == _AT_BOUNDINGBOX:
        vertex_count = reader.varint()
        vertices, bones = _read_vertices(reader, vertex_count)
        if nonessential:
            reader.int32()
        return BoundingBoxAttachment(name=name, world_vertices_length=vertex_count * 2,
                                     vertices=vertices, bones=bones)

    if kind == _AT_MESH:
        path = reader.string_ref()
        color = _rgba8888(reader.int32())
        vertex_count = reader.varint()
        uvs = reader.floats(vertex_count * 2)
        triangles = reader.short_array()
        vertices, bones = _read_vertices(reader, vertex_count)
        hull_length = reader.varint()
        if nonessential:
            reader.short_array()  # edges
            reader.float32()      # width
            reader.float32()      # height
        return MeshAttachment(name=name, path=path or name, color=color,
                              world_vertices_length=vertex_count * 2,
                              vertices=vertices, bones=bones, uvs=uvs,
                              triangles=triangles, hull_length=hull_length)

    if kind == _AT_LINKEDMESH:
        path = reader.string_ref()
        color = _rgba8888(reader.int32())
        skin_name = reader.string_ref()
        parent_name = reader.string_ref() or ""
        inherit_deform = reader.boolean()
        if nonessential:
            reader.float32()
            reader.float32()
        mesh = MeshAttachment(name=name, path=path or name, color=color)
        linked_meshes.append((mesh, skin_name, slot_index, parent_name, inherit_deform))
        return mesh

    if kind == _AT_PATH:
        closed = reader.boolean()
        constant_speed = reader.boolean()
        vertex_count = reader.varint()
        vertices, bones = _read_vertices(reader, vertex_count)
        lengths = reader.floats(vertex_count // 3)
        if nonessential:
            reader.int32()
        return PathAttachment(name=name, world_vertices_length=vertex_count * 2,
                              vertices=vertices, bones=bones,
                              closed=closed, constant_speed=constant_speed, lengths=lengths)

    if kind == _AT_POINT:
        rotation = reader.float32()
        x = reader.float32()
        y = reader.float32()
        if nonessential:
            reader.int32()
        return PointAttachment(name=name, x=x, y=y, rotation=rotation)

    if kind == _AT_CLIPPING:
        end_slot = skel.slots[reader.varint()]
        vertex_count = reader.varint()
        vertices, bones = _read_vertices(reader, vertex_count)
        if nonessential:
            reader.int32()
        return ClippingAttachment(name=name, world_vertices_length=vertex_count * 2,
                                  vertices=vertices, bones=bones, end_slot=end_slot)

    raise SkeletonParseError(f"未知的 attachment 型別 {kind}")


# ---------------------------------------------------------------- 動畫

def _read_animation(reader: _Reader, skel: SkeletonData, name: str) -> Animation:
    animation = Animation(name=name)
    timelines = animation.timelines
    duration = 0.0

    # Slot timelines
    for _ in range(reader.varint()):
        slot_index = reader.varint()
        for _ in range(reader.varint()):
            timeline_type = reader.byte()
            frame_count = reader.varint()
            if timeline_type == _SLOT_ATTACHMENT:
                times: list[float] = []
                names: list[str | None] = []
                for _ in range(frame_count):
                    times.append(reader.float32())
                    names.append(reader.string_ref())
                timelines.append(AttachmentTimeline(slot_index, times, names))
                duration = max(duration, times[-1])
            elif timeline_type == _SLOT_COLOR:
                times, colors, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    colors.append(_rgba8888(reader.int32()))
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(ColorTimeline(slot_index, times, colors, curves))
                duration = max(duration, times[-1])
            elif timeline_type == _SLOT_TWO_COLOR:
                times, colors, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    colors.append(_rgba8888(reader.int32()))
                    reader.int32()  # dark color（預覽忽略）
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(ColorTimeline(slot_index, times, colors, curves))
                duration = max(duration, times[-1])
            else:
                raise SkeletonParseError(f"未知的 slot timeline 型別 {timeline_type}")

    # Bone timelines
    for _ in range(reader.varint()):
        bone_index = reader.varint()
        for _ in range(reader.varint()):
            timeline_type = reader.byte()
            frame_count = reader.varint()
            if timeline_type == _BONE_ROTATE:
                times, values, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    values.append(reader.float32())
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(RotateTimeline(bone_index, times, values, curves))
                duration = max(duration, times[-1])
            elif timeline_type in (_BONE_TRANSLATE, _BONE_SCALE, _BONE_SHEAR):
                times, xs, ys, curves = [], [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    xs.append(reader.float32())
                    ys.append(reader.float32())
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                cls = {_BONE_TRANSLATE: TranslateTimeline,
                       _BONE_SCALE: ScaleTimeline,
                       _BONE_SHEAR: ShearTimeline}[timeline_type]
                timelines.append(cls(bone_index, times, xs, ys, curves))
                duration = max(duration, times[-1])
            else:
                raise SkeletonParseError(f"未知的 bone timeline 型別 {timeline_type}")

    # IK timelines
    for _ in range(reader.varint()):
        index = reader.varint()
        frame_count = reader.varint()
        times, frames, curves = [], [], CurveSet(frame_count)
        for frame in range(frame_count):
            times.append(reader.float32())
            frames.append((reader.float32(), reader.float32(), reader.sbyte(),
                           reader.boolean(), reader.boolean()))
            if frame < frame_count - 1:
                _read_curve(reader, curves, frame)
        timelines.append(IkConstraintTimeline(index, times, frames, curves))
        duration = max(duration, times[-1])

    # Transform constraint timelines
    for _ in range(reader.varint()):
        index = reader.varint()
        frame_count = reader.varint()
        times, frames, curves = [], [], CurveSet(frame_count)
        for frame in range(frame_count):
            times.append(reader.float32())
            frames.append((reader.float32(), reader.float32(), reader.float32(), reader.float32()))
            if frame < frame_count - 1:
                _read_curve(reader, curves, frame)
        timelines.append(TransformConstraintTimeline(index, times, frames, curves))
        duration = max(duration, times[-1])

    # Path constraint timelines
    for _ in range(reader.varint()):
        index = reader.varint()
        for _ in range(reader.varint()):
            timeline_type = reader.byte()
            frame_count = reader.varint()
            if timeline_type in (_PATH_POSITION, _PATH_SPACING):
                times, values, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    values.append(reader.float32())
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(PathConstraintValueTimeline(index, timeline_type, times, values, curves))
                duration = max(duration, times[-1])
            elif timeline_type == _PATH_MIX:
                times, frames, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    frames.append((reader.float32(), reader.float32()))
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(PathConstraintMixTimeline(index, times, frames, curves))
                duration = max(duration, times[-1])
            else:
                raise SkeletonParseError(f"未知的 path timeline 型別 {timeline_type}")

    # Deform timelines
    for _ in range(reader.varint()):
        skin = skel.skins[reader.varint()]
        for _ in range(reader.varint()):
            slot_index = reader.varint()
            for _ in range(reader.varint()):
                attachment_name = reader.string_ref() or ""
                attachment = skin.get(slot_index, attachment_name)
                if not isinstance(attachment, VertexAttachment):
                    raise SkeletonParseError(f"deform timeline 找不到附件 {attachment_name}")
                weighted = attachment.bones is not None
                if weighted:
                    deform_length = (len(attachment.vertices) // 3) * 2
                else:
                    deform_length = len(attachment.vertices)

                frame_count = reader.varint()
                times, deforms, curves = [], [], CurveSet(frame_count)
                for frame in range(frame_count):
                    times.append(reader.float32())
                    end = reader.varint()
                    if end == 0:
                        if weighted:
                            deform = [0.0] * deform_length
                        else:
                            deform = list(attachment.vertices)
                    else:
                        deform = [0.0] * deform_length
                        start = reader.varint()
                        values = reader.floats(end)
                        deform[start : start + end] = values
                        if not weighted:
                            for v in range(deform_length):
                                deform[v] += attachment.vertices[v]
                    deforms.append(deform)
                    if frame < frame_count - 1:
                        _read_curve(reader, curves, frame)
                timelines.append(DeformTimeline(slot_index, attachment, times, deforms, curves))
                duration = max(duration, times[-1])

    # Draw order timeline
    draw_order_count = reader.varint()
    if draw_order_count > 0:
        slot_count = len(skel.slots)
        times, orders = [], []
        for _ in range(draw_order_count):
            times.append(reader.float32())
            offset_count = reader.varint()
            if offset_count == 0:
                orders.append(None)
                continue
            draw_order = [-1] * slot_count
            unchanged = [0] * (slot_count - offset_count)
            original_index = unchanged_index = 0
            for _ in range(offset_count):
                slot_index = reader.varint()
                while original_index != slot_index:
                    unchanged[unchanged_index] = original_index
                    unchanged_index += 1
                    original_index += 1
                draw_order[original_index + reader.varint()] = original_index
                original_index += 1
            while original_index < slot_count:
                unchanged[unchanged_index] = original_index
                unchanged_index += 1
                original_index += 1
            for i in range(slot_count - 1, -1, -1):
                if draw_order[i] == -1:
                    unchanged_index -= 1
                    draw_order[i] = unchanged[unchanged_index]
            orders.append(draw_order)
        timelines.append(DrawOrderTimeline(times, orders))
        duration = max(duration, times[-1])

    # Event timeline
    event_count = reader.varint()
    if event_count > 0:
        times = []
        for _ in range(event_count):
            times.append(reader.float32())
            event_data = skel.events[reader.varint()]
            reader.varint(optimize_positive=False)  # int value
            reader.float32()                        # float value
            if reader.boolean():                    # 覆寫字串
                reader.string()
            if event_data.audio_path is not None:
                reader.float32()  # volume
                reader.float32()  # balance
        timelines.append(EventTimeline(times))
        duration = max(duration, times[-1])

    animation.duration = duration
    return animation
