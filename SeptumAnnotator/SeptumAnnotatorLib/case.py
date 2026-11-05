from __future__ import annotations

import os

import numpy as np
import SimpleITK as sitk

from scipy import ndimage
import slicer
from slicer import vtkMRMLScalarVolumeNode

from SeptumAnnotatorLib.labels import REQUIRED_STRUCTURES, loadLabelConfig
from SeptumAnnotatorLib.seed import convexHullMask
from SeptumAnnotatorLib.skeleton import Skeleton


class CaseData:
    """Egy betöltött eset: a node-ok és a számításokhoz szükséges tömbök."""

    case_id: str
    ct: vtkMRMLScalarVolumeNode
    ct_array: np.ndarray
    segmentation: slicer.vtkMRMLSegmentationNode
    segment_ids: dict  # {struktúra_név: segment_id}
    label_config: dict
    # Az UI-prefixekkel megegyező kulcsokkal kezeljük a skeletonokat és a síkokat.
    skeletons: dict
    generate_skeleton: bool = False

    _masks: dict[str, np.ndarray]  # {struktúra_név: bináris maszk (Z, Y, X) sorrendben}
    _distances: dict[str, np.ndarray]  # {struktúra_név: távolságtérkép (Z, Y, X) sorrendben, float32}
    _sheetness: np.ndarray  # (Z, Y, X) sorrendben, float32
    _hull: np.ndarray | None  # (Z, Y, X) sorrendben, bool

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
        """Átnevezi a szegmenseket a label config alapján; {név: segment_id} szótárt ad vissza."""
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

    def ijkToRas(self) -> np.ndarray:
        """A CT node IJK -> RAS transzformációs mátrixát numpy tömbként adja vissza."""
        import vtk

        ijk_to_ras = vtk.vtkMatrix4x4()
        self.ct.GetIJKToRASMatrix(ijk_to_ras)
        return slicer.util.arrayFromVTKMatrix(ijk_to_ras)