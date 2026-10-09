"""Image I/O, resampling (to the isotropic working grid and back) and distance maps."""

from __future__ import annotations

import math
from typing import Dict, Sequence, Tuple

import numpy as np
import SimpleITK as sitk


def read_image(path: str) -> sitk.Image:
    return sitk.ReadImage(path)


def to_array(img: sitk.Image) -> np.ndarray:
    return sitk.GetArrayFromImage(img)


def from_array(arr: np.ndarray, reference: sitk.Image) -> sitk.Image:
    img = sitk.GetImageFromArray(arr)
    img.CopyInformation(reference)
    return img


def isotropic_reference(img: sitk.Image, spacing_mm: float = 1.0) -> sitk.Image:
    """Empty image covering the same physical extent as `img` with isotropic spacing."""
    old_spacing = img.GetSpacing()
    size = [max(1, int(round(n * s / spacing_mm))) for n, s in zip(img.GetSize(), old_spacing)]
    ref = sitk.Image(size, sitk.sitkUInt8)
    ref.SetSpacing([spacing_mm] * 3)
    ref.SetDirection(img.GetDirection())
    # Keep the corner of the field of view: shift the first voxel centre by the change of half a voxel.
    direction = np.array(img.GetDirection()).reshape(3, 3)
    corner = np.array(img.GetOrigin()) - direction @ (np.array(old_spacing) / 2.0)
    ref.SetOrigin(tuple(corner + direction @ (np.full(3, spacing_mm) / 2.0)))
    return ref


def resample_ct(ct: sitk.Image, reference: sitk.Image, default_hu: float = -1024.0) -> sitk.Image:
    """Continuous (B-spline) interpolation of the CT onto the working grid."""
    return sitk.Resample(sitk.Cast(ct, sitk.sitkFloat32), reference, sitk.Transform(),
                         sitk.sitkBSpline, default_hu, sitk.sitkFloat32)


def resample_continuous(img: sitk.Image, reference: sitk.Image, default: float = 0.0) -> sitk.Image:
    """Linear interpolation of a continuous map (e.g. sheetness) onto the working grid."""
    return sitk.Resample(sitk.Cast(img, sitk.sitkFloat32), reference, sitk.Transform(),
                         sitk.sitkLinear, default, sitk.sitkFloat32)


def resample_labels(seg: sitk.Image, reference: sitk.Image) -> np.ndarray:
    """Per-label resampling: each label's binary mask is interpolated linearly
    (one-hot), then argmax picks the label per voxel. Interpolating the label map
    directly would mix label values (e.g. 1 and 5 -> 3)."""
    seg_arr = to_array(seg)
    values = np.unique(seg_arr)
    if 0 not in values:
        values = np.concatenate([[0], values])
    best = None
    best_val = None
    for value in values:
        mask = sitk.Cast(seg == int(value), sitk.sitkFloat32)
        frac = to_array(sitk.Resample(mask, reference, sitk.Transform(), sitk.sitkLinear, 0.0, sitk.sitkFloat32))
        if best is None:
            best, best_val = frac, np.full(frac.shape, value, dtype=seg_arr.dtype)
        else:
            better = frac > best
            best = np.where(better, frac, best)
            best_val = np.where(better, value, best_val)
    return best_val.astype(np.uint8 if values.max() < 256 else np.uint16)


def extract_structures(label_arr: np.ndarray, label_map: Dict[str, int]) -> Dict[str, np.ndarray]:
    return {name: label_arr == value for name, value in label_map.items()}


def resample_mask_to(mask_img: sitk.Image, reference: sitk.Image) -> np.ndarray:
    """Binary mask onto another grid: linear interpolation of the mask, threshold 0.5.
    Smoother than nearest neighbour when going back to the finer native grid."""
    frac = sitk.Resample(sitk.Cast(mask_img, sitk.sitkFloat32), reference, sitk.Transform(),
                         sitk.sitkLinear, 0.0, sitk.sitkFloat32)
    return to_array(frac) >= 0.5


def distance_map_mm(mask: np.ndarray, reference: sitk.Image) -> np.ndarray:
    """Signed Euclidean distance in mm (negative inside, positive outside)."""
    if not mask.any():
        return np.full(mask.shape, np.inf, dtype=np.float32)
    img = from_array(mask.astype(np.uint8), reference)
    dist = sitk.SignedMaurerDistanceMap(img, insideIsPositive=False, squaredDistance=False, useImageSpacing=True)
    return to_array(dist).astype(np.float32)


def write_mask(mask: np.ndarray, reference: sitk.Image, path: str) -> None:
    sitk.WriteImage(from_array(mask.astype(np.uint8), reference), path, useCompression=True)


def bbox_xyz(mask: np.ndarray, margin_vox: int = 0) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Bounding box of a (z, y, x) mask as SimpleITK (x, y, z) index and size."""
    nz = np.argwhere(mask)
    lo = np.maximum(nz.min(0) - margin_vox, 0)
    hi = np.minimum(nz.max(0) + 1 + margin_vox, mask.shape)
    return tuple(int(v) for v in lo[::-1]), tuple(int(v) for v in (hi - lo)[::-1])


def physical_box_to_index_box(src: sitk.Image, index: Sequence[int], size: Sequence[int],
                              dst: sitk.Image) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Map an index box of `src` onto the (clamped) index box of `dst` that covers it."""
    corners = []
    for dx in (-0.5, size[0] - 0.5):
        for dy in (-0.5, size[1] - 0.5):
            for dz in (-0.5, size[2] - 0.5):
                p = src.TransformContinuousIndexToPhysicalPoint((index[0] + dx, index[1] + dy, index[2] + dz))
                corners.append(dst.TransformPhysicalPointToContinuousIndex(p))
    corners = np.array(corners)
    lo = np.maximum(np.floor(corners.min(0)).astype(int), 0)
    hi = np.minimum(np.ceil(corners.max(0)).astype(int) + 1, dst.GetSize())
    return tuple(int(v) for v in lo), tuple(int(v) for v in np.maximum(hi - lo, 1))


def voxel_volume_mm3(img: sitk.Image) -> float:
    return float(math.prod(img.GetSpacing()))
