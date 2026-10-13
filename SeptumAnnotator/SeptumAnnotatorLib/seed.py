"""Seed generation (protocol 6–7) and the non-destructive edit layers."""

from __future__ import annotations

from typing import Optional

import numpy as np


def septal_band(dist_lv: np.ndarray, dist_rv: np.ndarray, d_mm: float) -> np.ndarray:
    """Voxels within d mm of the LV *and* within d mm of the RV (protocol 6).
    Distances are signed (negative inside), so chamber voxels next to the septum are included."""
    return (dist_lv <= d_mm) & (dist_rv <= d_mm)


def convex_hull_mask(mask: np.ndarray, chunk: int = 1_000_000) -> np.ndarray:
    """Convex hull of a binary mask in index space (the working grid is isotropic)."""
    try:
        from scipy.spatial import ConvexHull, Delaunay
    except ImportError as exc:
        raise ImportError("The convex-hull region needs scipy (pip install scipy)") from exc
    pts = np.argwhere(mask)
    if len(pts) < 4:
        return mask.copy()
    hull = ConvexHull(pts)
    tri = Delaunay(pts[hull.vertices])
    lo, hi = pts.min(0), pts.max(0) + 1
    axes = [np.arange(a, b) for a, b in zip(lo, hi)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    inside = np.empty(len(grid), dtype=bool)
    for start in range(0, len(grid), chunk):
        inside[start:start + chunk] = tri.find_simplex(grid[start:start + chunk]) >= 0
    out = np.zeros(mask.shape, dtype=bool)
    out[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = inside.reshape(hi - lo)
    return out | mask


def seed_myocardium(myo: np.ndarray, region: np.ndarray, below: np.ndarray) -> np.ndarray:
    """Plan A: seed = label 5 ∩ region ∩ below the outflow planes."""
    return myo & region & below


def seed_sheetness(sheetness: np.ndarray, threshold: float, region: np.ndarray, below: np.ndarray) -> np.ndarray:
    """Plan B: seed = (sheetness > t) ∩ region ∩ below the outflow planes."""
    return (sheetness > threshold) & region & below


class EditLayers:
    """Manual edits kept separately from the seed, so moving a slider never erases them.

    final = ((seed | add) & ~remove & below & ~chambers) | patch

    `patch` holds the fills of rejected openings; it is exempt from the
    no-chamber-voxel rule and from the planes (a logged GT override, protocol §5.3).
    """

    def __init__(self, shape):
        self.add = np.zeros(shape, dtype=bool)
        self.remove = np.zeros(shape, dtype=bool)
        self.patch = np.zeros(shape, dtype=bool)
        self.last_pushed: Optional[np.ndarray] = None
        self.dropped_chamber_voxels = 0

    def sync(self, current: Optional[np.ndarray]) -> None:
        """Record what the user changed relative to what was last shown."""
        if current is None or self.last_pushed is None:
            return
        current = current.astype(bool)
        added = current & ~self.last_pushed
        removed = self.last_pushed & ~current
        self.add = (self.add | added) & ~removed
        self.remove = (self.remove | removed) & ~added
        self.patch &= ~removed  # erasing a patched voxel undoes the patch there

    def compose(self, seed: np.ndarray, below: np.ndarray, chambers: np.ndarray) -> np.ndarray:
        self.dropped_chamber_voxels = int((self.add & chambers).sum())
        final = ((seed | self.add) & ~self.remove & below & ~chambers) | self.patch
        self.last_pushed = final.copy()
        return final

    def counts(self) -> dict:
        return {
            "voxels_added": int(self.add.sum()),
            "voxels_removed": int(self.remove.sum()),
            "voxels_patched": int(self.patch.sum()),
            "chamber_voxels_dropped": self.dropped_chamber_voxels,
        }
