"""
Spine 3.8 runtime（骨骼世界變換、約束、頂點計算）

移植自 spine-runtimes 3.8 的 Bone / IkConstraint / TransformConstraint /
PathConstraint 參考實作。更新順序刻意簡化：先算所有骨骼的世界變換
（檔案保證父骨骼在前），再依 ``order`` 依序套用約束，每套用一個約束就
重算受影響骨骼的所有子孫——對絕大多數 rig 與官方 update cache 等價。

Path 約束數學較長，任何一步出錯就把該約束停用並記錄備註，
避免整個預覽崩潰。
"""
from __future__ import annotations

import math

from core.spine.skeleton_data import (
    TM_NO_ROTATION_OR_REFLECTION,
    TM_NO_SCALE,
    TM_NO_SCALE_OR_REFLECTION,
    TM_NORMAL,
    TM_ONLY_TRANSLATION,
    Attachment,
    PathAttachment,
    SkeletonData,
    VertexAttachment,
)

_DEG_RAD = math.pi / 180.0
_RAD_DEG = 180.0 / math.pi


class Bone:
    __slots__ = ("data", "skeleton", "parent", "children",
                 "x", "y", "rotation", "scale_x", "scale_y", "shear_x", "shear_y",
                 "a", "b", "c", "d", "world_x", "world_y")

    def __init__(self, data, skeleton, parent: "Bone | None") -> None:
        self.data = data
        self.skeleton = skeleton
        self.parent = parent
        self.children: list[Bone] = []
        if parent is not None:
            parent.children.append(self)
        self.a = self.d = 1.0
        self.b = self.c = 0.0
        self.world_x = self.world_y = 0.0
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        data = self.data
        self.x = data.x
        self.y = data.y
        self.rotation = data.rotation
        self.scale_x = data.scale_x
        self.scale_y = data.scale_y
        self.shear_x = data.shear_x
        self.shear_y = data.shear_y

    # -------------------------------------------------- 世界變換

    def update_world_transform(self) -> None:
        self.update_world_transform_with(
            self.x, self.y, self.rotation, self.scale_x, self.scale_y, self.shear_x, self.shear_y
        )

    def update_world_transform_with(self, x, y, rotation, scale_x, scale_y, shear_x, shear_y) -> None:
        parent = self.parent
        if parent is None:
            rx = (rotation + shear_x) * _DEG_RAD
            ry = (rotation + 90 + shear_y) * _DEG_RAD
            self.a = math.cos(rx) * scale_x
            self.b = math.cos(ry) * scale_y
            self.c = math.sin(rx) * scale_x
            self.d = math.sin(ry) * scale_y
            self.world_x = x
            self.world_y = y
            return

        pa, pb, pc, pd = parent.a, parent.b, parent.c, parent.d
        self.world_x = pa * x + pb * y + parent.world_x
        self.world_y = pc * x + pd * y + parent.world_y
        mode = self.data.transform_mode

        if mode == TM_NORMAL:
            rx = (rotation + shear_x) * _DEG_RAD
            ry = (rotation + 90 + shear_y) * _DEG_RAD
            la = math.cos(rx) * scale_x
            lb = math.cos(ry) * scale_y
            lc = math.sin(rx) * scale_x
            ld = math.sin(ry) * scale_y
            self.a = pa * la + pb * lc
            self.b = pa * lb + pb * ld
            self.c = pc * la + pd * lc
            self.d = pc * lb + pd * ld
            return

        if mode == TM_ONLY_TRANSLATION:
            rx = (rotation + shear_x) * _DEG_RAD
            ry = (rotation + 90 + shear_y) * _DEG_RAD
            self.a = math.cos(rx) * scale_x
            self.b = math.cos(ry) * scale_y
            self.c = math.sin(rx) * scale_x
            self.d = math.sin(ry) * scale_y
        elif mode == TM_NO_ROTATION_OR_REFLECTION:
            s = pa * pa + pc * pc
            if s > 0.0001:
                s = abs(pa * pd - pb * pc) / s
                pa /= self.skeleton.scale_x
                pc /= self.skeleton.scale_y
                pb = pc * s
                pd = pa * s
                prx = math.atan2(pc, pa) * _RAD_DEG
            else:
                pa = 0.0
                pc = 0.0
                prx = 90 - math.atan2(pd, pb) * _RAD_DEG
            rx = (rotation + shear_x - prx) * _DEG_RAD
            ry = (rotation + shear_y - prx + 90) * _DEG_RAD
            la = math.cos(rx) * scale_x
            lb = math.cos(ry) * scale_y
            lc = math.sin(rx) * scale_x
            ld = math.sin(ry) * scale_y
            self.a = pa * la - pb * lc
            self.b = pa * lb - pb * ld
            self.c = pc * la + pd * lc
            self.d = pc * lb + pd * ld
        elif mode in (TM_NO_SCALE, TM_NO_SCALE_OR_REFLECTION):
            r = rotation * _DEG_RAD
            cos_r, sin_r = math.cos(r), math.sin(r)
            za = (pa * cos_r + pb * sin_r) / self.skeleton.scale_x
            zc = (pc * cos_r + pd * sin_r) / self.skeleton.scale_y
            s = math.sqrt(za * za + zc * zc)
            if s > 0.00001:
                s = 1 / s
            za *= s
            zc *= s
            s = math.sqrt(za * za + zc * zc)
            if mode == TM_NO_SCALE and (pa * pd - pb * pc < 0) != (
                (self.skeleton.scale_x < 0) != (self.skeleton.scale_y < 0)
            ):
                s = -s
            r = math.pi / 2 + math.atan2(zc, za)
            zb = math.cos(r) * s
            zd = math.sin(r) * s
            rx = shear_x * _DEG_RAD
            ry = (90 + shear_y) * _DEG_RAD
            la = math.cos(rx) * scale_x
            lb = math.cos(ry) * scale_y
            lc = math.sin(rx) * scale_x
            ld = math.sin(ry) * scale_y
            self.a = za * la + zb * lc
            self.b = za * lb + zb * ld
            self.c = zc * la + zd * lc
            self.d = zc * lb + zd * ld
        self.a *= self.skeleton.scale_x
        self.b *= self.skeleton.scale_x
        self.c *= self.skeleton.scale_y
        self.d *= self.skeleton.scale_y

    # -------------------------------------------------- 工具

    @property
    def world_rotation_x(self) -> float:
        return math.atan2(self.c, self.a) * _RAD_DEG

    @property
    def world_scale_x(self) -> float:
        return math.sqrt(self.a * self.a + self.c * self.c)

    @property
    def world_scale_y(self) -> float:
        return math.sqrt(self.b * self.b + self.d * self.d)

    def world_to_local_rotation(self, world_rotation: float) -> float:
        r = world_rotation * _DEG_RAD
        sin_r, cos_r = math.sin(r), math.cos(r)
        return math.atan2(self.a * sin_r - self.c * cos_r, self.d * cos_r - self.b * sin_r) * _RAD_DEG + self.rotation - self.shear_x

    def update_descendants(self) -> None:
        """約束改動此骨骼後，重算所有子孫的世界變換"""
        for child in self.children:
            child.update_world_transform()
            child.update_descendants()


