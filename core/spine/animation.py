"""
Spine 3.8 動畫 timeline

播放模型刻意簡化：每一影格先把骨架重設回 setup pose，再以 alpha=1 套用
單一動畫的所有 timeline——等價於 spine-runtimes 的
``skeleton.setToSetupPose(); animation.apply(...)``，不做動畫混合。
預覽用這樣就足夠，也大幅降低出錯空間。
"""
from __future__ import annotations

from bisect import bisect_right

from core.spine.skeleton_data import VertexAttachment

# ---------------------------------------------------------------- 曲線

CURVE_LINEAR = 0
CURVE_STEPPED = 1

_BEZIER_SAMPLES = 16


class CurveSet:
    """每段（frame i -> i+1）一條曲線：None=線性、"stepped"、或取樣後的貝茲點"""

    def __init__(self, frame_count: int) -> None:
        self.curves: list = [None] * max(0, frame_count - 1)

    def set_stepped(self, frame: int) -> None:
        self.curves[frame] = "stepped"

    def set_bezier(self, frame: int, cx1: float, cy1: float, cx2: float, cy2: float) -> None:
        # 取樣三次貝茲（時間軸 x -> 值比例 y），播放時線性內插
        points: list[tuple[float, float]] = []
        for i in range(1, _BEZIER_SAMPLES + 1):
            t = i / _BEZIER_SAMPLES
            u = 1.0 - t
            x = 3 * u * u * t * cx1 + 3 * u * t * t * cx2 + t * t * t
            y = 3 * u * u * t * cy1 + 3 * u * t * t * cy2 + t * t * t
            points.append((x, y))
        self.curves[frame] = points

    def percent(self, frame: int, ratio: float) -> float:
        curve = self.curves[frame] if frame < len(self.curves) else None
        if curve is None:
            return ratio
        if curve == "stepped":
            return 0.0
        prev_x = prev_y = 0.0
        for x, y in curve:
            if ratio <= x:
                if x == prev_x:
                    return y
                return prev_y + (y - prev_y) * (ratio - prev_x) / (x - prev_x)
            prev_x, prev_y = x, y
        return 1.0


def _find_frame(times: list[float], time: float) -> int:
    """回傳最後一個 time <= 指定時間的影格索引（至少 0）"""
    return max(0, bisect_right(times, time) - 1)


def _wrap_angle(degrees: float) -> float:
    degrees %= 360.0
    if degrees > 180.0:
        degrees -= 360.0
    elif degrees < -180.0:
        degrees += 360.0
    return degrees


# ---------------------------------------------------------------- 骨骼

class RotateTimeline:
    def __init__(self, bone_index: int, times: list[float], values: list[float], curves: CurveSet) -> None:
        self.bone_index = bone_index
        self.times = times
        self.values = values  # 相對 setup 的角度
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        bone = skeleton.bones[self.bone_index]
        if time >= self.times[-1]:
            bone.rotation = bone.data.rotation + self.values[-1]
            return
        frame = _find_frame(self.times, time)
        t0, t1 = self.times[frame], self.times[frame + 1]
        percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
        delta = _wrap_angle(self.values[frame + 1] - self.values[frame])
        bone.rotation = bone.data.rotation + self.values[frame] + delta * percent


class _XYTimeline:
    def __init__(self, bone_index: int, times: list[float], xs: list[float], ys: list[float], curves: CurveSet) -> None:
        self.bone_index = bone_index
        self.times = times
        self.xs = xs
        self.ys = ys
        self.curves = curves

    def _value(self, time: float) -> tuple[float, float] | None:
        if time < self.times[0]:
            return None
        if time >= self.times[-1]:
            return self.xs[-1], self.ys[-1]
        frame = _find_frame(self.times, time)
        t0, t1 = self.times[frame], self.times[frame + 1]
        percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
        x = self.xs[frame] + (self.xs[frame + 1] - self.xs[frame]) * percent
        y = self.ys[frame] + (self.ys[frame + 1] - self.ys[frame]) * percent
        return x, y


class TranslateTimeline(_XYTimeline):
    def apply(self, skeleton, time: float) -> None:
        value = self._value(time)
        if value is None:
            return
        bone = skeleton.bones[self.bone_index]
        bone.x = bone.data.x + value[0]
        bone.y = bone.data.y + value[1]


class ScaleTimeline(_XYTimeline):
    def apply(self, skeleton, time: float) -> None:
        value = self._value(time)
        if value is None:
            return
        bone = skeleton.bones[self.bone_index]
        bone.scale_x = bone.data.scale_x * value[0]
        bone.scale_y = bone.data.scale_y * value[1]


class ShearTimeline(_XYTimeline):
    def apply(self, skeleton, time: float) -> None:
        value = self._value(time)
        if value is None:
            return
        bone = skeleton.bones[self.bone_index]
        bone.shear_x = bone.data.shear_x + value[0]
        bone.shear_y = bone.data.shear_y + value[1]


# ---------------------------------------------------------------- Slot

class AttachmentTimeline:
    def __init__(self, slot_index: int, times: list[float], names: list[str | None]) -> None:
        self.slot_index = slot_index
        self.times = times
        self.names = names

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        name = self.names[_find_frame(self.times, time)]
        skeleton.slots[self.slot_index].set_attachment_by_name(name)


