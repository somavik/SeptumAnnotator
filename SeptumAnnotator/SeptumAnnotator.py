import os
from typing import Annotated

from scipy import ndimage

from SeptumAnnotatorLib.planes import OutflowPlane, Plane
from SeptumAnnotatorLib.config import CaseData, REQUIRED_STRUCTURES, SeedData, SeedSettings, loadLabelConfig, Skeleton, convexHullMask

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper


# Slicer újraindítás: slicer.util.reloadScriptedModule("SeptumAnnotator")

#
# SeptumAnnotator
#

class SeptumAnnotator(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Septum Annotator") 
        self.parent.categories = ["Segmentation"] # ez alatt fog megjelenni a modul a Slicerben
        self.parent.dependencies =  ["Segmentations", "Markups"] # segmentations -> septum, struktúrák. Markups -> vágósíkok, skeleton pontok. 
        self.parent.contributors = ["Soma Szolár (BME MIT)"]  

        # _() function marks text as translatable to other languages
        self.parent.helpText = _("""
        Semi-automatic annotation of the interventricular septum: automatic seed,
        skeleton-constrained outflow planes, correction and hole QA. 
        See README.md.
        """)

        self.parent.acknowledgementText = _("""
        BME MIT
        """)

#
# SeptumAnnotatorWidget
#

import qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

# A combo boxban kiválasztott érték határozza meg, melyik panelcsoport látható.
PLANE_WIDGETS = {
    "skeleton": {"IndexWidget", "TiltWidget"},
    "free hand": {"FreeHandWidget"},
    "no connection": set(),
}
# Ezekből állnak össze a konkrét UI-nevek.
OUTFLOWS = ("lvAorta", "rvPa") # Prefix
ALL_WIDGETS = ("IndexWidget", "TiltWidget", "FreeHandWidget") # Konkrét widgetek, amik a fenti dict-ben szerepelnek.

class SeptumAnnotatorWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    logic: "SeptumAnnotatorLogic"
    
    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        
        # Tárolja, hogy az adott kiáramlási síknál éppen melyik határ keresése folyik
        self._searching = {o: None for o in OUTFLOWS} # pl.: "lvAorta": None | "upper" | "lower"
        # Így a program tudja, hogy melyik csúszka mozgatása mikor frissítse az előnézetet.
        
    def _setDefaultValues(self):
        """
        Beállítja a felület alapértelmezett teszt-útvonalait és állapotait
        a gyorsabb tesztelés érdekében.
        """
        self.ui.ctPathLineEdit.currentPath = (
            r"C:\Users\somas\Desktop\25262\Onlab\project_soveny\ImageCHD_dataset\fixed_headers\ct_1001\ct_1001_fixed_image.nii.gz"
        )
        self.ui.segmentationPathLineEdit.currentPath = (
            r"C:\Users\somas\Desktop\25262\Onlab\project_soveny\ImageCHD_dataset\fixed_headers\ct_1001\ct_1001_fixed_label.nii.gz"
        )
        self.ui.labelConfigPathLineEdit.currentPath = r"C:\Users\somas\Desktop\a.json"
        self.ui.lvAortaSkeletonPathLineEdit.currentPath = (
            r"C:\Users\somas\Desktop\26271\szakgyak\generate_frame\frames\1_skeleton_lv_aorta.npz"
        )
        self.ui.rvPaSkeletonPathLineEdit.currentPath = (
            r"C:\Users\somas\Desktop\26271\szakgyak\generate_frame\frames\1_skeleton_rv_pa.npz"
        )
        self.ui.caseIdLineEdit.text = "1"

    def setup(self):
        print("[DEBUG] Setting up the SeptumAnnotatorWidget...")
        
        # Betöltjük a Qt Designerben megtervezett .ui fájlt
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/SeptumAnnotator.ui"))
        self.layout.addWidget(uiWidget)
        
        # A .ui fájlban névvel rendelkező widgeteket automatikusan attribútumként tölti fel.
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Bekötjük a Slicer jelenetet
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = SeptumAnnotatorLogic()
        
        # Eseménykezelők bekötése (Case)
        self.ui.ctPathLineEdit.currentPathChanged.connect(self._checkCanApply)
        self.ui.segmentationPathLineEdit.currentPathChanged.connect(self._checkCanApply)
        self.ui.labelConfigPathLineEdit.currentPathChanged.connect(self._checkCanApply)
        self.ui.lvAortaSkeletonPathLineEdit.currentPathChanged.connect(self._onSkeletonPathChanged)
        self.ui.rvPaSkeletonPathLineEdit.currentPathChanged.connect(self._onSkeletonPathChanged)
        self.ui.generateSkeletonCheckBox.toggled.connect(self._onGenerateSkeletonToggled)
        self.ui.loadCaseButton.clicked.connect(self.onLoadCaseButton)
        
        # Eseménykezelők bekötése (Seed)
        self.ui.seedStrategyComboBox.currentIndexChanged.connect(self._updateSeedControls)
        self.ui.regionComboBox.currentIndexChanged.connect(self._updateSeedControls)
        self.ui.loadSeedButton.clicked.connect(self.onLoadSeedButton)
        self.ui.seedCollapsibleButton.enabled = False
        self.ui.loadSeedButton.clicked.connect(self.onLoadSeedButton)
        self.ui.bandWidthSliderWidget.valueChanged.connect(self.onSeedParameterChanged)
        self.ui.regionComboBox.currentIndexChanged.connect(self.onSeedParameterChanged)
        self._updateSeedControls()

        # Eseménykezelők bekötése (Planes)
        # Mindkét kiáramlási szekció (rv - aorta, lv -pa) ugyanazt a logikát használja.
        for outflow in OUTFLOWS:
            # A lambda paramétere rögzíti az aktuális outflow értékét, hogy mivel lesz meghívva az eseménykezelő,
            # így a megfelelő panel kapja a megfelelő eseményt.
            self._w(outflow, "ModeComboBox").currentIndexChanged.connect(lambda *_, o=outflow: self.onModeChanged(o))
            self._w(outflow, "IndexSliderWidget").valueChanged.connect(lambda *_, o=outflow: self.onIndexSliderChanged(o))
            self._w(outflow, "StartAtMaxButton").clicked.connect(lambda *_, o=outflow: self.onStartAt(o, "upper"))
            self._w(outflow, "SetUpperButton").clicked.connect(lambda *_, o=outflow: self.onSetBound(o, "upper"))
            self._w(outflow, "StartAtMinButton").clicked.connect(lambda *_, o=outflow: self.onStartAt(o, "lower"))
            self._w(outflow, "SetLowerButton").clicked.connect(lambda *_, o=outflow: self.onSetBound(o, "lower"))
            self._w(outflow, "ResetButton").clicked.connect(lambda *_, o=outflow: self.onResetPlane(o))
            self._w(outflow, "TiltOverrideCheckBox").toggled.connect(lambda *_, o=outflow: self.onTiltChanged(o))
            self._w(outflow, "TiltAngleASliderWidget").valueChanged.connect(lambda *_, o=outflow: self.onTiltChanged(o))
            self._w(outflow, "TiltAngleBSliderWidget").valueChanged.connect(lambda *_, o=outflow: self.onTiltChanged(o))
            self._w(outflow, "ApplyMarkupPlaneButton").clicked.connect(lambda *_, o=outflow: self.onFreeHandApply(o))
            self._w(outflow, "LockCheckBox").toggled.connect(lambda checked, o=outflow: self.onLockToggled(o, checked))
        
        self.ui.caseCollapsibleButton.collapsed = False
        
        self.ui.seedCollapsibleButton.enabled = False
        self.ui.seedCollapsibleButton.collapsed = True
        
        self.ui.planesCollapsibleButton.enabled = False
        self.ui.planesCollapsibleButton.collapsed = True

        self._setDefaultValues()  # Alapértelmezett teszt-értékek beállítása

    
    # --- CASE ESEMÉNYKEZELŐK ---

    def _onSkeletonPathChanged(self, path):
        """Ha betallóznak egy fájlt, vegyük ki a pipát a generálásból."""
        lv_path = self.ui.lvAortaSkeletonPathLineEdit.currentPath
        rv_path = self.ui.rvPaSkeletonPathLineEdit.currentPath
        
        if lv_path or rv_path:
            self.ui.generateSkeletonCheckBox.blockSignals(True)
            self.ui.generateSkeletonCheckBox.checked = False
            self.ui.generateSkeletonCheckBox.blockSignals(False)
            
        self._checkCanApply()
        
    def _onGenerateSkeletonToggled(self, checked):
        # Generálás esetén a fájlválasztók inaktívak (az útvonalak megmaradnak, de nem használjuk őket)
        self.ui.lvAortaSkeletonPathLineEdit.enabled = not checked
        self.ui.rvPaSkeletonPathLineEdit.enabled = not checked
        self._checkCanApply()

    def _skeletonPaths(self):
        """(lv, rv) útvonalak, vagy (None, None), ha generálunk."""
        if self.ui.generateSkeletonCheckBox.checked:
            return None, None
        return (self.ui.lvAortaSkeletonPathLineEdit.currentPath,
            self.ui.rvPaSkeletonPathLineEdit.currentPath)

    def _checkCanApply(self, *args) -> None:
        missing = [name for name, w in (("CT", self.ui.ctPathLineEdit),
                        ("segmentation", self.ui.segmentationPathLineEdit),
                        ("label config", self.ui.labelConfigPathLineEdit))
                                        if not w.currentPath]
        lv, rv = self._skeletonPaths()
        if not self.ui.generateSkeletonCheckBox.checked and not (lv and rv):
            missing.append("both skeletons (or tick Generate Skeleton)")
        self.ui.loadCaseButton.enabled = not missing
        self.ui.loadCaseButton.toolTip = ("Load the case" if not missing
                                    else "Missing: " + ", ".join(missing))

    def onLoadCaseButton(self) -> None:
        lv, rv = self._skeletonPaths()
        with slicer.util.tryWithErrorDisplay("Failed to process the case.", waitCursor=True):
            self.logic.processCase(self.ui.ctPathLineEdit.currentPath,
                            self.ui.segmentationPathLineEdit.currentPath,
                            self.ui.labelConfigPathLineEdit.currentPath,
                            lv, rv,
                            generate_skeletons=self.ui.generateSkeletonCheckBox.checked,
                            case_id=self.ui.caseIdLineEdit.text)
            self._initPlaneControls()
            self.ui.planesCollapsibleButton.enabled = False
            self.ui.seedCollapsibleButton.enabled = True
            self.ui.seedCollapsibleButton.collapsed = False
        
    # --- SEED ESEMÉNYKEZELŐK ---
        
    def onSeedParameterChanged(self, *args):
        seed = self.logic.seed
        if seed is None or seed.settings is None:  # még nem nyomták meg a Load Seed-et
            return
        print(f"[DEBUG] Seed parameters changed: bandWidth={self.ui.bandWidthSliderWidget.value}, region={self.ui.regionComboBox.currentText}")
        self.onLoadSeedButton()
    
    def _seedOptions(self):
        """(strategy, region) a jelenlegi UI-állapotból. Sheetness esetén mindig band."""
        strategy = self.ui.seedStrategyComboBox.currentText.lower()
        region = "band" if strategy == "sheetness" else self.ui.regionComboBox.currentText.lower()
        return strategy, region

    def _updateSeedControls(self, *args):
        strategy, region = self._seedOptions()
        
        self.ui.regionComboBox.blockSignals(True)
        self.ui.regionComboBox.currentText = region
        self.ui.regionComboBox.blockSignals(False)
        
        self.ui.regionComboBox.enabled = strategy == "myocardium"
        self.ui.bandWidthSliderWidget.enabled = region == "band"
        self.ui.thresholdSliderWidget.enabled = strategy == "sheetness"
            
    def onLoadSeedButton(self) -> None:
        strategy, region = self._seedOptions()
        with slicer.util.tryWithErrorDisplay("Failed to generate the seed.", waitCursor=True):
            self.logic.generateSeed(SeedSettings(
                strategy=strategy,
                region=region,
                d_mm=self.ui.bandWidthSliderWidget.value,
                threshold=self.ui.thresholdSliderWidget.value,
            ))
            self.ui.planesCollapsibleButton.enabled = True
            self.ui.planesCollapsibleButton.collapsed = False
    
    # --- PLANE ESEMÉNYKEZELŐK ---
    
    def _w(self, outflow, name):
        """Lekéri a megfelelő widgetet az outflow-prefix és a widgettípus alapján."""
        return getattr(self.ui, f"{outflow}{name}")

    def _plane(self, o) -> OutflowPlane:
        """A logikából lekéri az adott kiáramlási szekcióhoz tartozó OutflowPlane objektumot."""
        # A widget és a logika ugyanazokat az lvAorta/rvPa kulcsokat használja.
        return self.logic.planes[o]

    def _setSilently(self, widget, prop, value):
        """ Megakadályozza, hogy a widget állapotának változtatása eseményláncot indítson. """
        widget.blockSignals(True)
        try:
            setattr(widget, prop, value)
        finally:
            widget.blockSignals(False)
            
    def _initPlaneControls(self) -> None:
        """Az új eset betöltésekor inicializálja a plane beállító UI elemeket."""
        # Használat: a onLoadCaseButton eseménykezelőben, az eset sikeres betöltésekor.
        self._searching = {o: None for o in OUTFLOWS} # törli a keresési állapotokat
        for o in OUTFLOWS: # mindkét kiáramláshoz:
            p = self._plane(o) # lekéri a síkállapotot
            slider = self._w(o, "IndexSliderWidget") # lekéri a csúszkát
            slider.minimum = 0 # beállítja a minimumot
            slider.maximum = p.skeleton.max_index if p.skeleton else 0 # beállítja a maximumot a skeleton max_index alapján
            self._refresh(o) # panel frissítése

    def _refresh(self, o):
        """A widget állapotát a logika aktuális síkállapotához igazítja."""
        p = self._plane(o) # A logikától lekéri az aktuális síkállapotot
        searching = self._searching.get(o) # Éppen melyik határ keresése folyik

        # Checkboxok állapota: sík állapotból blokkolással
        self._setSilently(self._w(o, "TiltOverrideCheckBox"), "checked", p.tilt is not None)
        self._setSilently(self._w(o, "LockCheckBox"), "checked", p.locked)

        # Widgetek láthatósága: a kiválasztott módhoz tartozik, a többi widget rejtve marad.
        for name in ALL_WIDGETS: 
            self._w(o, name).visible = name in PLANE_WIDGETS.get(p.mode, set())

        # Widgetek engedélyezése: a sík állapotától függően
        editable = not p.locked
        self._w(o, "ModeComboBox").enabled = editable
        self._w(o, "IndexWidget").enabled = editable and p.skeleton is not None
        self._w(o, "SetUpperButton").enabled = editable and searching == "upper"
        self._w(o, "SetLowerButton").enabled = editable and searching == "lower"
        self._w(o, "TiltWidget").enabled = editable and p.gt_index is not None
        self._w(o, "TiltAngleASliderWidget").enabled = editable and p.tilt is not None
        self._w(o, "TiltAngleBSliderWidget").enabled = editable and p.tilt is not None
        self._w(o, "FreeHandWidget").enabled = editable
        self._w(o, "LockCheckBox").enabled = p.isResolved or p.locked

        # Információs szöveg: a keresés állapota + a sík állapota (mik vannak már beállítva)
        searching_text = f"Searching the {searching} bound · " if searching else ""
        self._w(o, "InfoLabel").text = searching_text + p.describe()
        
        # Előnézeti sík: a csúszka aktuális értékétől függ, ha keresés folyik, egyébként a végleges sík látható.
        shown = (
            p.skeleton.planeAt(self._w(o, "IndexSliderWidget").value)
            if searching and p.skeleton is not None
            else p.plane
        )
        
        self.logic.showPlane(
            o,
            shown,
            freeHand=p.mode == "free hand",
            locked=p.locked,
        )

    def onModeChanged(self, o):
        """A combo box változásának eseménykezelője"""
        p = self._plane(o)
        mode = self._w(o, "ModeComboBox").currentText.lower()
        # Szabadkézi módhoz először biztosítunk egy kiinduló síkot, amit a felhasználó
        # a markup nézetben elmozdíthat.
        if mode == "free hand" and p.free_hand is None:
            self.logic.placePlaneNode(
                o,
                p.plane or self.logic.defaultPlane(o),
            )
        p.mode = mode
        self._searching[o] = None
        self._planeChanged(o)

    def onStartAt(self, o, which):
        """A Start at Max/Min gomb eseménykezelője: a csúszka mozgatása a skeleton határához."""
        p = self._plane(o)
        p.tilt = None  # a dőlés a korábbi GT-indexhez tartozott
        # A keresés csak előnézetet mozgat; a határ csak a Set gomb megnyomásakor rögzül.
        self._searching[o] = which
        self._w(o, "IndexSliderWidget").value = p.skeleton.max_index if which == "upper" else 0
        self._planeChanged(o)

    def onIndexSliderChanged(self, o):
        """A csúszka mozgatásának eseménykezelője, ha éppen keresés folyik, frissíti az előnézetet."""
        if self._searching.get(o):
            # A csúszka mozgatása nem vágja újra a seedet, csak a jelölt síkot frissíti.
            self._refresh(o)  # csak az előnézet mozog, a seed nem változik

    def onSetBound(self, o, which):
        """A Set Upper/Lower gomb eseménykezelője: a csúszka aktuális értékét rögzíti a sík állapotában."""
        with slicer.util.tryWithErrorDisplay("Could not set the bound."):
            # A rögzített határ az OutflowPlane állapotába kerül.
            self._plane(o).setBound(which, int(self._w(o, "IndexSliderWidget").value))
        self._searching[o] = None # A keresés befejeződött
        self._planeChanged(o)

    def onResetPlane(self, o):
        """A Reset gomb eseménykezelője: a sík állapotát visszaállítja alapállapotba, nincsen keresés, frissíti a felületet, újravágja."""
        # A reset csak a mentett síkparamétereket törli; a seed újravágása
        # az eseménykezelő közös útján történik.
        self._plane(o).reset()
        self._searching[o] = None
        self._planeChanged(o)

    def onTiltChanged(self, o):
        """A TiltOverrideCheckBox és a két dőléscsúszka eseménykezelője: a sík állapotát frissíti, újravágja a seedet."""
        on = self._w(o, "TiltOverrideCheckBox").checked
        self._plane(o).tilt = (self._w(o, "TiltAngleASliderWidget").value,
                            self._w(o, "TiltAngleBSliderWidget").value) if on else None
        self._planeChanged(o) # Az újravágás miatt néha lassú lehet.

    def onFreeHandApply(self, o):
        """A Free-Hand Apply gomb eseménykezelője: a Slicer markup aktuális helyzetét végleges, RAS-alapú Plane-ként mentjük."""
        self._plane(o).free_hand = self.logic.readPlaneNode(o)
        self._planeChanged(o)

    def onLockToggled(self, o, checked):
        """A LockCheckBox eseménykezelője: a sík állapotát zárolja, ha érvényes sík esetén zárolja vagy feloldja a síkot."""
        p = self._plane(o)
        # Csak feloldott és érvényes sík zárható le, 
        # a markup interakciója ilyenkor azonnal letiltódik.
        if checked and not p.isResolved:
            slicer.util.warningDisplay("Set both bounds (or apply a free-hand plane) before locking.")
        else:
            p.locked = checked
        self._refresh(o)  # a refresh visszaírja a pipát az állapotból

    def _planeChanged(self, o):
        """A jelenlegi plane alapján újravágja a seedet és frissíti a felületet."""
        # Minden végleges módosítás után a seedet minden aktív sík metszetével frissítjük.
        self.logic.recutSeed()  # seed = alap ∩ minden végleges sík alatti tartomány
        self._refresh(o)



import numpy as np
from dataclasses import dataclass

def lps_to_ras(v):
    """LPS -> RAS koordináta transzformáció (X és Y tengely tükrözése)."""
    return [-float(v[0]), -float(v[1]), float(v[2])]

def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n == 0:
        raise ValueError("zero-length vector")
    return v / n
            
#
# SeptumAnnotatorLogic
#

def belowPlaneMask(volumeNode, plane: Plane, bounds=None) -> np.ndarray:
    """Bool maszk (k, j, i) sorrendben: True a sík kamra felőli oldalán lévő voxeleknél.

    Feltételezi, hogy plane.normal egységvektor, és az artéria felé mutat.
    bounds: opcionális (k_slice, j_slice, i_slice), ekkor csak ezt a részt számolja 
    és a kimenet alakja a bounds alakja.
    """
    import vtk

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

    ijk_to_ras = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(ijk_to_ras)
    matrix = slicer.util.arrayFromVTKMatrix(ijk_to_ras)
    coefficients = matrix[:3, :3].T @ plane.normal       # (ci, cj, ck)
    constant = (matrix[:3, 3] - plane.point) @ plane.normal

    # A VTK dimenziói (i, j, k) sorrendűek, a numpy-maszk tengelyei (k, j, i).
    ni, nj, nk = volumeNode.GetImageData().GetDimensions()
    if bounds is None:
        bounds = (slice(0, nk), slice(0, nj), slice(0, ni))
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


class SeptumAnnotatorLogic(ScriptedLoadableModuleLogic):
    case: CaseData
    seed: SeedData
    
    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        self.case = None
        self.seed = None
        self.planes = None
        self.planeNodes = {} # A kiáramlási síkok markup-nodjainak tárolása, hogy a felhasználó által elmozdított síkokat is tudjuk olvasni.
        self.seedBaseMask = None
        
    # --- CASE LOGIKA ---

    def processCase(self, ct_path: str, segmentation_path: str, label_config_path: str, lv_skel: str, rv_skel: str, generate_skeletons: bool, case_id : str = "") -> None:
        """Betölti a CT-t és a szegmentációt, és megjeleníti őket."""
        
        new_case  = CaseData(case_id=case_id or os.path.basename(ct_path),
                               ct_path=ct_path,
                               seg_path=segmentation_path,
                               label_config_path=label_config_path,
                               skeleton_lv_aorta_path=lv_skel,
                               skeleton_rv_pa_path=rv_skel,
                               generate_skeleton=generate_skeletons)

        if self.case is not None:  # az előző esetet csak sikeres betöltés után dobjuk el
            self.case.removeNodes()
        self.case = new_case
        self.seed = SeedData(self.case)  # egy seed esetenként
        # A síkállapot az esethez tartozik, ezért seed-paraméter módosításakor
        # nem szabad új OutflowPlane objektumokkal felülírni.
        self.seedBaseMask = None
        # Minden kiáramlási szekció saját, tartós döntési állapotot kap.
        self.planes = {
            outflow: OutflowPlane(skeleton=self.case.skeletons.get(outflow))
            for outflow in OUTFLOWS
        }
        self.planeNodes = {}
        # A sík-node-ok csak egyszer jönnek létre az eset betöltésekor.
        self._ensurePlaneNodes()
        
        # Szegmensek megjelenítésének beállítása: 2D-ben 40% kitöltés, 60% kontúr, csak a kötelező struktúrák látszanak
        disp = self.case.segmentation.GetDisplayNode()
        disp.SetAllSegmentsOpacity2DFill(0.4)
        disp.SetAllSegmentsOpacity2DOutline(0.6)
        disp.SetAllSegmentsVisibility(False)
        for name in REQUIRED_STRUCTURES:
            disp.SetSegmentVisibility(self.case.segment_ids[name], True)
    
        # Skeleton-ok megjelenítése, eleve RAS koordinátákban, a fájlokban LPS-ben vannak tárolva
        # Generálási módban a skeletons szótár üres lehet, ezért csak a meglévőket rajzoljuk ki.
        for outflow, name in (("lvAorta", "Skeleton_lv_aorta"), ("rvPa", "Skeleton_rv_pa")):
            skeleton = self.case.skeletons.get(outflow)
            if skeleton is not None:
                self.case.track(self._addPolyline(name, skeleton.points))

        # Slicer nézetek frissítése 
        slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        slicer.util.setSliceViewerLayers(background=self.case.ct, label=None, fit=True)
        self.case.segmentation.CreateClosedSurfaceRepresentation()
        slicer.util.resetThreeDViews()
        
        print(f"Case loaded: {os.path.basename(ct_path)} "
                     f"(Generate Skeletons: {generate_skeletons})")
        
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
    
    # --- SEED LOGIKA ---S

    def generateSeed(self, settings: SeedSettings) -> None:
        if settings.strategy == "myocardium":
            base = self.case.mask("myo")   # tárolt
            region = self.case.hull() if settings.region == "hull" else self._band(settings.d_mm)
            mask = base & region
        elif settings.strategy == "sheetness":
            base = self.case.sheetness() > settings.threshold       # a térkép tárolt, a küszöb gyors
            mask = base & self._band(settings.d_mm)
        else:
            raise ValueError(f"Unknown seed strategy: {settings.strategy}")
        # Ezt a síkoktól függetlenül megőrizzük, hogy minden újravágás innen
        # induljon, ne az előző vágás eredményét vágjuk tovább.
        self.seedBaseMask = mask
        
        # A már beállított síkokat változatlanul hagyjuk, és az új alapmaszkot
        # ugyanazokkal a síkokkal vágjuk újra.
        self.seed.settings = settings
        self.recutSeed()
    
    def _band(self, d_mm: float) -> np.ndarray:
        return (self.case.distanceMap("lv") <= d_mm) & (self.case.distanceMap("rv") <= d_mm)

    # --- PLANE LOGIKA ---
    
    def defaultPlane(self, key) -> Plane:
        """A skeleton utolsó pontjánál létrehoz egy alapértelmezett síkot, az adott kiáramlási szekcióhoz."""
        # Használat: az onModeChanged eseménykezlőben, mikor free-hand módba váltunk, de még nincs korábban elmentett free-hand sík.
        skeleton = self.planes[key].skeleton
        if skeleton is None:
            raise ValueError(f"No skeleton is available for {key}.")
        return skeleton.planeAt(skeleton.max_index)
    
    def placePlaneNode(self, key, plane):
        """A kapott Plane objektum alapján beállítja a Slicer markup node-ot"""
        # Használat: az onModeChanged eseménykezlőben, a defaultPlane által visszaadott síkkal.
        node = self.planeNodes[key]
        node.SetCenterWorld(plane.point)
        node.SetNormalWorld(plane.normal)
        node.SetDisplayVisibility(True)
        node.SetLocked(False)
        
    def readPlaneNode(self, key) -> Plane:
        """A Slicer markup node-ot egy Plane objektummá alakítja."""
        # Használat: az onFreeHandApply eseménykezelőben.
        node = self.planeNodes[key]
        point = np.zeros(3)
        normal = np.zeros(3)
        node.GetCenterWorld(point)
        node.GetNormalWorld(normal)
        return Plane(point, _unit(normal))
    
    def showPlane(self, o, plane, freeHand, locked):
        """A markup sík node-ot a megadott síkra állítja, és a felhasználói interakciót engedélyezi vagy tiltja."""
        # Használat: az összes plane eseménykezelőben.
        node = self.planeNodes[o]
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
            if key in self.planeNodes: # Létezik már? Ha igen, akkor nem kell létrehozni.
                continue
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsPlaneNode", name) # Slicer plane létrehozása
            node.CreateDefaultDisplayNodes() # Alap beállítások
            node.SetDisplayVisibility(False) # Egyelőre nem látszódik
            # A node csak a sík vizuális megjelenítését végzi,
            # a tényleges állapot az OutflowPlane objektumban marad.
            self.case.track(node) # Case nyilvántartja központilag
            self.planeNodes[key] = node # A logika is tárolja, hogy a felület eseménykezelői könnyedén elérjék kulccsal.

    def recutSeed(self) -> None:
        """Az alap-seedet levágja, hogy a végleges síkok alatti tartomány maradjon."""
        if self.seed is None or self.seed.settings is None or self.seedBaseMask is None:
            return

        mask = self.seedBaseMask.copy()
        # A síkteszt csak a seed ténylegesen elfoglalt bounding boxán fut.
        # Ez a teljes CT-rács helyett jellemzően csak kis részterületet jelent.
        occupied = np.any(mask, axis=(1, 2)), np.any(mask, axis=(0, 2)), np.any(mask, axis=(0, 1))
        occupied_indices = [np.flatnonzero(axis) for axis in occupied]
        if not all(len(indices) for indices in occupied_indices):
            # Üres alapseednél nincs vizsgálható bounding box, 
            # de a seed node-ot így is frissíteni kell.
            self.seed.update(self.seed.settings, mask)
            return

        bounds = tuple(
            slice(int(indices[0]), int(indices[-1]) + 1)
            for indices in occupied_indices
        )
        for state in (self.planes or {}).values():
            plane = state.plane
            if plane is None:
                continue
            # A síkok metszete a seed bounding boxán belül, az eredeti maszkkal.
            mask[bounds] &= belowPlaneMask(self.case.ct, plane, bounds)

        self.seed.update(self.seed.settings, mask)

#
# SeptumAnnotatorTest
#

class SeptumAnnotatorTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_placeholder()

    def test_placeholder(self):
        self.delayDisplay("No tests yet")
