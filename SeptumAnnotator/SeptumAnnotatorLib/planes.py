"""Outflow cutting planes (protocol §9): skeleton-constrained, bidirectional search,
tilt override and free-hand fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import SimpleITK as sitk

Plane = Tuple[np.ndarray, np.ndarray]  # (point, unit normal) in physical LPS mm


class BoundsOrderError(ValueError):
    """The lower bound ended up above the upper bound: repeat the search (§9.4.4)."""


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n == 0:
        raise ValueError("zero-length vector")
    return v / n


@dataclass
class Skeleton:
    """Skeleton path of (ventricle ∪ proximal artery) with per-point tubeness normals.

    File format (.npz): `points` (N, 3) and `normals` (N, 3), physical LPS mm.
    Index 0 should be in the ventricle and N-1 at the artery end; `ensure_ventricle_first`
    fixes the order if needed. See `save_skeleton_npz` to convert voxel-space skeletons.
    """

    points: np.ndarray
    normals: np.ndarray

    def __post_init__(self):
        self.points = np.asarray(self.points, dtype=float).reshape(-1, 3)
        self.normals = np.asarray(self.normals, dtype=float).reshape(-1, 3)
        if len(self.points) < 2 or self.points.shape != self.normals.shape:
            raise ValueError("skeleton needs >= 2 points and one normal per point")
        self._orient_normals()
        seg = np.linalg.norm(np.diff(self.points, axis=0), axis=1)
        self.cum_length = np.concatenate([[0.0], np.cumsum(seg)])

    @classmethod
    def load_npz(cls, path: str) -> "Skeleton":
        data = np.load(path)
        return cls(data["points"], data["normals"])

    def __len__(self) -> int:
        return len(self.points)

    @property
    def max_index(self) -> int:
        return len(self.points) - 1

    def _orient_normals(self) -> None:
        """Eigenvector signs are arbitrary: point every normal along increasing index
        (towards the artery), so 'below the plane' is always the ventricle side."""
        n = np.array([_unit(v) for v in self.normals])
        idx = np.arange(len(n))
        tangent = self.points[np.minimum(idx + 1, len(n) - 1)] - self.points[np.maximum(idx - 1, 0)]
        flip = np.einsum("ij,ij->i", n, tangent) < 0
        n[flip] *= -1
        self.normals = n

    def reversed(self) -> "Skeleton":
        return Skeleton(self.points[::-1].copy(), self.normals[::-1].copy())

    def _interp(self, values: np.ndarray, index: float) -> np.ndarray:
        index = float(np.clip(index, 0, self.max_index))
        i0 = int(np.floor(index))
        i1 = min(i0 + 1, self.max_index)
        w = index - i0
        return (1 - w) * values[i0] + w * values[i1]

    def point_at(self, index: float) -> np.ndarray:
        return self._interp(self.points, index)

    def normal_at(self, index: float) -> np.ndarray:
        return _unit(self._interp(self.normals, index))

    def arc_length(self, index: float) -> float:
        return float(self._interp(self.cum_length, index))


def save_skeleton_npz(path: str, points_zyx, normals_zyx, reference: sitk.Image) -> None:
    """Convert a skeleton given in (z, y, x) voxel indices of `reference`
    (e.g. from the existing feature pipeline) to the physical .npz format."""
    pts = np.asarray(points_zyx, dtype=float)[:, ::-1]  # -> (x, y, z) index
    nrm = np.asarray(normals_zyx, dtype=float)[:, ::-1]
    phys = np.array([reference.TransformContinuousIndexToPhysicalPoint(tuple(p)) for p in pts])
    direction = np.array(reference.GetDirection()).reshape(3, 3)
    spacing = np.array(reference.GetSpacing())
    nphys = np.array([_unit(direction @ (spacing * v)) for v in nrm])  # direction in mm space
    np.savez(path, points=phys, normals=nphys)


def rotate_normal(normal, tilt_a_deg: float, tilt_b_deg: float) -> np.ndarray:
    """Tilt a normal by two angles around two axes perpendicular to it (§9.6)."""
    n = _unit(normal)
    helper = np.eye(3)[int(np.argmin(np.abs(n)))]
    u = _unit(np.cross(n, helper))
    v = np.cross(n, u)

    def rot(vec, axis, deg):
        t = np.deg2rad(deg)
        return vec * np.cos(t) + np.cross(axis, vec) * np.sin(t) + axis * np.dot(axis, vec) * (1 - np.cos(t))

    return _unit(rot(rot(n, u, tilt_a_deg), v, tilt_b_deg))


def below_plane_mask(reference: sitk.Image, point, normal) -> np.ndarray:
    """(z, y, x) mask of voxels with (x - p)·n <= 0, i.e. on the ventricle side.
    Evaluated in index space with broadcasting, so no coordinate arrays are allocated."""
    n = _unit(normal)
    spacing = np.array(reference.GetSpacing())
    direction = np.array(reference.GetDirection()).reshape(3, 3)
    origin = np.array(reference.GetOrigin())
    coef = spacing * (direction.T @ n)  # per index axis (x, y, z)
    const = float((origin - np.asarray(point, dtype=float)) @ n)
    nx, ny, nz = reference.GetSize()
    vx = np.arange(nx) * coef[0]
    vy = np.arange(ny) * coef[1]
    vz = np.arange(nz) * coef[2]
    return (vz[:, None, None] + vy[None, :, None] + vx[None, None, :] + const) <= 0.0


@dataclass
class PlaneState:
    """State of one outflow plane. Bidirectional search (§9.4): slide down from MAX and
    record the upper bound, slide up from MIN and record the lower bound; the GT index
    is their midpoint, the range width is the plane's uncertainty."""

    outflow: str
    skeleton: Optional[Skeleton] = None
    upper: Optional[int] = None
    lower: Optional[int] = None
    tilt_override: bool = False
    tilt_a: float = 0.0
    tilt_b: float = 0.0
    free_point: Optional[np.ndarray] = None
    free_normal: Optional[np.ndarray] = None
    skeleton_failed: bool = False
    not_present: bool = False
    locked: bool = False
    preview_index: Optional[float] = None  # slider position during the search; never logged

    def set_upper(self, index: int) -> None:
        self.upper = int(index)
        self._check_order()

    def set_lower(self, index: int) -> None:
        self.lower = int(index)
        self._check_order()

    def _check_order(self) -> None:
        if self.upper is not None and self.lower is not None and self.lower > self.upper:
            self.upper = self.lower = None
            raise BoundsOrderError("The lower bound is above the upper bound: repeat the search (protocol §9.4.4).")

    @property
    def gt_index(self) -> Optional[float]:
        if self.upper is None or self.lower is None:
            return None
        return (self.upper + self.lower) / 2.0

    def set_free_hand(self, point, normal) -> None:
        self.free_point = np.asarray(point, dtype=float)
        self.free_normal = _unit(normal)
        self.skeleton_failed = True

    def clear_free_hand(self) -> None:
        self.free_point = self.free_normal = None
        self.skeleton_failed = False

    def preview(self, index: float) -> Optional[Plane]:
        if self.skeleton is None:
            return None
        return self.skeleton.point_at(index), self.skeleton.normal_at(index)

    def plane(self, use_preview: bool = False) -> Optional[Plane]:
        """The plane that constrains the mask, or None if not defined yet.
        With use_preview, an unfinished search shows the cut at the slider position."""
        if self.not_present:
            return None
        if self.free_point is not None:
            return self.free_point, self.free_normal
        if self.skeleton is None:
            return None
        index = self.gt_index
        if index is None:
            if not use_preview or self.preview_index is None:
                return None
            index = self.preview_index
        point = self.skeleton.point_at(index)
        normal = self.skeleton.normal_at(index)
        if self.tilt_override and self.gt_index is not None:
            normal = rotate_normal(normal, self.tilt_a, self.tilt_b)
        return point, normal

    def reset_search(self) -> None:
        self.upper = self.lower = None
        self.locked = False

    @property
    def resolved(self) -> bool:
        return self.not_present or self.plane() is not None

    def to_log(self) -> dict:
        plane = self.plane()
        width_index = width_mm = None
        if self.gt_index is not None:
            width_index = self.upper - self.lower
            if self.skeleton is not None:
                width_mm = self.skeleton.arc_length(self.upper) - self.skeleton.arc_length(self.lower)
        return {
            "not_present": self.not_present,
            "upper_index": self.upper,
            "lower_index": self.lower,
            "gt_index": self.gt_index,
            "gt_arc_length_mm": None if (self.gt_index is None or self.skeleton is None)
            else self.skeleton.arc_length(self.gt_index),
            "range_width_index": width_index,
            "range_width_mm": width_mm,
            "skeleton_length": None if self.skeleton is None else len(self.skeleton),
            "point_lps_mm": None if plane is None else [float(v) for v in plane[0]],
            "normal_lps": None if plane is None else [float(v) for v in plane[1]],
            "tilt_override": self.tilt_override,
            "tilt_deg": [self.tilt_a, self.tilt_b] if self.tilt_override else None,
            "skeleton_failed": self.skeleton_failed,
            "locked": self.locked,
        }
