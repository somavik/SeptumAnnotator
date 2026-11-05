import numpy as np

from SeptumAnnotatorLib.geometry import Plane, lps_to_ras


class Skeleton:
    """(kamra -> cső) vázútvonal, RAS koordinátákban."""

    def __init__(self, skeleton_path: str):
        # Pontok és normál vektorok várhatóan: LPS, [N, 3] tömbök, normálok hossza 1
        data = np.load(skeleton_path)
        points = np.asarray(data["points"], dtype=float).reshape(-1, 3)
        normals = np.asarray(data["normals"], dtype=float).reshape(-1, 3)
        lengths = np.linalg.norm(normals, axis=1)

        # Ellenőrizzük, hogy nincs-e nulla hosszúságú normálvektor
        if (lengths == 0).any():
            raise ValueError(f"zero normals at indices {np.flatnonzero(lengths == 0).tolist()}")

        # A skeleton mentén számított érintővektorokkal egységesítjük a normálok előjelét.
        tangent = np.gradient(points, axis=0)
        tail = slice(int(len(points) * 0.7), None)
        # Egyetlen előjel az egész vázra, az artéria felőli végből: ott a normál a cső tengelye.
        if np.einsum("ij,ij->i", normals[tail], tangent[tail]).sum() < 0:
            normals = -normals

        # A fájl LPS-ben van, a Slicer RAS-ban dolgozik: egyszer váltunk, betöltéskor
        self.points = lps_to_ras(points)
        self.normals = lps_to_ras(normals) / lengths[:, None]

    @property
    def max_index(self) -> int:
        return len(self.points) - 1

    def planeAt(self, i: float) -> Plane:
        # GT index: (upper + lower) / 2, lehet .5
        idx = int(np.floor(i + 0.5))
        return Plane(self.points[idx], self.normals[idx])