class Slot:
    __slots__ = ("data", "bone", "skeleton", "color", "attachment", "deform")

    def __init__(self, data, bone: Bone, skeleton) -> None:
        self.data = data
        self.bone = bone
        self.skeleton = skeleton
        self.color = data.color
        self.attachment: Attachment | None = None
        self.deform: list[float] = []
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        self.color = self.data.color
        self.deform = []
        self.set_attachment_by_name(self.data.attachment_name)

    def set_attachment_by_name(self, name: str | None) -> None:
        new = self.skeleton.get_attachment(self.data.index, name) if name else None
        if new is not self.attachment:
            self.attachment = new
            self.deform = []


class Skeleton:
    def __init__(self, data: SkeletonData) -> None:
        self.data = data
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.skin = data.default_skin
        self.notes: list[str] = []
        self._disabled_constraints: set[int] = set()

        self.bones: list[Bone] = []
        bone_map: dict[int, Bone] = {}
        for bone_data in data.bones:
            parent = bone_map[bone_data.parent.index] if bone_data.parent else None
            bone = Bone(bone_data, self, parent)
            bone_map[bone_data.index] = bone
            self.bones.append(bone)

        # path 約束的 target 是 slot，slots 必須先建好
        self.slots: list[Slot] = [
            Slot(slot_data, self.bones[slot_data.bone.index], self) for slot_data in data.slots
        ]
        self.draw_order: list[Slot] = list(self.slots)

        self.ik_constraints = [_IkConstraint(d, self) for d in data.ik_constraints]
        self.transform_constraints = [_TransformConstraint(d, self) for d in data.transform_constraints]
        self.path_constraints = [_PathConstraint(d, self) for d in data.path_constraints]

        # 約束依 order 排序
        self._ordered_constraints = sorted(
            [(c.data.order, 0, c) for c in self.ik_constraints]
            + [(c.data.order, 1, c) for c in self.transform_constraints]
            + [(c.data.order, 2, c) for c in self.path_constraints],
            key=lambda item: item[0],
        )

    # -------------------------------------------------- 附件

    def set_skin(self, name: str | None) -> None:
        if name is None:
            self.skin = self.data.default_skin
        else:
            self.skin = next((s for s in self.data.skins if s.name == name), self.data.default_skin)

    def get_attachment(self, slot_index: int, name: str | None) -> Attachment | None:
        if not name:
            return None
        if self.skin is not None:
            attachment = self.skin.get(slot_index, name)
            if attachment is not None:
                return attachment
        if self.data.default_skin is not None and self.data.default_skin is not self.skin:
            return self.data.default_skin.get(slot_index, name)
        return None

    # -------------------------------------------------- 姿勢

    def set_to_setup_pose(self) -> None:
        for bone in self.bones:
            bone.set_to_setup_pose()
        for constraint in self.ik_constraints + self.transform_constraints + self.path_constraints:
            constraint.set_to_setup_pose()
        for slot in self.slots:
            slot.set_to_setup_pose()
        self.draw_order = list(self.slots)

    def update_world_transform(self) -> None:
        for bone in self.bones:
            bone.update_world_transform()
        for index, (_, _, constraint) in enumerate(self._ordered_constraints):
            if index in self._disabled_constraints:
                continue
            try:
                constraint.apply()
            except Exception as exc:  # noqa: BLE001 - 約束失敗不應毀掉預覽
                self._disabled_constraints.add(index)
                self.notes.append(f"約束 {constraint.data.name} 套用失敗，已停用（{exc}）")

    # -------------------------------------------------- 頂點

    def compute_world_vertices(self, slot: Slot, attachment: VertexAttachment) -> list[float]:
        """回傳世界座標 [x0,y0,x1,y1,...]，已套用 deform 與骨骼權重。"""
        deform = slot.deform
        if attachment.bones is None:
            vertices = deform if len(deform) == len(attachment.vertices) else attachment.vertices
            bone = slot.bone
            a, b, c, d = bone.a, bone.b, bone.c, bone.d
            wx, wy = bone.world_x, bone.world_y
            out = [0.0] * len(vertices)
            for i in range(0, len(vertices), 2):
                x, y = vertices[i], vertices[i + 1]
                out[i] = a * x + b * y + wx
                out[i + 1] = c * x + d * y + wy
            return out

        # 加權
        bones = attachment.bones
        weights = attachment.vertices  # x,y,w 三元組展平
        has_deform = len(deform) > 0
        out: list[float] = []
        v = 0  # weights 索引（三元組）
        f = 0  # deform 索引（成對）
        i = 0
        skeleton_bones = self.bones
        while i < len(bones):
            bone_count = bones[i]
            i += 1
            wx = wy = 0.0
            for _ in range(bone_count):
                bone = skeleton_bones[bones[i]]
                i += 1
                x = weights[v]
                y = weights[v + 1]
                weight = weights[v + 2]
                v += 3
                if has_deform:
                    x += deform[f]
                    y += deform[f + 1]
                    f += 2
                wx += (bone.a * x + bone.b * y + bone.world_x) * weight
                wy += (bone.c * x + bone.d * y + bone.world_y) * weight
            out.append(wx)
            out.append(wy)
        return out


