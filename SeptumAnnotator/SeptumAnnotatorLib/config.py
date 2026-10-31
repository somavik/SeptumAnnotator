from __future__ import annotations

from importlib.resources import path
import json
import logging
import os
from typing import TYPE_CHECKING

import numpy as np
import SimpleITK as sitk


from scipy import ndimage
import slicer
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLLabelMapVolumeNode, vtkMRMLSegmentationNode
"""
Mi a különbség a vtkMRMLLabelMapVolumeNode és a vtkMRMLSegmentationNode között?
LabelMap: voxel = címkeérték (int), minden voxelhez egy címke tartozik, egy voxel csak egy int  értéket vehet fel

"""

from dataclasses import dataclass

if TYPE_CHECKING:
    from SeptumAnnotatorLib.planes import Plane

REQUIRED_STRUCTURES = {"lv", "rv", "aorta", "pa"}  # + "myo" a myocardium seed-stratégiához

def lps_to_ras(values: np.ndarray) -> np.ndarray:
    """Convert coordinates from LPS convention to Slicer's RAS convention."""
    converted = np.asarray(values, dtype=float).copy()
    converted[..., :2] *= -1
    return converted


def loadLabelConfig(path):
    """{címkeérték: név} szótárat ad vissza, ellenőrzés után."""
    with open(path, encoding="utf-8") as f:
        name_to_value = json.load(f)
        
    # Ellenőrizzük, hogy minden kötelező struktúra szerepel-e
    missing = REQUIRED_STRUCTURES - set(name_to_value)
    if missing:
        raise ValueError(f"{os.path.basename(path)}: missing structures {sorted(missing)}")
    
    # Ellenőrizzük, hogy nincs-e két névhez ugyanaz a címkeérték
    value_to_name = {int(v): name for name, v in name_to_value.items()}
    if len(value_to_name) != len(name_to_value):
        raise ValueError(f"{os.path.basename(path)}: two structures share a label value")
    
    return value_to_name

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


class Skeleton:
    """(kamra -> cső) vázútvonal, RAS koordinátákban."""

    def __init__(self, skeleton_path: str):
        # Pontok és normál vektorok várhatóan: LPS, [N, 3] tömbök, normálok hossza 1
        data = np.load(skeleton_path)
        points = np.asarray(data["points"], dtype=float).reshape(-1, 3) # biztosítjuk a dimenziókat
        normals = np.asarray(data["normals"], dtype=float).reshape(-1, 3)
        lengths = np.linalg.norm(normals, axis=1) # biztosítjuk, hogy a normálok hosszát
        
        # Ellenőrizzük, hogy nincs-e nulla hosszúságú normálvektor
        if (lengths == 0).any():
            raise ValueError(f"zero normals at indices {np.flatnonzero(lengths == 0).tolist()}")
        
        
        tangent = np.gradient(points, axis=0) # tangent[i]: megmutatja, hogy a pont[i]-től merre van a pont[i+1] (a váz mentén)
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
        from SeptumAnnotatorLib.planes import Plane

        idx = int(np.floor(i + 0.5))
        return Plane(self.points[idx], self.normals[idx])

