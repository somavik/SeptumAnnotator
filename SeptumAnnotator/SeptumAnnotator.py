"""SeptumAnnotator: semi-automatic ventricular-septum annotation (3D Slicer scripted module).

The computations live in SeptumAnnotatorLib (no Slicer dependency); this file is the
GUI and the conversion between numpy arrays and Slicer nodes.
"""

import importlib
import logging
import os

import ctk
import numpy as np
import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)
from slicer.util import VTKObservationMixin

import SeptumAnnotatorLib
from SeptumAnnotatorLib import config as cfg
from SeptumAnnotatorLib import manifest, planes, session

# Submodules in dependency order, for "Reload" during development.
_LIB_MODULES = ("config", "imaging", "manifest", "seed", "planes", "openings", "session")

STRUCTURE_COLORS = {
    "lv": (0.85, 0.25, 0.25), "rv": (0.25, 0.45, 0.9), "la": (0.95, 0.6, 0.6), "ra": (0.6, 0.75, 1.0),
    "myo": (0.85, 0.75, 0.45), "aorta": (0.95, 0.35, 0.0), "pa": (0.2, 0.75, 0.95),
}
SEPTUM_COLOR = (0.2, 0.9, 0.3)
OPENING_COLOR = (1.0, 0.0, 1.0)
OUTFLOW_TITLES = {"lv_aorta": "LV – aorta", "rv_pa": "RV – PA"}


def lps_to_ras(v):
    return [-float(v[0]), -float(v[1]), float(v[2])]


ras_to_lps = lps_to_ras  # the flip is its own inverse


#
# Module
#