# ---------------------------------------------------------------- IK

class _IkConstraint:
    def __init__(self, data, skeleton: Skeleton) -> None:
        self.data = data
        self.skeleton = skeleton
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.bones[data.target.index]
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        data = self.data
        self.mix = data.mix
        self.softness = data.softness
        self.bend_direction = data.bend_direction
        self.compress = data.compress
        self.stretch = data.stretch

    def apply(self) -> None:
        if self.mix == 0:
            return
        target = self.target
        if len(self.bones) == 1:
            self._apply1(self.bones[0], target.world_x, target.world_y,
                         self.compress, self.stretch, self.data.uniform, self.mix)
            self.bones[0].update_descendants()
        elif len(self.bones) == 2:
            self._apply2(self.bones[0], self.bones[1], target.world_x, target.world_y,
                         self.bend_direction, self.stretch, self.softness, self.mix)
            self.bones[0].update_descendants()

    @staticmethod
    def _apply1(bone: Bone, target_x, target_y, compress, stretch, uniform, alpha) -> None:
        parent = bone.parent
        if parent is None:
            pa, pb, pc, pd = 1.0, 0.0, 0.0, 1.0
            px, py = 0.0, 0.0
        else:
            pa, pb, pc, pd = parent.a, parent.b, parent.c, parent.d
            px, py = parent.world_x, parent.world_y
        rotation_ik = -bone.shear_x - bone.rotation
        mode = bone.data.transform_mode
        if mode == TM_ONLY_TRANSLATION:
            tx = target_x - bone.world_x
            ty = target_y - bone.world_y
        else:
            if mode == TM_NO_ROTATION_OR_REFLECTION:
                s = abs(pa * pd - pb * pc) / max(pa * pa + pc * pc, 0.0001)
                sa = pa / bone.skeleton.scale_x
                sc = pc / bone.skeleton.scale_y
                pb = -sc * s * bone.skeleton.scale_x
                pd = sa * s * bone.skeleton.scale_y
                rotation_ik += math.atan2(sc, sa) * _RAD_DEG
            x = target_x - px
            y = target_y - py
            det = pa * pd - pb * pc
            if abs(det) <= 0.0001:
                tx = ty = 0.0
            else:
                tx = (x * pd - y * pb) / det - bone.x
                ty = (y * pa - x * pc) / det - bone.y
        rotation_ik += math.atan2(ty, tx) * _RAD_DEG
        if bone.scale_x < 0:
            rotation_ik += 180
        if rotation_ik > 180:
            rotation_ik -= 360
        elif rotation_ik < -180:
            rotation_ik += 360
        scale_x = bone.scale_x
        scale_y = bone.scale_y
        if compress or stretch:
            if mode in (TM_NO_SCALE, TM_NO_SCALE_OR_REFLECTION):
                tx = target_x - bone.world_x
                ty = target_y - bone.world_y
            length = bone.data.length * scale_x
            distance = math.sqrt(tx * tx + ty * ty)
            if ((compress and distance < length) or (stretch and distance > length)) and length > 0.0001:
                s = (distance / length - 1) * alpha + 1
                scale_x *= s
                if uniform:
                    scale_y *= s
        bone.update_world_transform_with(bone.x, bone.y, bone.rotation + rotation_ik * alpha,
                                         scale_x, scale_y, bone.shear_x, bone.shear_y)

    @staticmethod
    def _apply2(parent: Bone, child: Bone, target_x, target_y, bend_dir, stretch, softness, alpha) -> None:
        # 移植 spine 3.8 IkConstraint.apply(parent, child, ...)
        if alpha == 0:
            child.update_world_transform()
            return
        px, py = parent.x, parent.y
        psx, psy = parent.scale_x, parent.scale_y
        csx = child.scale_x
        if psx < 0:
            psx = -psx
            os1 = 180
            s2 = -1
        else:
            os1 = 0
            s2 = 1
        if psy < 0:
            psy = -psy
            s2 = -s2
        if csx < 0:
            csx = -csx
            os2 = 180
        else:
            os2 = 0
        cx = child.x
        u = abs(psx - psy) <= 0.0001
        if not u:
            cy = 0.0
            cwx = parent.a * cx + parent.world_x
            cwy = parent.c * cx + parent.world_y
        else:
            cy = child.y
            cwx = parent.a * cx + parent.b * cy + parent.world_x
            cwy = parent.c * cx + parent.d * cy + parent.world_y
        pp = parent.parent
        if pp is None:
            pa, pb, pc, pd = 1.0, 0.0, 0.0, 1.0
            ppx, ppy = 0.0, 0.0
        else:
            pa, pb, pc, pd = pp.a, pp.b, pp.c, pp.d
            ppx, ppy = pp.world_x, pp.world_y
        det = pa * pd - pb * pc
        if abs(det) <= 0.0001:
            det = 0.0001
        x = target_x - ppx
        y = target_y - ppy
        tx = (x * pd - y * pb) / det - px
        ty = (y * pa - x * pc) / det - py
        dd = tx * tx + ty * ty
        x = cwx - ppx
        y = cwy - ppy
        dx = (x * pd - y * pb) / det - px
        dy = (y * pa - x * pc) / det - py
        l1 = math.sqrt(dx * dx + dy * dy)
        l2 = child.data.length * csx
        if softness != 0:
            softness *= psx * (csx + 1) / 2
            td = math.sqrt(dd)
            sd = td - l1 - l2 * psx + softness
            if sd > 0:
                p = min(1.0, sd / (softness * 2)) - 1
                p = (sd - softness * (1 - p * p)) / td if td != 0 else 0
                tx -= p * tx
                ty -= p * ty
                dd = tx * tx + ty * ty
        parent_scale_x = parent.scale_x
        if u:
            l2 *= psx
            cos_v = (dd - l1 * l1 - l2 * l2) / (2 * l1 * l2) if l1 * l2 != 0 else 0.0
            if cos_v < -1:
                cos_v = -1
            elif cos_v > 1:
                cos_v = 1
                if stretch and l1 + l2 > 0.0001:
                    parent_scale_x *= (math.sqrt(dd) / (l1 + l2) - 1) * alpha + 1
            a2 = math.acos(cos_v) * bend_dir
            x = l1 + l2 * cos_v
            y = l2 * math.sin(a2)
            a1 = math.atan2(ty * x - tx * y, tx * x + ty * y)
        else:
            a_ = psx * l2
            b_ = psy * l2
            aa = a_ * a_
            bb = b_ * b_
            ta = math.atan2(ty, tx)
            c = bb * l1 * l1 + aa * dd - aa * bb
            c1 = -2 * bb * l1
            c2 = bb - aa
            d_ = c1 * c1 - 4 * c2 * c
            solved = False
            if d_ >= 0:
                q = math.sqrt(d_)
                if c1 < 0:
                    q = -q
                q = -(c1 + q) / 2
                r0 = q / c2 if c2 != 0 else 0.0
                r1 = c / q if q != 0 else 0.0
                r = r0 if abs(r0) < abs(r1) else r1
                if r * r <= dd:
                    y = math.sqrt(dd - r * r) * bend_dir
                    a1 = ta - math.atan2(y, r)
                    a2 = math.atan2(y / psy, (r - l1) / psx)
                    solved = True
            if not solved:
                # 目標不可達：取橢圓上最近/最遠點（對應 spine 參考實作的 fallback）
                min_angle = math.pi
                min_x = l1 - a_
                min_dist = min_x * min_x
                min_y = 0.0
                max_angle = 0.0
                max_x = l1 + a_
                max_dist = max_x * max_x
                max_y = 0.0
                c0 = -a_ * l1 / (aa - bb) if aa != bb else 2.0
                if -1 <= c0 <= 1:
                    c0 = math.acos(c0)
                    x = a_ * math.cos(c0) + l1
                    y = b_ * math.sin(c0)
                    d2 = x * x + y * y
                    if d2 < min_dist:
                        min_angle, min_dist, min_x, min_y = c0, d2, x, y
                    if d2 > max_dist:
                        max_angle, max_dist, max_x, max_y = c0, d2, x, y
                if dd <= (min_dist + max_dist) / 2:
                    a1 = ta - math.atan2(min_y * bend_dir, min_x)
                    a2 = min_angle * bend_dir
                else:
                    a1 = ta - math.atan2(max_y * bend_dir, max_x)
                    a2 = max_angle * bend_dir
        os_v = math.atan2(cy, cx) * s2
        rotation = parent.rotation
        a1 = (a1 - os_v) * _RAD_DEG + os1 - rotation
        if a1 > 180:
            a1 -= 360
        elif a1 < -180:
            a1 += 360
        parent.update_world_transform_with(px, py, rotation + a1 * alpha,
                                           parent_scale_x, parent.scale_y, 0, 0)
        rotation = child.rotation
        a2 = ((a2 + os_v) * _RAD_DEG - child.shear_x) * s2 + os2 - rotation
        if a2 > 180:
            a2 -= 360
        elif a2 < -180:
            a2 += 360
        child.update_world_transform_with(cx, cy, rotation + a2 * alpha,
                                          child.scale_x, child.scale_y, child.shear_x, child.shear_y)


