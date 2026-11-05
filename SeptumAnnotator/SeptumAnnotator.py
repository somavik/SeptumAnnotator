import os
import importlib
import sys

# Fejlesztés: a modul újratöltésekor a Lib-et is töltsük újra (függőségi sorrendben).
# -> Ha a Lib-ben változtatok, akkor elég a modult újratölteni, nem kell a Slicert újraindítani, hogy a változások érvényesüljenek.
for _name in ("geometry", "labels", "skeleton", "outflow", "seed", "case", "scene"):
    _module = sys.modules.get(f"SeptumAnnotatorLib.{_name}")
    if _module is not None:
        importlib.reload(_module)

from SeptumAnnotatorLib.geometry import Plane
from SeptumAnnotatorLib.outflow import OutflowPlane, OUTFLOWS
from SeptumAnnotatorLib.labels import REQUIRED_STRUCTURES
from SeptumAnnotatorLib.seed import SeedSettings, cutBelowPlanes
from SeptumAnnotatorLib.case import CaseData
from SeptumAnnotatorLib.scene import PlaneNodes, SeptumData

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin


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

# A combo boxban kiválasztott érték határozza meg, melyik panelcsoport látható.
PLANE_WIDGETS = {
    "skeleton": {"IndexWidget", "TiltWidget"},
    "free hand": {"FreeHandWidget"},
    "no connection": set(),
}
# Ezekből állnak össze a konkrét UI-nevek.
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

        # for path_picker, setting_key in (
        #     (self.ui.ctPathLineEdit, "SeptumAnnotator/ctPath"),
        #     (self.ui.segmentationPathLineEdit, "SeptumAnnotator/segmentationPath"),
        #     (self.ui.labelConfigPathLineEdit, "SeptumAnnotator/labelConfigPath"),
        #     (self.ui.lvAortaSkeletonPathLineEdit, "SeptumAnnotator/lvAortaSkeletonPath"),
        #     (self.ui.rvPaSkeletonPathLineEdit, "SeptumAnnotator/rvPaSkeletonPath"),
        # ):
        #     path_picker.settingKey = setting_key

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
        self.ui.loadSeedButton.clicked.connect(lambda: self.onLoadSeed(resetEdits=True))
        self.ui.seedCollapsibleButton.enabled = False
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
        
        # Eseménykezelők bekötése (Connections)
        self.editor = self.ui.correctionSegmentEditorWidget
        self.editor.setMRMLScene(slicer.mrmlScene)
        # Egy editor node elég; újratöltéskor a meglévőt használjuk újra.
        self.editorNode = slicer.mrmlScene.GetFirstNodeByName("SeptumAnnotatorEditor") or \
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "SeptumAnnotatorEditor")
        self.editor.setMRMLSegmentEditorNode(self.editorNode)  # enélkül az editor inaktív marad
        self.editor.setEffectNameOrder(["Islands", "Erase", "Paint", "Smoothing", "Scissors"])
        self.editor.unorderedEffectsVisible = False  # csak a felsorolt effektek látszanak
        
        # Eseménykezelők bekötése (Export)
        self.ui.exportPushButton.clicked.connect(self.onExportClicked)

        self.ui.caseCollapsibleButton.collapsed = False
        
        self.ui.seedCollapsibleButton.enabled = False
        self.ui.seedCollapsibleButton.collapsed = True
        
        self.ui.planesCollapsibleButton.enabled = False
        self.ui.planesCollapsibleButton.collapsed = True
        
        self.ui.correctionCollapsibleButton.enabled = False 
        self.ui.correctionCollapsibleButton.collapsed = True
        
        self.ui.exportPushButton.enabled = False
        
        self._setDefaultValues()

    def cleanup(self):
        """Újratöltéskor és kilépéskor fut: az eset node-jait töröljük,
        különben az új logic nem tudna róluk, és a jelenetben maradnának."""
        if self.logic is not None and self.logic.case is not None:
            self.logic.case.removeNodes()
        self.removeObservers()

    
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
        print(f"[DEBUG] Checking skeleton paths: generateSkeleton={self.ui.generateSkeletonCheckBox.checked}, lvPath={self.ui.lvAortaSkeletonPathLineEdit.currentPath}, rvPath={self.ui.rvPaSkeletonPathLineEdit.currentPath}")
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
        print("[DEBUG] Loading case with parameters: ctPath={self.ui.ctPathLineEdit.currentPath}, segmentationPath={self.ui.segmentationPathLineEdit.currentPath}, labelConfigPath={self.ui.labelConfigPathLineEdit.currentPath}, generateSkeletons={self.ui.generateSkeletonCheckBox.checked}, caseId={self.ui.caseIdLineEdit.text}")
        lv, rv = self._skeletonPaths()
        with slicer.util.tryWithErrorDisplay("Failed to process the case.", waitCursor=True):
            self.logic.processCase(self.ui.ctPathLineEdit.currentPath,
                            self.ui.segmentationPathLineEdit.currentPath,
                            self.ui.labelConfigPathLineEdit.currentPath,
                            lv, rv,
                            generate_skeletons=self.ui.generateSkeletonCheckBox.checked,
                            case_id=self.ui.caseIdLineEdit.text)
            self._initPlaneControls()
            self.ui.seedCollapsibleButton.enabled = True
            self.ui.seedCollapsibleButton.collapsed = False
            
            self.ui.planesCollapsibleButton.enabled = False
            self.ui.planesCollapsibleButton.collapsed = True
                    
            self.ui.correctionCollapsibleButton.enabled = False 
            self.ui.correctionCollapsibleButton.collapsed = True
                    
            self.ui.exportPushButton.enabled = False
        
    # --- SEED ESEMÉNYKEZELŐK ---
        
    def onSeedParameterChanged(self, *args):
        septum = self.logic.septum
        if septum is None or septum.base is None:  # még nem nyomták meg a Load Seed-et
            return
        self.onLoadSeed(resetEdits=False)
    
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
            
    def onLoadSeed(self, resetEdits: bool = False) -> None:
        septum = self.logic.septum
        if resetEdits and septum.hasEdits():
            if not slicer.util.confirmYesNoDisplay("Reloading the seed discards your corrections. Continue?"):
                return
        strategy, region = self._seedOptions()
        with slicer.util.tryWithErrorDisplay("Failed to generate the seed.", waitCursor=True):
            self.logic.generateSeed(SeedSettings(
                strategy=strategy,
                region=region,
                d_mm=self.ui.bandWidthSliderWidget.value,
                threshold=self.ui.thresholdSliderWidget.value,
            ), 
                resetEdits=resetEdits)
            if resetEdits:  # csak a gombnál
                self.ui.planesCollapsibleButton.enabled = True
                self.ui.planesCollapsibleButton.collapsed = False
            
            self.ui.correctionCollapsibleButton.enabled = True  
            self.ui.correctionCollapsibleButton.collapsed = False
            self.editor.setSegmentationNode(self.logic.septum.node)
            self.editor.setSourceVolumeNode(self.logic.case.ct)
            self.editor.setCurrentSegmentID(self.logic.septum.segmentId)
            
            self.ui.exportPushButton.enabled = True
            
    
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
        
        self.logic.scene.showPlane(
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
            self.logic.scene.placePlaneNode(
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
        self._plane(o).free_hand = self.logic.scene.readPlaneNode(o)
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
    
    # --- EXPORT ESEMÉNYKEZELŐK ---
    
    def onExportClicked(self):
        """Az export gomb eseménykezelője: elmenti a szeptumot a felhasználó által kiválasztott útvonalra."""
        if not self.logic.case or not self.logic.septum:
            return

        # Kézi szerkesztések érvényesítése 
        self.logic.septum.syncEdits()

        case_id = self.logic.case.case_id or "unknown"
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(output_dir, exist_ok=True)
        
        file_path = os.path.join(output_dir, f"septum_{case_id}.nii.gz")

        with slicer.util.tryWithErrorDisplay("Failed to export septum.", waitCursor=True):
            # 2. Szegmentáció konvertálása LabelMap-pé a biztonságos NIfTI mentéshez
            temp_labelmap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "temp_export")
            slicer.modules.segmentations.logic().ExportVisibleSegmentsToLabelmapNode(
                self.logic.septum.node, 
                temp_labelmap, 
                self.logic.case.ct  # A CT biztosítja a rácsot és a pontos NIfTI fejlécet
            )

            # Fájl kimentése a háttértárra
            success = slicer.util.saveNode(temp_labelmap, filename=file_path)
            
            # Ideiglenes csomópont feltakarítása a memóriából
            slicer.mrmlScene.RemoveNode(temp_labelmap)

            if success:
                slicer.util.infoDisplay(f"Septum exported to {file_path}.")
            else:
                slicer.util.errorDisplay("Mentés nem sikerült a Slicer saveNode hívásakor.")


import numpy as np

#
# SeptumAnnotatorLogic
#

class SeptumAnnotatorLogic(ScriptedLoadableModuleLogic):
    case: CaseData
    septum: SeptumData
    
    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        self.case = None
        self.septum = None
        self.planes = None
        self.scene = None        
    # --- CASE LOGIKA ---

    def processCase(self, ct_path: str, segmentation_path: str, label_config_path: str, lv_skel: str, rv_skel: str, generate_skeletons: bool, case_id : str = "") -> None:
        """Betölti a CT-t és a szegmentációt, és megjeleníti őket."""
        print(f"[DEBUG] Processing case: ct_path={ct_path}, segmentation_path={segmentation_path}, label_config_path={label_config_path}, lv_skel={lv_skel}, rv_skel={rv_skel}, generate_skeletons={generate_skeletons}, case_id={case_id}")
        new_case  = CaseData(case_id=case_id or os.path.basename(ct_path).split(".")[0],
                               ct_path=ct_path,
                               seg_path=segmentation_path,
                               label_config_path=label_config_path,
                               skeleton_lv_aorta_path=lv_skel,
                               skeleton_rv_pa_path=rv_skel,
                               generate_skeleton=generate_skeletons)

        if self.case is not None:  # az előző esetet csak sikeres betöltés után dobjuk el
            self.case.removeNodes()
        self.case = new_case
        self.septum = SeptumData(self.case)  # egy seed esetenként
        # A síkállapot az esethez tartozik, ezért seed-paraméter módosításakor
        # nem szabad új OutflowPlane objektumokkal felülírni.
        # Minden kiáramlási szekció saját, tartós döntési állapotot kap.
        self.planes = {
            outflow: OutflowPlane(skeleton=self.case.skeletons.get(outflow))
            for outflow in OUTFLOWS
        }
        self.scene = PlaneNodes(self.case)
        # A sík-node-ok csak egyszer jönnek létre az eset betöltésekor.
        self.scene._ensurePlaneNodes()
        
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
                self.case.track(self.scene._addPolyline(name, skeleton.points))

        # Slicer nézetek frissítése 
        slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        slicer.util.setSliceViewerLayers(background=self.case.ct, label=None, fit=True)
        self.case.segmentation.CreateClosedSurfaceRepresentation()
        slicer.util.resetThreeDViews()
        
        print(f"Case loaded: {os.path.basename(ct_path)} "
                     f"(Generate Skeletons: {generate_skeletons})")
        
    # --- SEED LOGIKA ---

    def generateSeed(self, settings: SeedSettings, resetEdits: bool) -> None:
        if settings.strategy == "myocardium":
            strategy = self.case.mask("myo")   # tárolt
            region = self.case.hull() if settings.region == "hull" else self._band(settings.d_mm)
            base = strategy & region
        elif settings.strategy == "sheetness":
            strategy = self.case.sheetness() > settings.threshold       # a térkép tárolt, a küszöb gyors
            base = strategy & self._band(settings.d_mm)
        else:
            raise ValueError(f"Unknown seed strategy: {settings.strategy}")
        
        self.septum.setBase(base, resetEdits=resetEdits)
        
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
    
    def recutSeed(self) -> None:
        """Az alap-seedet levágja, hogy a végleges síkok alatti tartomány maradjon."""
        if self.septum  is None or self.septum.base is None:
            return
        
        self.septum.syncEdits()

        planes = [p.plane for p in self.planes.values() if p.plane is not None]
        self.septum.compose(self.case.ijkToRas(), planes)
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