class SeptumAnnotator(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "Septum Annotator"
        self.parent.categories = ["Segmentation"]
        self.parent.dependencies = ["Segmentations", "Markups"]
        self.parent.contributors = ["Soma Szolár (BME MIT)"]
        self.parent.helpText = (
            "Semi-automatic annotation of the interventricular septum: automatic seed, "
            "skeleton-constrained outflow planes, correction and hole QA. See README.md."
        )
        self.parent.acknowledgementText = (
            "Developed at the Department of Artificial Intelligence and Systems Engineering, "
            "Budapest University of Technology and Economics."
        )


#
# Logic: scene <-> arrays
#


class SeptumAnnotatorLogic(ScriptedLoadableModuleLogic):
    """Owns the CaseSession and every node it puts into the scene."""

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.session = None
        self.nodes = {}
        self.planeNodes = {}

    # ---------------------------------------------------------------- loading
    def loadCase(self, case, d_mm, sheetness_threshold):
        self.clear()
        import sitkUtils

        self.session = session.CaseSession(case, d_mm=d_mm, sheetness_threshold=sheetness_threshold)
        s = self.session

        self.nodes["ct"] = slicer.util.loadVolume(case.ct_path, {"name": f"{case.case_id}_CT", "show": False})
        self.nodes["work"] = sitkUtils.PushVolumeToSlicer(s.ct, name=f"{case.case_id}_CT_1mm")
        self.nodes["work"].SetHideFromEditors(False)

        gt = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{case.case_id}_structures")
        gt.SetReferenceImageGeometryParameterFromVolumeNode(self.nodes["work"])
        gt.CreateDefaultDisplayNodes()
        for name, mask in s.structures.items():
            if not mask.any():
                continue
            sid = gt.GetSegmentation().AddEmptySegment(name, name, STRUCTURE_COLORS.get(name, (0.7, 0.7, 0.7)))
            slicer.util.updateSegmentBinaryLabelmapFromArray(mask.astype(np.uint8), gt, sid, self.nodes["work"])
        disp = gt.GetDisplayNode()
        disp.SetAllSegmentsOpacity2DFill(0.0)
        disp.SetAllSegmentsOpacity2DOutline(0.6)
        disp.SetAllSegmentsVisibility(False)
        for name in ("lv", "rv", "aorta", "pa"):
            if gt.GetSegmentation().GetSegment(name):
                disp.SetSegmentVisibility(name, True)
        self.nodes["structures"] = gt

        if case.mitral_path:
            self.nodes["mitral"] = slicer.util.loadSegmentation(case.mitral_path, {"name": f"{case.case_id}_mitral"})

        sep = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{case.case_id}_septum")
        sep.SetReferenceImageGeometryParameterFromVolumeNode(self.nodes["work"])
        sep.CreateDefaultDisplayNodes()
        self.septumId = sep.GetSegmentation().AddEmptySegment("septum", "septum", SEPTUM_COLOR)
        sep.GetDisplayNode().SetOpacity2DFill(0.45)
        self.nodes["septum"] = sep

        qa = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{case.case_id}_opening")
        qa.SetReferenceImageGeometryParameterFromVolumeNode(self.nodes["work"])
        qa.CreateDefaultDisplayNodes()
        self.openingId = qa.GetSegmentation().AddEmptySegment("opening", "opening", OPENING_COLOR)
        qa.GetDisplayNode().SetOpacity2DFill(0.8)
        self.nodes["opening"] = qa

        for outflow, state in s.planes.items():
            if state.skeleton is not None:
                self.nodes[f"skel_{outflow}"] = self._addPolyline(f"{case.case_id}_skeleton_{outflow}",
                                                                  state.skeleton.points)
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsPlaneNode", f"{outflow}_plane")
            node.CreateDefaultDisplayNodes()
            node.SetSizeWorld(70, 70) if hasattr(node, "SetSizeWorld") else node.SetSize(70, 70)
            node.GetDisplayNode().SetSelectedColor(STRUCTURE_COLORS["aorta" if outflow == "lv_aorta" else "pa"])
            node.GetDisplayNode().SetOpacity(0.35)
            node.SetLocked(True)
            node.SetDisplayVisibility(False)
            self.planeNodes[outflow] = node
            self.nodes[f"plane_{outflow}"] = node

        slicer.util.setSliceViewerLayers(background=self.nodes["ct"], fit=True)
        self.pushMask()
        sep.CreateClosedSurfaceRepresentation()
        return s

    def _addPolyline(self, name, points_lps):
        pts = vtk.vtkPoints()
        for p in points_lps:
            pts.InsertNextPoint(lps_to_ras(p))
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(len(points_lps))
        for i in range(len(points_lps)):
            line.GetPointIds().SetId(i, i)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(cells)
        model = slicer.modules.models.logic().AddModel(poly)
        model.SetName(name)
        model.GetDisplayNode().SetColor(1, 1, 0)
        model.GetDisplayNode().SetLineWidth(3)
        model.GetDisplayNode().SetVisibility2D(True)
        return model

    def clear(self):
        for node in self.nodes.values():
            if node is not None and slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        self.nodes, self.planeNodes, self.session = {}, {}, None

    # ---------------------------------------------------------------- mask
    def currentMask(self):
        arr = slicer.util.arrayFromSegmentBinaryLabelmap(self.nodes["septum"], self.septumId, self.nodes["work"])
        return arr > 0

    def syncEdits(self):
        if self.session is not None:
            self.session.sync_edits(self.currentMask())

    def pushMask(self):
        final = self.session.final_mask()
        slicer.util.updateSegmentBinaryLabelmapFromArray(final.astype(np.uint8), self.nodes["septum"],
                                                         self.septumId, self.nodes["work"])
        return final

    def recompute(self, **kwargs):
        """Keep the user's edits, recompute the seed, show the new mask."""
        self.syncEdits()
        self.session.update(**kwargs)
        return self.pushMask()

    # ---------------------------------------------------------------- planes
    def showPlane(self, outflow, plane):
        node = self.planeNodes.get(outflow)
        if node is None:
            return
        if plane is None:
            node.SetDisplayVisibility(False)
            return
        node.SetCenterWorld(lps_to_ras(plane[0]))
        node.SetNormalWorld(lps_to_ras(plane[1]))
        node.SetDisplayVisibility(True)

    def freeHandFromNode(self, outflow):
        node = self.planeNodes[outflow]
        c, n = [0.0] * 3, [0.0] * 3
        node.GetCenterWorld(c)
        node.GetNormalWorld(n)
        return ras_to_lps(c), ras_to_lps(n)

    # ---------------------------------------------------------------- openings
    def showOpening(self, opening):
        mask = np.zeros(self.session.lv.shape, dtype=np.uint8)
        if opening is not None:
            mask.flat[opening.voxels] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, self.nodes["opening"], self.openingId,
                                                         self.nodes["work"])
        if opening is not None:
            r, a, s_ = lps_to_ras(opening.centroid_lps)
            slicer.modules.markups.logic().JumpSlicesToLocation(r, a, s_, True)


#
# Widget
#


class SeptumAnnotatorWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self.cases = []
        self.manifest = None
        self.grind = False
        self.exported = True
        self.planeUi = {}
        self.currentOpening = None
        self._updating = False

    # ================================================================ setup
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = SeptumAnnotatorLogic()
        self._setupCaseSection()
        self._setupSeedSection()
        self._setupPlaneSection()
        self._setupEditSection()
        self._setupOpeningSection()
        self._setupFinishSection()
        self.layout.addStretch(1)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self._setCaseLoaded(False)

    def _section(self, title, collapsed=False):
        box = ctk.ctkCollapsibleButton()
        box.text = title
        box.collapsed = collapsed
        self.layout.addWidget(box)
        return box, qt.QFormLayout(box)

    @staticmethod
    def _path(filters, name, dirs=False):
        w = ctk.ctkPathLineEdit()
        w.filters = (ctk.ctkPathLineEdit.Dirs if dirs else ctk.ctkPathLineEdit.Files)
        if filters:
            w.nameFilters = filters
        w.settingKey = f"SeptumAnnotator/{name}"
        return w

    @staticmethod
    def _combo(values):
        c = qt.QComboBox()
        c.addItems(list(values))
        return c

    def _setupCaseSection(self):
        box, form = self._section("1. Case")
        self.caseBox = box
        modeRow = qt.QHBoxLayout()
        self.grindRadio = qt.QRadioButton("Grind (config table)")
        self.customRadio = qt.QRadioButton("Custom (single case)")
        self.grindRadio.checked = True
        modeRow.addWidget(self.grindRadio)
        modeRow.addWidget(self.customRadio)
        form.addRow(modeRow)
        self.outputDir = self._path(None, "outputDir", dirs=True)
        form.addRow("Output folder:", self.outputDir)

        self.grindWidget = qt.QWidget()
        g = qt.QFormLayout(self.grindWidget)
        g.setContentsMargins(0, 0, 0, 0)
        self.configPath = self._path(["Case tables (*.csv *.xlsx)"], "configPath")
        g.addRow("Config table:", self.configPath)
        self.startButton = qt.QPushButton("Start / resume")
        g.addRow(self.startButton)
        form.addRow(self.grindWidget)

        self.customWidget = qt.QWidget()
        c = qt.QFormLayout(self.customWidget)
        c.setContentsMargins(0, 0, 0, 0)
        self.customCaseId = qt.QLineEdit()
        self.customCt = self._path(["Images (*.nii *.nii.gz *.nrrd *.mha)"], "customCt")
        self.customSeg = self._path(["Images (*.nii *.nii.gz *.nrrd *.mha)"], "customSeg")
        self.customSegSource = self._combo(cfg.SEG_SOURCES)
        self.customLabelMap = self._combo(cfg.LABEL_MAPS)
        self.customStrategy = self._combo(cfg.SEED_STRATEGIES)
        self.customSheetness = self._path(["Images (*.nii *.nii.gz *.nrrd *.mha)"], "customSheetness")
        self.customSkeletons = {o: self._path(["Skeleton (*.npz)"], f"customSkeleton_{o}") for o in cfg.OUTFLOWS}
        self.customMitral = self._path(["Images (*.nii *.nii.gz *.nrrd *.seg.nrrd)"], "customMitral")
        c.addRow("Case ID:", self.customCaseId)
        c.addRow("CT:", self.customCt)
        c.addRow("Segmentation:", self.customSeg)
        c.addRow("Segmentation source:", self.customSegSource)
        c.addRow("Label map:", self.customLabelMap)
        c.addRow("Seed strategy:", self.customStrategy)
        c.addRow("Sheetness map (Plan B):", self.customSheetness)
        for o, w in self.customSkeletons.items():
            c.addRow(f"Skeleton {OUTFLOW_TITLES[o]}:", w)
        c.addRow("Mitral valve (optional):", self.customMitral)
        self.loadCustomButton = qt.QPushButton("Load case")
        c.addRow(self.loadCustomButton)
        form.addRow(self.customWidget)

        self.caseLabel = qt.QLabel("No case loaded.")
        self.caseLabel.wordWrap = True
        form.addRow(self.caseLabel)

        self.grindRadio.connect("toggled(bool)", self._onModeChanged)
        self.startButton.connect("clicked()", self.onStartGrind)
        self.loadCustomButton.connect("clicked()", self.onLoadCustom)
        self._onModeChanged()

    def _setupSeedSection(self):
        box, form = self._section("2. Seed")
        self.seedBox = box
        self.regionCombo = self._combo(cfg.REGIONS)
        form.addRow("Region:", self.regionCombo)
        self.dSlider = ctk.ctkSliderWidget()
        self.dSlider.minimum, self.dSlider.maximum, self.dSlider.singleStep = 0.5, 20.0, 0.5
        self.dSlider.value = cfg.DEFAULTS["d_mm"]
        self.dSlider.suffix = " mm"
        self.dSlider.tracking = False
        form.addRow("Band distance d:", self.dSlider)
        self.tSlider = ctk.ctkSliderWidget()
        self.tSlider.minimum, self.tSlider.maximum, self.tSlider.singleStep, self.tSlider.decimals = 0, 1, 0.01, 2
        self.tSlider.value = cfg.DEFAULTS["sheetness_threshold"]
        self.tSlider.tracking = False
        self.tLabel = qt.QLabel("Sheetness threshold t:")
        form.addRow(self.tLabel, self.tSlider)
        self.seedInfo = qt.QLabel("")
        form.addRow(self.seedInfo)
        self.regionCombo.connect("currentIndexChanged(int)", self.onSeedParams)
        self.dSlider.connect("valueChanged(double)", self.onSeedParams)
        self.tSlider.connect("valueChanged(double)", self.onSeedParams)

    def _setupPlaneSection(self):
        box, layout = self._section("3. Outflow planes (bidirectional search)")
        self.planeBox = box
        help_ = qt.QLabel("1) Start at MAX, slide down until the cut first looks acceptable, Set upper. "
                          "2) Start at MIN, slide up until it first looks acceptable, Set lower. "
                          "The GT index is the midpoint.")
        help_.wordWrap = True
        layout.addRow(help_)
        for outflow in cfg.OUTFLOWS:
            group = qt.QGroupBox(OUTFLOW_TITLES[outflow])
            f = qt.QFormLayout(group)
            ui = {}
            ui["slider"] = ctk.ctkSliderWidget()
            ui["slider"].decimals = 0
            ui["slider"].singleStep = 1
            ui["slider"].tracking = False
            f.addRow("Skeleton index:", ui["slider"])
            row = qt.QHBoxLayout()
            for key, text in (("fromMax", "Start at MAX"), ("setUpper", "Set upper"),
                              ("fromMin", "Start at MIN"), ("setLower", "Set lower"), ("reset", "Reset")):
                ui[key] = qt.QPushButton(text)
                row.addWidget(ui[key])
            f.addRow(row)
            ui["info"] = qt.QLabel("")
            f.addRow(ui["info"])
            ui["tilt"] = qt.QCheckBox("Tilt override")
            ui["tiltA"] = ctk.ctkSliderWidget()
            ui["tiltB"] = ctk.ctkSliderWidget()
            for s in (ui["tiltA"], ui["tiltB"]):
                s.minimum, s.maximum, s.singleStep, s.decimals = -45, 45, 1, 0
                s.suffix = "°"
                s.tracking = False
            f.addRow(ui["tilt"])
            f.addRow("Tilt angle a:", ui["tiltA"])
            f.addRow("Tilt angle b:", ui["tiltB"])
            row2 = qt.QHBoxLayout()
            ui["free"] = qt.QCheckBox("Free-hand (skeleton failed)")
            ui["applyFree"] = qt.QPushButton("Apply markup plane")
            row2.addWidget(ui["free"])
            row2.addWidget(ui["applyFree"])
            f.addRow(row2)
            row3 = qt.QHBoxLayout()
            ui["notPresent"] = qt.QCheckBox("Connection not present")
            ui["lock"] = qt.QCheckBox("Lock")
            row3.addWidget(ui["notPresent"])
            row3.addWidget(ui["lock"])
            f.addRow(row3)
            layout.addRow(group)
            self.planeUi[outflow] = ui

            ui["slider"].connect("valueChanged(double)", lambda v, o=outflow: self.onPlaneSlider(o, v))
            ui["fromMax"].connect("clicked()", lambda o=outflow: self.onPlaneJump(o, "max"))
            ui["fromMin"].connect("clicked()", lambda o=outflow: self.onPlaneJump(o, "min"))
            ui["setUpper"].connect("clicked()", lambda o=outflow: self.onPlaneBound(o, "upper"))
            ui["setLower"].connect("clicked()", lambda o=outflow: self.onPlaneBound(o, "lower"))
            ui["reset"].connect("clicked()", lambda o=outflow: self.onPlaneReset(o))
            ui["tilt"].connect("toggled(bool)", lambda c, o=outflow: self.onPlaneTilt(o))
            ui["tiltA"].connect("valueChanged(double)", lambda v, o=outflow: self.onPlaneTilt(o))
            ui["tiltB"].connect("valueChanged(double)", lambda v, o=outflow: self.onPlaneTilt(o))
            ui["free"].connect("toggled(bool)", lambda c, o=outflow: self.onPlaneFree(o, c))
            ui["applyFree"].connect("clicked()", lambda o=outflow: self.onPlaneApplyFree(o))
            ui["notPresent"].connect("toggled(bool)", lambda c, o=outflow: self.onPlaneNotPresent(o, c))
            ui["lock"].connect("toggled(bool)", lambda c, o=outflow: self.onPlaneLock(o, c))

    def _setupEditSection(self):
        box, form = self._section("4. Correction", collapsed=True)
        self.editBox = box
        self.editor = slicer.qMRMLSegmentEditorWidget()
        self.editor.setMRMLScene(slicer.mrmlScene)
        self.editorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "SeptumAnnotatorEditor")
        self.editorNode.SetSingletonTag("SeptumAnnotatorEditor")
        self.editor.setMRMLSegmentEditorNode(self.editorNode)
        if hasattr(self.editor, "setEffectNameOrder"):
            self.editor.setEffectNameOrder(["Islands", "Erase", "Paint", "Smoothing", "Scissors"])
        if hasattr(self.editor, "unorderedEffectsVisible"):
            self.editor.unorderedEffectsVisible = False
        form.addRow(self.editor)
        note = qt.QLabel("Prefer 'Islands' (remove spurious components). Chamber voxels added here are "
                         "dropped automatically; holes are handled in step 5.")
        note.wordWrap = True
        form.addRow(note)

    def _setupOpeningSection(self):
        box, form = self._section("5. Hole QA")
        self.openingBox = box
        self.detectButton = qt.QPushButton("Detect openings")
        form.addRow(self.detectButton)
        self.openingList = qt.QListWidget()
        self.openingList.setMinimumHeight(120)
        form.addRow(self.openingList)
        row = qt.QHBoxLayout()
        self.prevButton = qt.QPushButton("◀ Previous")
        self.nextButton = qt.QPushButton("Next ▶")
        self.approveButton = qt.QPushButton("Approve (true defect)")
        self.rejectButton = qt.QPushButton("Reject (patch)")
        for b in (self.prevButton, self.approveButton, self.rejectButton, self.nextButton):
            row.addWidget(b)
        form.addRow(row)
        self.uncertainCheck = qt.QCheckBox("Uncertain")
        form.addRow(self.uncertainCheck)
        self.detectButton.connect("clicked()", self.onDetectOpenings)
        self.openingList.connect("currentRowChanged(int)", self.onOpeningSelected)
        self.prevButton.connect("clicked()", lambda: self._stepOpening(-1))
        self.nextButton.connect("clicked()", lambda: self._stepOpening(1))
        self.approveButton.connect("clicked()", lambda: self.onDecide("approve"))
        self.rejectButton.connect("clicked()", lambda: self.onDecide("reject"))

    def _setupFinishSection(self):
        box, form = self._section("6. Lock & export")
        self.finishBox = box
        self.notesEdit = qt.QPlainTextEdit()
        self.notesEdit.setMaximumHeight(60)
        self.notesEdit.placeholderText = "Notes for the log (optional)"
        form.addRow(self.notesEdit)
        self.exportButton = qt.QPushButton("Lock && export")
        self.exportButton.setStyleSheet("font-weight: bold")
        form.addRow(self.exportButton)
        self.exportButton.connect("clicked()", self.onExport)

    # ================================================================ helpers
    def _setCaseLoaded(self, loaded):
        for box in (self.seedBox, self.planeBox, self.editBox, self.openingBox, self.finishBox):
            box.enabled = loaded

    def _onModeChanged(self, *args):
        self.grindWidget.visible = self.grindRadio.checked
        self.customWidget.visible = self.customRadio.checked

    def _confirmDiscard(self):
        if self.logic.session is None or self.exported:
            return True
        return slicer.util.confirmYesNoDisplay("The current case has not been exported. Discard it?")

    def _outputDir(self):
        path = self.outputDir.currentPath
        if not path:
            raise ValueError("Select an output folder first.")
        os.makedirs(path, exist_ok=True)
        return path

    # ================================================================ case loading
    def onStartGrind(self):
        if not self._confirmDiscard():
            return
        with slicer.util.tryWithErrorDisplay("Could not start grind mode.", waitCursor=True):
            path = self.configPath.currentPath
            if path.lower().endswith((".xlsx", ".xlsm")):
                self._ensureOpenpyxl()
            self.cases = cfg.load_config(path)
            if not self.cases:
                raise ValueError("The config table contains no cases.")
            self.manifest = manifest.Manifest(os.path.join(self._outputDir(), "manifest.csv"),
                                              [c.case_id for c in self.cases])
            self.grind = True
            self._loadNextGrindCase()

    def _loadNextGrindCase(self):
        next_id = self.manifest.next_case([c.case_id for c in self.cases])
        if next_id is None:
            self.logic.clear()
            self._setCaseLoaded(False)
            counts = self.manifest.counts()
            self.caseLabel.text = f"All cases done ({counts['done']}/{len(self.cases)})."
            slicer.util.infoDisplay("All cases in the config table are done.")
            return
        case = next(c for c in self.cases if c.case_id == next_id)
        self._loadCase(case)
        self.manifest.set_status(case.case_id, "in_progress")
        counts = self.manifest.counts()
        self.caseLabel.text = (f"<b>{case.case_id}</b> — {counts['done']}/{len(self.cases)} done "
                               f"(seed: {case.seed_strategy}, region: {case.region}, segmentation: {case.seg_source})")

    def onLoadCustom(self):
        if not self._confirmDiscard():
            return
        with slicer.util.tryWithErrorDisplay("Could not load the case.", waitCursor=True):
            ct = self.customCt.currentPath
            case = cfg.CaseConfig(
                case_id=self.customCaseId.text.strip() or os.path.basename(ct).split(".")[0],
                ct_path=ct,
                seg_path=self.customSeg.currentPath,
                seg_source=self.customSegSource.currentText,
                label_map=self.customLabelMap.currentText,
                seed_strategy=self.customStrategy.currentText,
                region=self.regionCombo.currentText,
                sheetness_path=self.customSheetness.currentPath,
                skeleton_lv_aorta=self.customSkeletons["lv_aorta"].currentPath,
                skeleton_rv_pa=self.customSkeletons["rv_pa"].currentPath,
                mitral_path=self.customMitral.currentPath,
            )
            self._outputDir()
            self.grind = False
            self._loadCase(case)
            self.caseLabel.text = f"<b>{case.case_id}</b> (custom)"

    def _loadCase(self, case):
        s = self.logic.loadCase(case, self.dSlider.value, self.tSlider.value)
        self.exported = False
        self._updating = True
        try:
            self.regionCombo.currentText = case.region
            plan_b = case.seed_strategy == "sheetness"
            self.tSlider.visible = self.tLabel.visible = plan_b
            for outflow, ui in self.planeUi.items():
                state = s.planes[outflow]
                n = len(state.skeleton) if state.skeleton is not None else 1
                ui["slider"].minimum, ui["slider"].maximum = 0, max(n - 1, 0)
                ui["slider"].value = n - 1
                ui["tilt"].checked = False
                ui["tiltA"].value = ui["tiltB"].value = 0
                ui["free"].checked = state.skeleton is None
                ui["notPresent"].checked = state.not_present
                ui["lock"].checked = False
                self._refreshPlane(outflow)
            self.notesEdit.plainText = ""
            self.openingList.clear()
            self.currentOpening = None
        finally:
            self._updating = False
        if not slicer.mrmlScene.IsNodePresent(self.editorNode):  # removed by a scene close
            self.editorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "SeptumAnnotatorEditor")
            self.editor.setMRMLSegmentEditorNode(self.editorNode)
        self.editor.setSegmentationNode(self.logic.nodes["septum"])
        self.editor.setSourceVolumeNode(self.logic.nodes["work"])
        self.editor.setCurrentSegmentID(self.logic.septumId)
        lm = slicer.app.layoutManager()
        lm.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        # The editor switches the background to its source (1 mm) volume; show the native CT instead.
        slicer.util.setSliceViewerLayers(background=self.logic.nodes["ct"], fit=True)
        lm.threeDWidget(0).threeDView().resetFocalPoint()
        lm.threeDWidget(0).threeDView().resetCamera()
        self._setCaseLoaded(True)
        self._updateSeedInfo()

    def _ensureOpenpyxl(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            if slicer.util.confirmOkCancelDisplay("Reading .xlsx needs the 'openpyxl' package. Install it now?"):
                slicer.util.pip_install("openpyxl")
            else:
                raise

    # ================================================================ seed
    def onSeedParams(self, *args):
        if self._updating or self.logic.session is None:
            return
        with slicer.util.tryWithErrorDisplay("Seed update failed.", waitCursor=True):
            self.logic.recompute(d_mm=self.dSlider.value, region=self.regionCombo.currentText,
                                 sheetness_threshold=self.tSlider.value)
            self._updateSeedInfo()

    def _updateSeedInfo(self):
        s = self.logic.session
        e = s.edits.counts()
        self.seedInfo.text = (f"Seed: {int(s.seed.sum())} voxels · added {e['voxels_added']} · "
                              f"removed {e['voxels_removed']} · patched {e['voxels_patched']}")

    # ================================================================ planes
    def _state(self, outflow):
        return self.logic.session.planes[outflow]

    def _refreshPlane(self, outflow):
        state, ui = self._state(outflow), self.planeUi[outflow]
        has_skel = state.skeleton is not None
        free = state.free_point is not None or ui["free"].checked
        locked = state.locked
        searching = has_skel and not free and not state.not_present and not locked
        for key in ("slider", "fromMax", "fromMin", "setUpper", "setLower", "reset"):
            ui[key].enabled = searching
        ui["tilt"].enabled = has_skel and state.gt_index is not None and not locked and not free
        ui["tiltA"].enabled = ui["tiltB"].enabled = ui["tilt"].enabled and ui["tilt"].checked
        ui["free"].enabled = not locked and not state.not_present
        ui["applyFree"].enabled = ui["free"].checked and not locked
        ui["notPresent"].enabled = not locked
        ui["lock"].enabled = state.resolved or locked

        parts = []
        if not has_skel:
            parts.append("No skeleton: use free-hand mode.")
        if state.upper is not None:
            parts.append(f"upper {state.upper}")
        if state.lower is not None:
            parts.append(f"lower {state.lower}")
        if state.gt_index is not None:
            width = state.skeleton.arc_length(state.upper) - state.skeleton.arc_length(state.lower)
            parts.append(f"<b>GT index {state.gt_index:g}</b> (range {state.upper - state.lower} idx, {width:.1f} mm)")
        if state.free_point is not None:
            parts.append("<b>free-hand plane</b>")
        if state.not_present:
            parts.append("connection not present")
        ui["info"].text = " · ".join(parts)

        plane_node = self.logic.planeNodes.get(outflow)
        if plane_node is not None:
            plane_node.SetLocked(not (ui["free"].checked and not locked))
        if ui["free"].checked and state.free_point is None and not locked:
            # start the free-hand plane where the skeleton plane is (or at the chamber centre)
            plane = state.plane(use_preview=True)
            if plane is None:
                ventricle = cfg.OUTFLOW_STRUCTURES[outflow][0]
                zyx = np.argwhere(self.logic.session.structures[ventricle]).mean(0)
                p = self.logic.session.ref.TransformContinuousIndexToPhysicalPoint(tuple(float(v) for v in zyx[::-1]))
                plane = (np.array(p), np.array([0.0, 0.0, 1.0]))
            self.logic.showPlane(outflow, plane)
        else:
            self.logic.showPlane(outflow, state.plane(use_preview=not locked))

    def _planeChanged(self, outflow):
        self.logic.recompute()
        self._refreshPlane(outflow)
        self._updateSeedInfo()

    def onPlaneSlider(self, outflow, value):
        if self._updating or self.logic.session is None:
            return
        state = self._state(outflow)
        state.preview_index = float(value)
        if state.gt_index is None:
            self._planeChanged(outflow)
        else:
            self.logic.showPlane(outflow, state.preview(value))

    def onPlaneJump(self, outflow, which):
        state = self._state(outflow)
        slider = self.planeUi[outflow]["slider"]
        slider.value = state.skeleton.max_index if which == "max" else 0
        self.onPlaneSlider(outflow, slider.value)

    def onPlaneBound(self, outflow, which):
        state = self._state(outflow)
        index = int(round(self.planeUi[outflow]["slider"].value))
        try:
            state.set_upper(index) if which == "upper" else state.set_lower(index)
        except planes.BoundsOrderError as exc:
            slicer.util.warningDisplay(str(exc))
        self._planeChanged(outflow)

    def onPlaneReset(self, outflow):
        state = self._state(outflow)
        state.reset_search()
        state.tilt_override = False
        self.planeUi[outflow]["tilt"].checked = False
        self._planeChanged(outflow)

    def onPlaneTilt(self, outflow):
        if self._updating or self.logic.session is None:
            return
        state, ui = self._state(outflow), self.planeUi[outflow]
        state.tilt_override = ui["tilt"].checked
        state.tilt_a, state.tilt_b = ui["tiltA"].value, ui["tiltB"].value
        self._planeChanged(outflow)

    def onPlaneFree(self, outflow, checked):
        if self._updating or self.logic.session is None:
            return
        if not checked:
            self._state(outflow).clear_free_hand()
            self._planeChanged(outflow)
        else:
            self._refreshPlane(outflow)

    def onPlaneApplyFree(self, outflow):
        point, normal = self.logic.freeHandFromNode(outflow)
        self._state(outflow).set_free_hand(point, normal)
        self._planeChanged(outflow)

    def onPlaneNotPresent(self, outflow, checked):
        if self._updating or self.logic.session is None:
            return
        self._state(outflow).not_present = checked
        self._planeChanged(outflow)

    def onPlaneLock(self, outflow, checked):
        if self._updating or self.logic.session is None:
            return
        state = self._state(outflow)
        if checked and not state.resolved:
            slicer.util.warningDisplay("Set both bounds (or a free-hand plane) before locking.")
            self._updating = True
            self.planeUi[outflow]["lock"].checked = False
            self._updating = False
            return
        state.locked = checked
        self.logic.session.mark(f"plane_{outflow}_{'locked' if checked else 'unlocked'}")
        self._planeChanged(outflow)

    # ================================================================ openings
    def onDetectOpenings(self):
        with slicer.util.tryWithErrorDisplay("Opening detection failed.", waitCursor=True):
            self.logic.syncEdits()
            self.logic.pushMask()
            self.logic.session.detect_openings()
            self._fillOpeningList()
            if self.openingList.count:
                self.openingList.setCurrentRow(0)
            else:
                self.logic.showOpening(None)
            self._updateSeedInfo()

    def _fillOpeningList(self, keep_row=None):
        self.openingList.blockSignals(True)
        self.openingList.clear()
        for o in self.logic.session.openings:
            mark = {"approve": "✔ approved", "reject": "✖ rejected", None: "• undecided"}[o.decision]
            unc = " (uncertain)" if o.uncertain else ""
            self.openingList.addItem(f"{mark}{unc} — {o.size_mm3:.1f} mm³ — {o.origin}")
        self.openingList.blockSignals(False)
        if keep_row is not None and 0 <= keep_row < self.openingList.count:
            self.openingList.setCurrentRow(keep_row)

    def onOpeningSelected(self, row):
        ops = self.logic.session.openings if self.logic.session else []
        self.currentOpening = ops[row] if 0 <= row < len(ops) else None
        self.logic.showOpening(self.currentOpening)
        if self.currentOpening is not None:
            self.uncertainCheck.checked = self.currentOpening.uncertain

    def _stepOpening(self, step):
        n = self.openingList.count
        if n:
            self.openingList.setCurrentRow((self.openingList.currentRow + step) % n)

    def onDecide(self, decision):
        if self.currentOpening is None:
            return
        with slicer.util.tryWithErrorDisplay("Could not apply the decision.", waitCursor=True):
            row = self.openingList.currentRow
            self.logic.syncEdits()
            self.logic.session.decide(self.currentOpening, decision, self.uncertainCheck.checked)
            self.logic.pushMask()
            self._fillOpeningList(keep_row=row)
            undecided = [i for i, o in enumerate(self.logic.session.openings) if o.decision is None]
            if undecided:
                self.openingList.setCurrentRow(next((i for i in undecided if i > row), undecided[0]))
            self._updateSeedInfo()

    # ================================================================ export
    def onExport(self):
        s = self.logic.session
        if s is None:
            return
        with slicer.util.tryWithErrorDisplay("Export failed.", waitCursor=True):
            self.logic.syncEdits()
            try:
                log = s.export(self._outputDir(), notes=self.notesEdit.plainText)
            except session.ExportBlocked as exc:
                self.logic.pushMask()
                self._fillOpeningList()
                slicer.util.warningDisplay("Cannot export yet:\n• " + str(exc).replace("; ", "\n• "))
                return
            self.exported = True
            if log["native_check"]["differs"]:
                slicer.util.warningDisplay(
                    "Openings differ between the 1 mm grid and the native grid after transforming back. "
                    "The case is exported; it is flagged in the log (native_check) for a second look.")
            if self.grind:
                self.manifest.set_status(s.case.case_id, "done")
                self._loadNextGrindCase()
            else:
                self.logic.pushMask()
                slicer.util.infoDisplay(f"Exported {s.case.case_id} to {self._outputDir()}.")

    # ================================================================ scene
    def onSceneStartClose(self, caller, event):
        self.logic.nodes, self.logic.planeNodes, self.logic.session = {}, {}, None
        self._setCaseLoaded(False)
        self.caseLabel.text = "No case loaded."

    def cleanup(self):
        self.removeObservers()

    def onReload(self):
        """Developer-mode Reload: also reload the core library, not only this file."""
        importlib.reload(SeptumAnnotatorLib)
        for name in _LIB_MODULES:
            importlib.reload(importlib.import_module(f"SeptumAnnotatorLib.{name}"))
        ScriptedLoadableModuleWidget.onReload(self)


#
# Test
#


class SeptumAnnotatorTest(ScriptedLoadableModuleTest):
    """Loads a synthetic case through the GUI logic and exports it."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_synthetic_case()

    def test_synthetic_case(self):
        import importlib.util
        import tempfile

        tools = os.path.join(os.path.dirname(__file__), "..", "tools", "make_synthetic_case.py")
        spec = importlib.util.spec_from_file_location("make_synthetic_case", tools)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = tempfile.mkdtemp(prefix="septum_test_")
        row = mod.make_case(out, "synth_test", with_vsd=True, rng=np.random.default_rng(0))
        case = cfg.case_from_row({k: v for k, v in row.items() if not k.startswith("_")}, out)

        logic = SeptumAnnotatorLogic()
        s = logic.loadCase(case, d_mm=6.0, sheetness_threshold=0.1)
        for state in s.planes.values():
            mid = state.skeleton.max_index // 2
            state.set_upper(mid + 2)
            state.set_lower(mid - 2)
            state.locked = True
        logic.recompute()
        for o in s.detect_openings():
            s.decide(o, "approve")
        log = s.export(os.path.join(out, "export"))
        self.assertTrue(os.path.isfile(os.path.join(out, "export", "synth_test_septum.nii.gz")))
        self.assertGreater(log["seed"]["final_voxels"], 0)
        self.delayDisplay("Test passed")