# ---------------------------------------------------------------- Transform 約束

class _TransformConstraint:
    def __init__(self, data, skeleton: Skeleton) -> None:
        self.data = data
        self.skeleton = skeleton
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.bones[data.target.index]
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        data = self.data
        self.rotate_mix = data.rotate_mix
        self.translate_mix = data.translate_mix
        self.scale_mix = data.scale_mix
        self.shear_mix = data.shear_mix

    def apply(self) -> None:
        if self.rotate_mix == 0 and self.translate_mix == 0 and self.scale_mix == 0 and self.shear_mix == 0:
            return
        if self.data.local or self.data.relative:
            # 本工具的素材沒用到 local/relative 模式；為了預覽穩定性採用絕對世界模式的近似
            pass
        self._apply_absolute_world()
        for bone in self.bones:
            bone.update_descendants()

    def _apply_absolute_world(self) -> None:
        data = self.data
        target = self.target
        ta, tb, tc, td = target.a, target.b, target.c, target.d
        deg_rad_reflect = _DEG_RAD if ta * td - tb * tc > 0 else -_DEG_RAD
        offset_rotation = data.offset_rotation * deg_rad_reflect
        offset_shear_y = data.offset_shear_y * deg_rad_reflect

        for bone in self.bones:
            if self.rotate_mix != 0:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                r = math.atan2(tc, ta) - math.atan2(c, a) + offset_rotation
                if r > math.pi:
                    r -= math.pi * 2
                elif r < -math.pi:
                    r += math.pi * 2
                r *= self.rotate_mix
                cos_r, sin_r = math.cos(r), math.sin(r)
                bone.a = cos_r * a - sin_r * c
                bone.b = cos_r * b - sin_r * d
                bone.c = sin_r * a + cos_r * c
                bone.d = sin_r * b + cos_r * d

            if self.translate_mix != 0:
                tx = ta * data.offset_x + tb * data.offset_y + target.world_x
                ty = tc * data.offset_x + td * data.offset_y + target.world_y
                bone.world_x += (tx - bone.world_x) * self.translate_mix
                bone.world_y += (ty - bone.world_y) * self.translate_mix

            if self.scale_mix > 0:
                s = math.sqrt(bone.a * bone.a + bone.c * bone.c)
                if s > 0.00001:
                    ts = math.sqrt(ta * ta + tc * tc)
                    s = (s + (ts - s + data.offset_scale_x) * self.scale_mix) / s
                bone.a *= s
                bone.c *= s
                s = math.sqrt(bone.b * bone.b + bone.d * bone.d)
                if s > 0.00001:
                    ts = math.sqrt(tb * tb + td * td)
                    s = (s + (ts - s + data.offset_scale_y) * self.scale_mix) / s
                bone.b *= s
                bone.d *= s

            if self.shear_mix > 0:
                b, d = bone.b, bone.d
                by = math.atan2(d, b)
                r = math.atan2(td, tb) - math.atan2(tc, ta) - (by - math.atan2(bone.c, bone.a))
                if r > math.pi:
                    r -= math.pi * 2
                elif r < -math.pi:
                    r += math.pi * 2
                r = by + (r + offset_shear_y) * self.shear_mix
                s = math.sqrt(b * b + d * d)
                bone.b = math.cos(r) * s
                bone.d = math.sin(r) * s


