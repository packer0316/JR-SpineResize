"""
Spine 3.8 骨架資料模型（唯讀）

欄位對應 spine-runtimes 3.8 的 SkeletonData 結構。
本工具只用它來「播放預覽」，永遠不會把資料寫回 .skel。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# transform 繼承模式（TransformMode）
TM_NORMAL = 0
TM_ONLY_TRANSLATION = 1
TM_NO_ROTATION_OR_REFLECTION = 2
TM_NO_SCALE = 3
TM_NO_SCALE_OR_REFLECTION = 4

# blend mode
BLEND_NORMAL = 0
BLEND_ADDITIVE = 1
BLEND_MULTIPLY = 2
BLEND_SCREEN = 3


@dataclass
class BoneData:
    index: int
    name: str
    parent: "BoneData | None" = None
    rotation: float = 0.0
    x: float = 0.0
    y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    shear_x: float = 0.0
    shear_y: float = 0.0
    length: float = 0.0
    transform_mode: int = TM_NORMAL
    skin_required: bool = False


@dataclass
class SlotData:
    index: int
    name: str
    bone: BoneData = None  # type: ignore[assignment]
    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    dark_color: tuple[float, float, float] | None = None
    attachment_name: str | None = None
    blend_mode: int = BLEND_NORMAL


@dataclass
class IkConstraintData:
    name: str
    order: int = 0
    skin_required: bool = False
    bones: list[BoneData] = field(default_factory=list)
    target: BoneData = None  # type: ignore[assignment]
    mix: float = 1.0
    softness: float = 0.0
    bend_direction: int = 1
    compress: bool = False
    stretch: bool = False
    uniform: bool = False


@dataclass
class TransformConstraintData:
    name: str
    order: int = 0
    skin_required: bool = False
    bones: list[BoneData] = field(default_factory=list)
    target: BoneData = None  # type: ignore[assignment]
    local: bool = False
    relative: bool = False
    offset_rotation: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_scale_x: float = 0.0
    offset_scale_y: float = 0.0
    offset_shear_y: float = 0.0
    rotate_mix: float = 1.0
    translate_mix: float = 1.0
    scale_mix: float = 1.0
    shear_mix: float = 1.0


# PositionMode: fixed=0, percent=1
# SpacingMode: length=0, fixed=1, percent=2
# RotateMode: tangent=0, chain=1, chainScale=2
@dataclass
class PathConstraintData:
    name: str
    order: int = 0
    skin_required: bool = False
    bones: list[BoneData] = field(default_factory=list)
    target: SlotData = None  # type: ignore[assignment]
    position_mode: int = 1
    spacing_mode: int = 0
    rotate_mode: int = 1
    offset_rotation: float = 0.0
    position: float = 0.0
    spacing: float = 0.0
    rotate_mix: float = 1.0
    translate_mix: float = 1.0


# ---------------------------------------------------------------- 附件


@dataclass
class Attachment:
    name: str


@dataclass
class RegionAttachment(Attachment):
    path: str = ""
    rotation: float = 0.0
    x: float = 0.0
    y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    width: float = 0.0
    height: float = 0.0
    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)


@dataclass
class VertexAttachment(Attachment):
    """有頂點的附件共同基底（mesh / boundingbox / path / clipping）"""

    world_vertices_length: int = 0
    vertices: list[float] = field(default_factory=list)      # 非加權：x,y,...
    bones: list[int] | None = None                           # 加權：展平的骨骼索引串
    # 加權時 vertices 為 x,y,weight 三元組展平（依 bones 排列）
    deform_attachment: "VertexAttachment | None" = None      # deform timeline 的目標

    def __post_init__(self) -> None:
        if self.deform_attachment is None:
            self.deform_attachment = self


@dataclass
class MeshAttachment(VertexAttachment):
    path: str = ""
    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    uvs: list[float] = field(default_factory=list)
    triangles: list[int] = field(default_factory=list)
    hull_length: int = 0
    parent_mesh: "MeshAttachment | None" = None


@dataclass
class BoundingBoxAttachment(VertexAttachment):
    pass


@dataclass
class PathAttachment(VertexAttachment):
    closed: bool = False
    constant_speed: bool = True
    lengths: list[float] = field(default_factory=list)


@dataclass
class PointAttachment(Attachment):
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0


@dataclass
class ClippingAttachment(VertexAttachment):
    end_slot: SlotData | None = None


@dataclass
class Skin:
    name: str
    # (slot_index, attachment_name) -> Attachment
    attachments: dict[tuple[int, str], Attachment] = field(default_factory=dict)

    def get(self, slot_index: int, name: str) -> Attachment | None:
        return self.attachments.get((slot_index, name))


@dataclass
class EventData:
    name: str
    int_value: int = 0
    float_value: float = 0.0
    string_value: str | None = None
    audio_path: str | None = None


@dataclass
class Animation:
    name: str
    timelines: list = field(default_factory=list)
    duration: float = 0.0

    def apply(self, skeleton, time: float) -> None:
        for timeline in self.timelines:
            timeline.apply(skeleton, time)


@dataclass
class SkeletonData:
    version: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    bones: list[BoneData] = field(default_factory=list)
    slots: list[SlotData] = field(default_factory=list)
    ik_constraints: list[IkConstraintData] = field(default_factory=list)
    transform_constraints: list[TransformConstraintData] = field(default_factory=list)
    path_constraints: list[PathConstraintData] = field(default_factory=list)
    default_skin: Skin | None = None
    skins: list[Skin] = field(default_factory=list)
    events: list[EventData] = field(default_factory=list)
    animations: list[Animation] = field(default_factory=list)

    def find_animation(self, name: str) -> Animation | None:
        for animation in self.animations:
            if animation.name == name:
                return animation
        return None
