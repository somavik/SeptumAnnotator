from dataclasses import dataclass

import numpy as np

from SeptumAnnotatorLib.geometry import belowPlaneMask


def convexHullMask(mask: np.ndarray, chunk: int = 100000) -> np.ndarray:
    """A myocardium hull seed kiszámítása a szívkamrák és a septum alapján."""

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
    out = np.zeros(mask.shape, dtype=bool)
    plane_axes = [np.arange(lo[axis], hi[axis]) for axis in (1, 2)]
    plane = np.stack(np.meshgrid(*plane_axes, indexing="ij"), axis=-1).reshape(-1, 2)
    grid = np.empty((len(plane), 3), dtype=plane.dtype)
    grid[:, 1:] = plane
    slice_inside = np.empty(len(plane), dtype=bool)

    for slice_index in range(lo[0], hi[0]):
        grid[:, 0] = slice_index
        for start in range(0, len(grid), chunk):
            stop = min(start + chunk, len(grid))
            slice_inside[start:stop] = tri.find_simplex(grid[start:stop]) >= 0
        out[slice_index, lo[1]:hi[1], lo[2]:hi[2]] = (
            slice_inside.reshape(hi[1:] - lo[1:])
        )

    return out | mask


@dataclass
class SeedSettings:
    strategy: str = "myocardium"
    region: str = "band"
    d_mm: float = 5.0
    threshold: float = 0.1


def cutBelowPlanes(base: np.ndarray, ijk_to_ras: np.ndarray, planes) -> np.ndarray:
    """Az alap-seedet a síkok alatti tartományra vágja, csak a seed bounding boxán számolva."""
    mask = base.copy()
    # A síkteszt csak a seed ténylegesen elfoglalt bounding boxán fut.
    # Ez a teljes CT-rács helyett jellemzően csak kis részterületet jelent.
    occupied = np.any(mask, axis=(1, 2)), np.any(mask, axis=(0, 2)), np.any(mask, axis=(0, 1))
    occupied_indices = [np.flatnonzero(axis) for axis in occupied]
    if not all(len(indices) for indices in occupied_indices):
        return mask

    bounds = tuple(
        slice(int(indices[0]), int(indices[-1]) + 1)
        for indices in occupied_indices
    )
    for plane in planes:
        # A síkok metszete a seed bounding boxán belül, az eredeti maszkkal.
        mask[bounds] &= belowPlaneMask(ijk_to_ras, mask.shape, plane, bounds)
    return mask