# ---------------------------------------------------------------- Path 約束

# PositionMode
_PM_FIXED = 0
# SpacingMode
_SM_LENGTH = 0
_SM_FIXED = 1
_SM_PERCENT = 2
# RotateMode
_RM_TANGENT = 0
_RM_CHAIN = 1
_RM_CHAIN_SCALE = 2

_NONE = -1
_BEFORE = -2
_AFTER = -3


class _PathConstraint:
    def __init__(self, data, skeleton: Skeleton) -> None:
        self.data = data
        self.skeleton = skeleton
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.slots[data.target.index]
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        data = self.data
        self.position = data.position
        self.spacing = data.spacing
        self.rotate_mix = data.rotate_mix
        self.translate_mix = data.translate_mix

    def apply(self) -> None:
        if self.rotate_mix == 0 and self.translate_mix == 0:
            return
        attachment = self.target.attachment
        if not isinstance(attachment, PathAttachment):
            return
        data = self.data
        bones = self.bones
        if not bones:
            return
        spacing_mode = data.spacing_mode
        length_spacing = spacing_mode == _SM_LENGTH
        rotate_mode = data.rotate_mode
        tangents = rotate_mode == _RM_TANGENT
        scale = rotate_mode == _RM_CHAIN_SCALE
        bone_count = len(bones)
        spaces_count = bone_count if tangents else bone_count + 1
        spaces = [0.0] * spaces_count
        lengths = [0.0] * bone_count if scale else None
        spacing = self.spacing
        if spacing_mode == _SM_PERCENT:
            if scale and lengths is not None:
                for i in range(bone_count):
                    bone = bones[i]
                    set_up_length = bone.data.length
                    x = set_up_length * bone.a
                    y = set_up_length * bone.c
                    lengths[i] = math.sqrt(x * x + y * y)
            for i in range(1, spaces_count):
                spaces[i] = spacing
        else:
            for i in range(spaces_count - 1):
                bone = bones[i]
                set_up_length = bone.data.length
                if set_up_length < 0.0001:
                    if lengths is not None:
                        lengths[i] = 0.0
                    spaces[i + 1] = 0.0
                    continue
                x = set_up_length * bone.a
                y = set_up_length * bone.c
                length = math.sqrt(x * x + y * y)
                if lengths is not None:
                    lengths[i] = length
                if length_spacing:
                    spaces[i + 1] = (set_up_length + spacing) * length / set_up_length
                else:  # fixed
                    spaces[i + 1] = spacing
            if scale and lengths is not None and spaces_count - 1 < bone_count:
                bone = bones[bone_count - 1]
                set_up_length = bone.data.length
                x = set_up_length * bone.a
                y = set_up_length * bone.c
                lengths[bone_count - 1] = math.sqrt(x * x + y * y)

        positions = self._compute_world_positions(attachment, spaces_count, spaces, tangents,
                                                  data.position_mode == _PM_FIXED,
                                                  spacing_mode == _SM_PERCENT)
        bone_x = positions[0]
        bone_y = positions[1]
        offset_rotation = data.offset_rotation
        tip = False
        if offset_rotation == 0:
            tip = rotate_mode == _RM_CHAIN
        else:
            tip = False
            p_bone = self.target.bone
            offset_rotation *= _DEG_RAD if p_bone.a * p_bone.d - p_bone.b * p_bone.c > 0 else -_DEG_RAD

        for i in range(bone_count):
            bone = bones[i]
            bone.world_x += (bone_x - bone.world_x) * self.translate_mix
            bone.world_y += (bone_y - bone.world_y) * self.translate_mix
            p = i * 3 + 3
            x = positions[p]
            y = positions[p + 1]
            dx = x - bone_x
            dy = y - bone_y
            if scale and lengths is not None:
                length = lengths[i]
                if length >= 0.0001:
                    s = (math.sqrt(dx * dx + dy * dy) / length - 1) * self.rotate_mix + 1
                    bone.a *= s
                    bone.c *= s
            bone_x = x
            bone_y = y
            if self.rotate_mix > 0:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                if tangents:
                    r = positions[p - 1]
                elif spaces[i + 1] < 0.0001:
                    r = positions[p + 2]
                else:
                    r = math.atan2(dy, dx)
                r -= math.atan2(c, a)
                if tip:
                    cos_r = math.cos(r)
                    sin_r = math.sin(r)
                    length = bone.data.length
                    bone_x += (length * (cos_r * a - sin_r * c) - dx) * self.rotate_mix
                    bone_y += (length * (sin_r * a + cos_r * c) - dy) * self.rotate_mix
                else:
                    r += offset_rotation
                if r > math.pi:
                    r -= math.pi * 2
                elif r < -math.pi:
                    r += math.pi * 2
                r *= self.rotate_mix
                cos_r = math.cos(r)
                sin_r = math.sin(r)
                bone.a = cos_r * a - sin_r * c
                bone.b = cos_r * b - sin_r * d
                bone.c = sin_r * a + cos_r * c
                bone.d = sin_r * b + cos_r * d
            bone.update_descendants()

    def _compute_world_positions(self, path: PathAttachment, spaces_count: int, spaces: list[float],
                                 tangents: bool, percent_position: bool, percent_spacing: bool) -> list[float]:
        """統一走細分取樣路線；非等速路徑以等速近似，預覽視覺差異可忽略。"""
        world = self.skeleton.compute_world_vertices(self.target, path)
        return self._constant_speed_positions(world, path, spaces_count, spaces, tangents,
                                              self.position, path.closed,
                                              path.world_vertices_length)

    def _constant_speed_positions(self, world: list[float], path: PathAttachment,
                                  spaces_count: int, spaces: list[float], tangents: bool,
                                  position: float, closed: bool, vertices_length: int) -> list[float]:
        """以貝茲曲線細分計算路徑上每個間距點的位置與切線角。"""
        out = [0.0] * (spaces_count * 3 + 2)
        if closed:
            world = world + world[0:2]
        # 曲線群：每 6 個 float 一段三次貝茲（spine path 頂點格式 handle-anchor-handle）
        # world 佈局: [h_out0, a0? ...] spine 的 path 頂點是 (cp1, anchor, cp2) 三點一組
        # 依 spine 實作：曲線 i 由 anchor_i、cp2_i、cp1_{i+1}、anchor_{i+1} 構成
        point_count = len(world) // 2
        triples = point_count // 3
        segments = []
        for i in range(triples if closed else triples - 1):
            a = i * 3 + 1
            n = ((i + 1) % triples) * 3 + 1
            ax, ay = world[a * 2], world[a * 2 + 1]
            cox, coy = world[(a + 1) * 2], world[(a + 1) * 2 + 1]
            nix, niy = world[(n - 1) * 2], world[(n - 1) * 2 + 1]
            nx, ny = world[n * 2], world[n * 2 + 1]
            segments.append((ax, ay, cox, coy, nix, niy, nx, ny))
        if not segments:
            return out

        # 細分每段並累積長度
        samples_per_segment = 10
        points: list[tuple[float, float]] = []
        for ax, ay, cox, coy, nix, niy, nx, ny in segments:
            for s in range(samples_per_segment):
                t = s / samples_per_segment
                u = 1 - t
                x = u*u*u*ax + 3*u*u*t*cox + 3*u*t*t*nix + t*t*t*nx
                y = u*u*u*ay + 3*u*u*t*coy + 3*u*t*t*niy + t*t*t*ny
                points.append((x, y))
        last = segments[-1]
        points.append((last[6], last[7]))

        cumulative = [0.0]
        for i in range(1, len(points)):
            dx = points[i][0] - points[i - 1][0]
            dy = points[i][1] - points[i - 1][1]
            cumulative.append(cumulative[-1] + math.sqrt(dx * dx + dy * dy))
        path_length = cumulative[-1]
        if path_length <= 0:
            return out

        if self.data.position_mode != _PM_FIXED:
            position *= path_length
        if self.data.spacing_mode == _SM_PERCENT:
            spaces = [s * path_length for s in spaces]

        def locate(distance: float) -> tuple[float, float, float]:
            if closed:
                distance %= path_length
                if distance < 0:
                    distance += path_length
            else:
                distance = max(0.0, min(distance, path_length))
            # 二分搜尋
            lo, hi = 0, len(cumulative) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if cumulative[mid] < distance:
                    lo = mid + 1
                else:
                    hi = mid
            i = max(1, lo)
            seg_len = cumulative[i] - cumulative[i - 1]
            t = (distance - cumulative[i - 1]) / seg_len if seg_len > 0 else 0.0
            x = points[i - 1][0] + (points[i][0] - points[i - 1][0]) * t
            y = points[i - 1][1] + (points[i][1] - points[i - 1][1]) * t
            angle = math.atan2(points[i][1] - points[i - 1][1], points[i][0] - points[i - 1][0])
            return x, y, angle

        distance = position
        for i in range(spaces_count):
            distance += spaces[i]
            x, y, angle = locate(distance)
            out[i * 3] = x
            out[i * 3 + 1] = y
            out[i * 3 + 2] = angle
        return out