class CaseData:
    """Egy betöltött eset: a node-ok és a számításokhoz szükséges tömbök."""

    case_id: str
    ct : vtkMRMLScalarVolumeNode
    ct_array : np.ndarray
    segmentation: slicer.vtkMRMLSegmentationNode
    segment_ids: dict  # {struktúra_név: segment_id}
    label_config : dict
    # Az UI-prefixekkel megegyező kulcsokkal kezeljük a skeletonokat és a síkokat.
    skeletons : dict
    generate_skeleton: bool = False
    
    _masks: dict[str, np.ndarray]  # {struktúra_név: bináris maszk (Z, Y, X) sorrendben}
    _distances: dict[str, np.ndarray]  # {struktúra_név: távolságtérkép (Z, Y, X) sorrendben, float32}
    _sheetness : np.ndarray  # (Z, Y, X) sorrendben, float32
    _hull : np.ndarray | None  # (Z, Y, X) sorrendben, bool
    
    nodes: list  # A CaseData által létrehozott Slicer-node-ok nyilvántartása.

    def __init__(self, ct_path: str, seg_path: str, label_config_path: str, skeleton_lv_aorta_path: str = "", skeleton_rv_pa_path: str = "", generate_skeleton: bool = False, case_id: str = ""):
        self.case_id = case_id
        self.generate_skeleton = generate_skeleton
        self.label_config = loadLabelConfig(label_config_path)

        self.skeletons = {}
        if not generate_skeleton:
            self.skeletons = {
                "lvAorta": Skeleton(skeleton_lv_aorta_path),
                "rvPa": Skeleton(skeleton_rv_pa_path),
            }
        # Skeleton-generálási módban a későbbi pipeline töltheti fel ezt a
        # szótárat; addig az OutflowPlane-ok None skeletonnal is létrejöhetnek.

        # Node-ok: mindet rögtön nyilvántartjuk, hiba esetén takarítunk
        self.nodes = []
        try:
            self.ct = self.track(slicer.util.loadVolume(ct_path, {"show": False}))
            self._checkZDirection(ct_path)
            self.ct_array = slicer.util.arrayFromVolume(self.ct)
            
            self.segmentation = self.track(slicer.util.loadSegmentation(seg_path, {"show": False}))
            self.segment_ids = self._indexSegments(os.path.basename(seg_path))
            
            self._distances = {}
            self._sheetness = None
            self._hull = None
            self._masks = {}            
            
        except Exception:
            self.removeNodes()
            raise


    def _checkZDirection(self, path: str) -> None:
        """A váz sorrendje a generátorban a Z-indexen alapul: a növekvő Z-indexnek superior irányba kell mutatnia."""
        reader = sitk.ImageFileReader()
        reader.SetFileName(path)
        reader.ReadImageInformation()  # csak a fejléc
        k_dir = np.array(reader.GetDirection()).reshape(3, 3)[:, 2]  # LPS; az S mindkét rendszerben +z
        name = os.path.basename(path)
        if k_dir[2] <= 0:
            raise ValueError(f"{name}: increasing Z index points inferior ({np.round(k_dir, 2)}); "
                            f"the skeleton order would be reversed.")
        if k_dir[2] < 0.9:
            print(f"{name}: the Z axis is oblique ({np.round(k_dir, 2)}).")


    def track(self, node):
        self.nodes.append(node)
        return node

    def removeNodes(self):
        for node in self.nodes:
            if slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        self.nodes = []

    def _indexSegments(self, file_name: str) -> dict:
        """Átnevezi a szegmenseket a label config alapján; {név: segment_id} szótárat ad vissza."""
        segment_ids = {}
        segmentation = self.segmentation.GetSegmentation()
        for i in range(segmentation.GetNumberOfSegments()):
            segment_id = segmentation.GetNthSegmentID(i)
            segment = segmentation.GetSegment(segment_id)
            name = self.label_config.get(segment.GetLabelValue())
            if name is not None:
                segment.SetName(name)
                segment_ids[name] = segment_id
        missing = REQUIRED_STRUCTURES - set(segment_ids)
        if missing:
            raise ValueError(f"{file_name}: no voxels for {sorted(missing)}")
        return segment_ids

    def hasStructure(self, name: str) -> bool:
        return name in self.segment_ids

    def mask(self, name: str) -> np.ndarray:
        """A struktúra bináris maszkja a CT rácsán; első híváskor számolja, utána a tároltat adja."""
        if name not in self._masks:
            if name not in self.segment_ids:
                raise ValueError(f"Case {self.case_id}: no '{name}' segment in the segmentation")
            self._masks[name] = slicer.util.arrayFromSegmentBinaryLabelmap(
                self.segmentation, self.segment_ids[name], self.ct) > 0
        return self._masks[name]
    
    def distanceMap(self, name: str) -> np.ndarray:
        if name not in self._distances:
            spacing_kji = self.ct.GetSpacing()[::-1]  # a tömb (k, j, i) sorrendű
            self._distances[name] = ndimage.distance_transform_edt(
                ~self.mask(name), sampling=spacing_kji).astype(np.float32)
        return self._distances[name]
        
    def sheetness(self) -> np.ndarray:
        if self._sheetness is None:
            raise NotImplementedError("Sheetness map is not available yet.")
        return self._sheetness
    
    def hull(self) -> np.ndarray:
        if self._hull is None:
            self._hull = convexHullMask(self.mask("lv") | self.mask("rv"))
        return self._hull
    
    

    
@dataclass
class SeedSettings:
    strategy: str = "myocardium"
    region: str = "band"
    d_mm: float = 5.0
    threshold: float = 0.1

class SeedData:
    """Egy eset seedje: beállítások, eredmény-maszk és a megjelenítő node."""

    def __init__(self, case: CaseData):
        self.case = case
        self.settings = None
        self.mask = None
        self.node = case.track(slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", f"{case.case_id}_seed"))  # a case takarítja
        self.node.SetReferenceImageGeometryParameterFromVolumeNode(case.ct)
        self.node.CreateDefaultDisplayNodes()
        self.node.GetDisplayNode().SetOpacity2DFill(0.5)
        self.segmentId = self.node.GetSegmentation().AddEmptySegment("seed", "seed", (0.2, 0.9, 0.3))

    def update(self, settings: SeedSettings, mask: np.ndarray) -> None:
        self.settings, self.mask = settings, mask
        slicer.util.updateSegmentBinaryLabelmapFromArray(
            mask.astype(np.uint8), self.node, self.segmentId, self.case.ct)
        self.node.CreateClosedSurfaceRepresentation()  # minden frissítés után kell
