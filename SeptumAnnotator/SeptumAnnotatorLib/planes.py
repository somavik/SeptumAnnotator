from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from SeptumAnnotatorLib.config import Skeleton

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
        return vec * np.cos(t) + np.cross(axis, vec) * np.sin(t) + axis * np.dot(axis, vec) * (1 - np.cos(t))

    r = rot(rot(n, u, a_deg), v, b_deg)
    return r / np.linalg.norm(r)

@dataclass
class OutflowPlane:
    """Egy kiáramlási pálya vágósíkjának végleges állapota.

    Ez az osztály az annotáló döntését tartja nyilván, vagyis azt az állapotot,
    amelyet a műveleti naplóban is érdemes rögzíteni: a módot, a keresés
    határait, a dőlést, a szabadkézi síkot és a zárolást.

    A felhasználói interakció állapota nem ide tartozik. Azt a widget kezeli:
    melyik határ keresése folyik, illetve milyen értéken áll a csúszka.
    Előnézeti sík sincs külön eltárolva; azt a widget a skeleton és a csúszka
    értékéből számítja ki.
    """
    skeleton: Optional[Skeleton]
    mode: str = "skeleton"  # "skeleton" | "free hand" | "no connection"
    upper: Optional[int] = None  # A felső határ, amelyet az annotáló beállított.
    lower: Optional[int] = None  # Az alsó határ, amelyet az annotáló beállított.
    tilt: Optional[Tuple[float, float]] = None  # (a, b) fokban; None = nincs dőlésfelülírás.
    free_hand: Optional[Plane] = None  # A markupból átvett szabadkézi sík.
    locked: bool = False  # A sík beállításait az annotáló lezárta.

    @property
    def gt_index(self) -> Optional[float]:
        """A két beállított határ középpontja, ha mindkettő ismert."""
        # A két skeleton-index átlaga adja a végleges sík helyét.
        if self.upper is None or self.lower is None:
            return None
        return (self.upper + self.lower) / 2

    @property
    def plane(self) -> Optional[Plane]:
        """A döntésből származó végleges sík, amely a seedet vágja.

        A csúszka közbeni előnézet nem itt készül. 
        return None azt jelenti, hogy az aktuális döntés alapján még nincs végleges vágósík.
        """
        if self.mode == "free hand":
            return self.free_hand
        if self.mode == "no connection" or self.skeleton is None or self.gt_index is None:
            return None
        # Skeleton mód és van GT index -> a végleges sík a GT indexről
        # opcionálisan a felhasználói dőlésből
        p = self.skeleton.planeAt(self.gt_index)
        return p.tilted(*self.tilt) if self.tilt is not None else p

    @property
    def isResolved(self) -> bool:
        return self.mode == "no connection" or self.plane is not None

    def setBound(self, which: str, index: int) -> None:
        """Elmenti az annotáló által beállított felső vagy alsó határt."""
        # Fordított sorrendű határok esetén egyik félállapotot sem hagyjuk érvényben.
        setattr(self, which, int(index))
        if self.gt_index is not None and self.lower > self.upper:
            self.upper = self.lower = None
            raise ValueError("The lower bound is above the upper bound: repeat the search.")

    def reset(self) -> None:
        """Visszaállítja a sík döntési paramétereit alapállapotba."""
        # A módot és a zárolást nem töröljük.
        self.upper = self.lower = self.tilt = None

    def describe(self) -> str:
        """Rövid, felhasználónak szánt összefoglalót készít az állapotról."""
        if self.mode == "no connection":
            text = "No cutting plane (connection not present)."
        elif self.mode == "free hand":
            text = "Free-hand plane applied." if self.free_hand else "Drag the plane in the 3D view, then Apply."
        else:
            # lambda függvény a skeleton-indexek megjelenítésére, ha az adott határ nincs beállítva, akkor -, ha be van állítva, akkor a számát írja ki.
            fmt = lambda v: "–" if v is None else f"{v}"
            text = f"upper {fmt(self.upper)} · lower {fmt(self.lower)} · GT {fmt(self.gt_index)}"
            if self.tilt is not None:
                text += f" · tilt a {self.tilt[0]:.1f}°, b {self.tilt[1]:.1f}°"
        return ("LOCKED · " if self.locked else "") + text
