"""Hole QA (protocol §10): find every place where LV and RV blood pools connect
without crossing septum voxels, using 6-connectivity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import SimpleITK as sitk

DECISIONS = ("approve", "reject")


def dilate6(mask: np.ndarray) -> np.ndarray:
    """One-voxel dilation with face (6-) connectivity."""
    out = mask.copy()
    out[1:] |= mask[:-1]
    out[:-1] |= mask[1:]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    out[:, :, 1:] |= mask[:, :, :-1]
    out[:, :, :-1] |= mask[:, :, 1:]
    return out


def label6(mask: np.ndarray) -> np.ndarray:
    """Connected components with face connectivity (fullyConnected=False)."""
    img = sitk.GetImageFromArray(mask.astype(np.uint8))
    return sitk.GetArrayFromImage(sitk.ConnectedComponent(img, False))


@dataclass
class Opening:
    id: str
    voxels: np.ndarray = field(repr=False)  # sorted flat indices into the (z, y, x) grid
    size_vox: int
    size_mm3: float
    centroid_zyx: tuple
    centroid_lps: tuple
    origin: str  # "seed_gap" | "direct_contact" | "manual_edit"
    gt_override: bool = False  # patching it would cover GT chamber voxels
    blind_decision: Optional[str] = None
    decision: Optional[str] = None
    uncertain: bool = False

    def decide(self, decision: str, uncertain: bool = False) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if self.blind_decision is None:
            self.blind_decision = decision  # first decision is the blind one
        self.decision = decision
        self.uncertain = uncertain

    def to_log(self) -> dict:
        return {
            "id": self.id,
            "size_vox": self.size_vox,
            "size_mm3": round(self.size_mm3, 2),
            "centroid_lps_mm": [round(float(v), 2) for v in self.centroid_lps],
            "origin": self.origin,
            "gt_override": self.gt_override,
            "blind_decision": self.blind_decision,
            "final_decision": self.decision,
            "uncertain": self.uncertain,
        }


def _make_opening(flat: np.ndarray, origin: str, shape, reference: sitk.Image, gt_override: bool) -> Opening:
    zyx = np.array(np.unravel_index(flat, shape)).T
    c = zyx.mean(0)
    lps = reference.TransformContinuousIndexToPhysicalPoint((float(c[2]), float(c[1]), float(c[0])))
    vox_mm3 = float(np.prod(reference.GetSpacing()))
    oid = f"{origin[0]}-{int(round(c[0]))}-{int(round(c[1]))}-{int(round(c[2]))}"
    return Opening(oid, flat, len(flat), len(flat) * vox_mm3, tuple(c), tuple(lps), origin, gt_override)


def detect_openings(septum: np.ndarray, lv: np.ndarray, rv: np.ndarray, domain: np.ndarray,
                    reference: sitk.Image, seed: Optional[np.ndarray] = None) -> List[Opening]:
    """Openings inside `domain` (region ∩ below the planes), largest first.

    * gap openings: 6-connected components of non-septum, non-chamber voxels that touch
      both the LV and the RV (e.g. a gap in label 5);
    * direct contacts: LV voxels face-adjacent to RV voxels (no tissue at all between them).
    """
    shape = septum.shape
    lv_d, rv_d = dilate6(lv), dilate6(rv)
    openings: List[Opening] = []

    gap = domain & ~septum & ~lv & ~rv
    if gap.any():
        cc = label6(gap)
        touch_lv = np.unique(cc[lv_d & (cc > 0)])
        touch_rv = np.unique(cc[rv_d & (cc > 0)])
        for lab in np.intersect1d(touch_lv, touch_rv):
            flat = np.flatnonzero(cc == lab)
            removed_by_user = seed is not None and bool(seed.flat[flat].any())
            openings.append(_make_opening(flat, "manual_edit" if removed_by_user else "seed_gap",
                                          shape, reference, gt_override=False))

    contact = domain & ((lv & rv_d) | (rv & lv_d)) & ~septum
    if contact.any():
        cc = label6(contact)
        for lab in np.unique(cc[cc > 0]):
            openings.append(_make_opening(np.flatnonzero(cc == lab), "direct_contact",
                                          shape, reference, gt_override=True))

    openings.sort(key=lambda o: o.size_vox, reverse=True)
    return openings


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.intersect1d(a, b, assume_unique=True).size
    return inter / float(a.size + b.size - inter) if inter else 0.0


def carry_over(previous: List[Opening], current: List[Opening], min_iou: float = 0.5) -> List[Opening]:
    """Match recomputed openings to earlier ones (IoU >= min_iou).

    * matched an approved opening -> decision is kept;
    * matched a rejected opening (it is open again, e.g. the patch was erased) ->
      only the blind decision is kept, the opening must be decided again;
    * changed / new openings come back undecided (protocol §5).

    Returns the earlier openings that were matched."""
    matched = []
    for new in current:
        best, best_iou = None, 0.0
        for old in previous:
            if old.blind_decision is None:
                continue
            iou = _iou(new.voxels, old.voxels)
            if iou > best_iou:
                best, best_iou = old, iou
        if best is None or best_iou < min_iou:
            continue
        matched.append(best)
        new.blind_decision = best.blind_decision
        if best.decision == "approve":
            new.decision, new.uncertain = best.decision, best.uncertain
    return matched
