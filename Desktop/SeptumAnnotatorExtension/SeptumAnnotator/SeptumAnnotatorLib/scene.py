from __future__ import annotations

import numpy as np
import slicer

from SeptumAnnotatorLib.geometry import Plane, unit
from SeptumAnnotatorLib.seed import SeedSettings, cutBelowPlanes


class SeptumData:
    """Egy eset septum maszkja: karbantartott maszk, base, added, removed és a megjelenítő node."""

    def __init__(self, case):
        self.case = case
        self.mask = None
        self.added = None
        self.removed = None
        self.base = None
        self.node = case.track(slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", f"{case.case_id}_seed"))  # a case takarítja
        self.node.SetReferenceImageGeometryParameterFromVolumeNode(case.ct)
        self.node.CreateDefaultDisplayNodes()
        self.node.GetDisplayNode().SetOpacity2DFill(0.5)
        self.segmentId = self.node.GetSegmentation().AddEmptySegment("seed", "seed", (0.2, 0.9, 0.3))

    def hasEdits(self) -> bool:
        print("hasEdits: added.any() =", self.added.any() if self.added is not None else None,
              "removed.any() =", self.removed.any() if self.removed is not None else None)
        self.syncEdits()  # a még nem szinkronizált festést is számoljuk
        return self.mask is not None and bool(self.added.any() or self.removed.any())

    def setBase(self, base: np.ndarray, resetEdits: bool) -> None:
        """Csak tárol: új alap-seed, a szerkesztések megtartásával vagy törlésével."""
        self.base = base
        if resetEdits or self.added is None:
            self.added = np.zeros(base.shape, dtype=bool)
            self.removed = np.zeros(base.shape, dtype=bool)
            self.mask = None
        else:
            self.syncEdits()
            
    def syncEdits(self) -> None:
        """A node és a legutóbb kiírt eredmény különbsége = a felhasználó szerkesztései."""
        if self.mask is None:
            return
        current = slicer.util.arrayFromSegmentBinaryLabelmap(self.node, self.segmentId, self.case.ct) > 0
        painted, erased = current & ~self.mask, self.mask & ~current
        self.added = (self.added | painted) & ~erased
        self.removed = (self.removed | erased) & ~painted
    
    def compose(self, ijk_to_ras, planes) -> None:
        """Eredmény = síkokkal vágva ((base ∪ added) ∖ removed); a node-ba is kiírja."""
        self.mask = cutBelowPlanes((self.base | self.added) & ~self.removed, ijk_to_ras, planes)
        slicer.util.updateSegmentBinaryLabelmapFromArray(
            self.mask.astype(np.uint8), self.node, self.segmentId, self.case.ct)
        self.node.CreateClosedSurfaceRepresentation()


    

class PlaneNodes:
    def __init__(self, case):
        self.case = case
        self.nodes = {}

    def _addPolyline(self, name: str, points: np.ndarray):
        """Kirajzolja a skeleton pontokat a 3D Slicer nézetébe sárga vonalként."""
        import vtk
        pts = vtk.vtkPoints()
        for p in points:
            pts.InsertNextPoint(p)

        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(len(points))
        for i in range(len(points)):
            line.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)

        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(cells)

        model = slicer.modules.models.logic().AddModel(poly)
        model.SetName(name)
        model.GetDisplayNode().SetColor(1, 1, 0)  # Sárga szín
        model.GetDisplayNode().SetLineWidth(3)
        model.GetDisplayNode().SetVisibility2D(True) # Jelenjen meg a 2D CT-ken is

        return model

    def placePlaneNode(self, key, plane):
        """A kapott Plane objektum alapján beállítja a Slicer markup node-ot"""
        # Használat: az onModeChanged eseménykezelőben, a defaultPlane által visszaadott síkkal.
        node = self.nodes[key]
        node.SetCenterWorld(plane.point)
        node.SetNormalWorld(plane.normal)
        node.SetDisplayVisibility(True)
        node.SetLocked(False)

    def readPlaneNode(self, key) -> Plane:
        """A Slicer markup node-ot egy Plane objektummá alakítja."""
        # Használat: az onFreeHandApply eseménykezelőben.
        node = self.nodes[key]
        point = np.zeros(3)
        normal = np.zeros(3)
        node.GetCenterWorld(point)
        node.GetNormalWorld(normal)
        return Plane(point, unit(normal))

    def showPlane(self, o, plane, freeHand, locked):
        """A markup sík node-ot a megadott síkra állítja, és a felhasználói interakciót engedélyezi vagy tiltja."""
        # Használat: az összes plane eseménykezelőben.
        node = self.nodes[o]
        userControls = freeHand and not locked
        # Szabadkézi, feloldott módban a user mozgathatja a node-ot,
        # más esetben a node-ot a modell által számított síkkal szinkronizáljuk.
        if not userControls:  # zárolt szabadkézi síknál is: a látott sík legyen a használt sík
            if plane is None:
                node.SetDisplayVisibility(False)
            else:
                node.SetCenterWorld(plane.point)
                node.SetNormalWorld(plane.normal)
                node.SetDisplayVisibility(True)
        else:
            node.SetDisplayVisibility(True)
        node.SetLocked(not userControls)
        displayNode = node.GetDisplayNode()
        # Free-hand módban azonnal látszódjanak a forgató és mozgató handle-ek.
        displayNode.SetHandlesInteractive(userControls)
        displayNode.SetRotationHandleVisibility(userControls)
        displayNode.SetTranslationHandleVisibility(userControls)
        displayNode.SetScaleHandleVisibility(False)  # a méret nem befolyásolja a vágást

    def _ensurePlaneNodes(self):
        """Létrehozza a Slicer markup sík node-okat, ha még nem léteznek."""
        # Használat: a processCase eseménykezelőben, az eset sikeres betöltésekor.
        # A kulcsoknak meg kell egyezniük az OUTFLOWS és a CaseData.skeletons kulcsaival.
        for key, name in (("lvAorta", "Plane_lv_aorta"), ("rvPa", "Plane_rv_pa")): # Végig a két síkon
            if key in self.nodes: # Létezik már? Ha igen, akkor nem kell létrehozni.
                continue
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsPlaneNode", name) # Slicer plane létrehozása
            node.CreateDefaultDisplayNodes() # Alap beállítások
            node.SetDisplayVisibility(False) # Egyelőre nem látszódik
            # A node csak a sík vizuális megjelenítését végzi,
            # a tényleges állapot az OutflowPlane objektumban marad.
            self.case.track(node) # Case nyilvántartja központilag
            self.nodes[key] = node # A logika is tárolja, hogy a felület eseménykezelői könnyedén elérjék kulccsal.