class ColorTimeline:
    def __init__(self, slot_index: int, times: list[float], colors: list[tuple], curves: CurveSet) -> None:
        self.slot_index = slot_index
        self.times = times
        self.colors = colors  # (r,g,b,a) 0..1
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        slot = skeleton.slots[self.slot_index]
        if time >= self.times[-1]:
            slot.color = self.colors[-1]
            return
        frame = _find_frame(self.times, time)
        t0, t1 = self.times[frame], self.times[frame + 1]
        percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
        c0, c1 = self.colors[frame], self.colors[frame + 1]
        slot.color = tuple(a + (b - a) * percent for a, b in zip(c0, c1))  # type: ignore[assignment]


class DeformTimeline:
    def __init__(self, slot_index: int, attachment: VertexAttachment,
                 times: list[float], deforms: list[list[float]], curves: CurveSet) -> None:
        self.slot_index = slot_index
        self.attachment = attachment
        self.times = times
        self.deforms = deforms
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        slot = skeleton.slots[self.slot_index]
        current = slot.attachment
        if not isinstance(current, VertexAttachment):
            return
        if current.deform_attachment is not self.attachment:
            return
        if time >= self.times[-1]:
            slot.deform = list(self.deforms[-1])
            return
        frame = _find_frame(self.times, time)
        t0, t1 = self.times[frame], self.times[frame + 1]
        percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
        d0, d1 = self.deforms[frame], self.deforms[frame + 1]
        slot.deform = [a + (b - a) * percent for a, b in zip(d0, d1)]


# ---------------------------------------------------------------- 約束

class IkConstraintTimeline:
    def __init__(self, index: int, times: list[float], frames: list[tuple], curves: CurveSet) -> None:
        self.index = index
        self.times = times
        self.frames = frames  # (mix, softness, bend, compress, stretch)
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        constraint = skeleton.ik_constraints[self.index]
        if time >= self.times[-1]:
            mix, softness, bend, compress, stretch = self.frames[-1]
        else:
            frame = _find_frame(self.times, time)
            t0, t1 = self.times[frame], self.times[frame + 1]
            percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
            m0, s0, bend, compress, stretch = self.frames[frame]
            m1, s1 = self.frames[frame + 1][0], self.frames[frame + 1][1]
            mix = m0 + (m1 - m0) * percent
            softness = s0 + (s1 - s0) * percent
        constraint.mix = mix
        constraint.softness = softness
        constraint.bend_direction = bend
        constraint.compress = compress
        constraint.stretch = stretch


class TransformConstraintTimeline:
    def __init__(self, index: int, times: list[float], frames: list[tuple], curves: CurveSet) -> None:
        self.index = index
        self.times = times
        self.frames = frames  # (rotate, translate, scale, shear)
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        constraint = skeleton.transform_constraints[self.index]
        if time >= self.times[-1]:
            values = self.frames[-1]
        else:
            frame = _find_frame(self.times, time)
            t0, t1 = self.times[frame], self.times[frame + 1]
            percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
            f0, f1 = self.frames[frame], self.frames[frame + 1]
            values = tuple(a + (b - a) * percent for a, b in zip(f0, f1))
        (constraint.rotate_mix, constraint.translate_mix,
         constraint.scale_mix, constraint.shear_mix) = values


class PathConstraintValueTimeline:
    """position(0) / spacing(1)"""

    def __init__(self, index: int, kind: int, times: list[float], values: list[float], curves: CurveSet) -> None:
        self.index = index
        self.kind = kind
        self.times = times
        self.values = values
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        constraint = skeleton.path_constraints[self.index]
        if time >= self.times[-1]:
            value = self.values[-1]
        else:
            frame = _find_frame(self.times, time)
            t0, t1 = self.times[frame], self.times[frame + 1]
            percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
            value = self.values[frame] + (self.values[frame + 1] - self.values[frame]) * percent
        if self.kind == 0:
            constraint.position = value
        else:
            constraint.spacing = value


class PathConstraintMixTimeline:
    def __init__(self, index: int, times: list[float], frames: list[tuple], curves: CurveSet) -> None:
        self.index = index
        self.times = times
        self.frames = frames  # (rotate_mix, translate_mix)
        self.curves = curves

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        constraint = skeleton.path_constraints[self.index]
        if time >= self.times[-1]:
            rotate, translate = self.frames[-1]
        else:
            frame = _find_frame(self.times, time)
            t0, t1 = self.times[frame], self.times[frame + 1]
            percent = self.curves.percent(frame, (time - t0) / (t1 - t0) if t1 > t0 else 1.0)
            f0, f1 = self.frames[frame], self.frames[frame + 1]
            rotate = f0[0] + (f1[0] - f0[0]) * percent
            translate = f0[1] + (f1[1] - f0[1]) * percent
        constraint.rotate_mix = rotate
        constraint.translate_mix = translate


# ---------------------------------------------------------------- 其他

class DrawOrderTimeline:
    def __init__(self, times: list[float], orders: list[list[int] | None]) -> None:
        self.times = times
        self.orders = orders  # None = setup 順序

    def apply(self, skeleton, time: float) -> None:
        if time < self.times[0]:
            return
        order = self.orders[_find_frame(self.times, time)]
        if order is None:
            skeleton.draw_order = list(skeleton.slots)
        else:
            skeleton.draw_order = [skeleton.slots[i] for i in order]


class EventTimeline:
    """事件只需要被解析（推進資料流），播放時不做事"""

    def __init__(self, times: list[float]) -> None:
        self.times = times

    def apply(self, skeleton, time: float) -> None:
        pass
