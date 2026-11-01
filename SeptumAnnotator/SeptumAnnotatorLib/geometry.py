from dataclasses import dataclass

import numpy as np


def lps_to_ras(values: np.ndarray) -> np.ndarray:
    """Convert coordinates from LPS convention to Slicer's RAS convention."""
    converted = np.asarray(values, dtype=float).copy()
    converted[..., :2] *= -1
    return converted


def unit(values: np.ndarray) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    length = np.linalg.norm(vector)
    if length == 0:
        raise ValueError("zero-length vector")
    return vector / length


@dataclass
class Plane:
    """Vágósík RAS-koordinátákban: egy pont és egy egységnormál."""

    point: np.ndarray
    normal: np.ndarray

    def tilted(self, a_deg: float, b_deg: float) -> "Plane":
        """A sík normáljának megdöntött változatát adja vissza."""
        # A sík pontja változatlan marad, csak az irányt meghatározó normált forgatjuk.
        return Plane(self.point, rotateNormal(self.normal, a_deg, b_deg))


def rotateNormal(normal: np.ndarray, a_deg: float, b_deg: float) -> np.ndarray:
    """A normált két, rá merőleges tengely körül dönti meg (Rodrigues-forgatás)."""
    n = normal / np.linalg.norm(normal)
    u = np.cross(n, np.eye(3)[np.argmin(np.abs(n))])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)

    def rot(vec, axis, deg):
        t = np.deg2rad(deg)
        return (
            vec * np.cos(t)
            + np.cross(axis, vec) * np.sin(t)
            + axis * np.dot(axis, vec) * (1 - np.cos(t))
        )

    r = rot(rot(n, u, a_deg), v, b_deg)
    return r / np.linalg.norm(r)


def belowPlaneMask(ijk_to_ras: np.ndarray, shape_kji, plane: Plane, bounds=None) -> np.ndarray:
    """Bool maszk (k, j, i) sorrendben: True a sík kamra felőli oldalán lévő voxeleknél.

    Feltételezi, hogy plane.normal egységvektor, és az artéria felé mutat.
    bounds: opcionális (k_slice, j_slice, i_slice), ekkor csak ezt a részt számolja
    és a kimenet alakja a bounds alakja.
    """
    # Előjeles távolság egy x RAS pontra: d = (x - p) · n   (mm-ben, mert |n| = 1)
    #   d = 0: a síkon,  d < 0: kamra felőli oldal,  d > 0: artéria felőli oldal.
    #
    # Egy voxel RAS pontja: x = M · v + t,  ahol v = (i, j, k),
    #   M = matrix[:3, :3]  (IJK -> RAS forgatás és voxelméret),
    #   t = matrix[:3, 3]   (a (0, 0, 0) voxel középpontjának RAS koordinátája).
    #
    # Behelyettesítve:  d = (M · v) · n + (t - p) · n
    # Mivel (M · v) · n = v · (Mᵀ · n):
    #   d = i·ci + j·cj + k·ck + constant,   ahol (ci, cj, ck) = Mᵀ · n.
    # Ezért a síkérték három tengelyenkénti 1D tag összegeként számítható,
    # és nem kell teljes 3D RAS-koordinátatömböket létrehozni.
    coefficients = ijk_to_ras[:3, :3].T @ plane.normal
    constant = (ijk_to_ras[:3, 3] - plane.point) @ plane.normal

    # A numpy-maszk tengelyei (k, j, i).
    if bounds is None:
        bounds = tuple(slice(0, n) for n in shape_kji)
    k_slice, j_slice, i_slice = bounds

    # Abszolút indexekkel számolunk (nem a bounding boxhoz képest),
    # ezért a constant a részterületen is ugyanaz. A konstanst egyszer, a k taghoz adjuk.
    i = (np.arange(i_slice.start, i_slice.stop) * coefficients[0]).astype(np.float32)
    j = (np.arange(j_slice.start, j_slice.stop) * coefficients[1]).astype(np.float32)
    k = (np.arange(k_slice.start, k_slice.stop) * coefficients[2] + constant).astype(np.float32)

    # Broadcasting: a három 1D tag (k, j, i) alakú tömbbé adódik össze, így minden voxelnél
    # megkapjuk d-t. Pl. az i tag egy adott i indexnél minden j-re és k-ra ugyanaz.
    # float32: a pontosan a síkon fekvő voxelek kerekítés miatt bármelyik oldalra kerülhetnek.
    return (k[:, None, None] + j[None, :, None] + i[None, None, :]) <= 0