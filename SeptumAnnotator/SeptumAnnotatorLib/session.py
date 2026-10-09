"""One annotation case end to end: load → seed → planes → edits → openings → export.

Used by the Slicer module, but runs without Slicer (see Testing/test_core.py).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from typing import Dict, List, Optional

import numpy as np
import SimpleITK as sitk

from . import PROTOCOL_VERSION, __version__
from . import imaging, seed as seedlib
from .config import DEFAULTS, OUTFLOW_STRUCTURES, OUTFLOWS, CaseConfig
from .openings import Opening, carry_over, detect_openings, dilate6
from .planes import PlaneState, Skeleton, below_plane_mask


class ExportBlocked(RuntimeError):
    pass


class CaseSession:
    def __init__(self, case: CaseConfig, spacing_mm: float = DEFAULTS["spacing_mm"],
                 d_mm: float = DEFAULTS["d_mm"], sheetness_threshold: float = DEFAULTS["sheetness_threshold"]):
        case.validate()
        self.case = case
        self.d_mm = float(d_mm)
        self.sheetness_threshold = float(sheetness_threshold)
        self.region_kind = case.region
        self.events: Dict[str, float] = {}
        self._t0 = time.time()

        # --- native data
        self.native_ct = imaging.read_image(case.ct_path)
        self.native_seg = imaging.read_image(case.seg_path)
        labels = case.labels()

        # --- working grid (isotropic) and resampled inputs
        self.ref = imaging.isotropic_reference(self.native_seg, spacing_mm)
        self.ct = imaging.resample_ct(self.native_ct, self.ref)
        label_arr = imaging.resample_labels(self.native_seg, self.ref)
        self.structures = imaging.extract_structures(label_arr, labels)
        for name in ("lv", "rv"):
            if not self.structures[name].any():
                raise ValueError(f"Case {case.case_id}: structure {name!r} is empty in {case.seg_path}")
        if case.seed_strategy == "myocardium" and not self.structures["myo"].any():
            raise ValueError(f"Case {case.case_id}: myocardium label is empty; use seed_strategy 'sheetness'")
        self.lv = self.structures["lv"]
        self.rv = self.structures["rv"]
        self.chambers = self.lv | self.rv
        self.dist_lv = imaging.distance_map_mm(self.lv, self.ref)
        self.dist_rv = imaging.distance_map_mm(self.rv, self.ref)
        self.sheetness = None
        if case.seed_strategy == "sheetness":
            self.sheetness = imaging.to_array(
                imaging.resample_continuous(imaging.read_image(case.sheetness_path), self.ref))

        # --- planes
        self.planes: Dict[str, PlaneState] = {}
        for outflow in OUTFLOWS:
            state = PlaneState(outflow)
            path = case.skeleton_path(outflow)
            if path:
                state.skeleton = Skeleton.load_npz(path)
            ventricle, artery = OUTFLOW_STRUCTURES[outflow]
            if not self.structures.get(artery, np.zeros(1, bool)).any():
                state.not_present = True  # e.g. atresia; the annotator can override in the GUI
            self.planes[outflow] = state

        self.edits = seedlib.EditLayers(self.lv.shape)
        self.openings: List[Opening] = []
        self.rejected: List[Opening] = []
        self._hull_cache: Optional[np.ndarray] = None
        self.region = self._region()
        self.below = self._below()
        self.seed = self._seed()
        self.mark("loaded")

    # ------------------------------------------------------------ seed parts
    def mark(self, event: str) -> None:
        self.events[event] = round(time.time() - self._t0, 1)

    def _region(self) -> np.ndarray:
        if self.region_kind == "hull":
            if self._hull_cache is None:
                self._hull_cache = seedlib.convex_hull_mask(self.chambers)
            return self._hull_cache
        return seedlib.septal_band(self.dist_lv, self.dist_rv, self.d_mm)

    def _below(self) -> np.ndarray:
        below = np.ones(self.lv.shape, dtype=bool)
        for state in self.planes.values():
            plane = state.plane(use_preview=not state.locked)
            if plane is not None:
                below &= below_plane_mask(self.ref, *plane)
        return below

    def _seed(self) -> np.ndarray:
        if self.case.seed_strategy == "sheetness":
            return seedlib.seed_sheetness(self.sheetness, self.sheetness_threshold, self.region, self.below)
        return seedlib.seed_myocardium(self.structures["myo"], self.region, self.below)

    def update(self, d_mm: Optional[float] = None, region: Optional[str] = None,
               sheetness_threshold: Optional[float] = None) -> None:
        """Recompute the seed after a parameter or plane change. Edits are kept."""
        if d_mm is not None:
            self.d_mm = float(d_mm)
        if region is not None:
            self.region_kind = region
        if sheetness_threshold is not None:
            self.sheetness_threshold = float(sheetness_threshold)
        self.region = self._region()
        self.below = self._below()
        self.seed = self._seed()

    # ------------------------------------------------------------ mask
    def sync_edits(self, current_mask: Optional[np.ndarray]) -> None:
        self.edits.sync(current_mask)

    def final_mask(self) -> np.ndarray:
        return self.edits.compose(self.seed, self.below, self.chambers)

    # ------------------------------------------------------------ openings
    def domain(self) -> np.ndarray:
        return self.region & self.below

    def detect_openings(self) -> List[Opening]:
        previous = self.openings + self.rejected
        self.openings = detect_openings(self.final_mask(), self.lv, self.rv, self.domain(), self.ref, self.seed)
        matched = carry_over(previous, self.openings)
        # Rejected openings that are open again must be decided again.
        self.rejected = [o for o in self.rejected if not any(o is m for m in matched)]
        self.mark("openings_detected")
        return self.openings

    def decide(self, opening: Opening, decision: str, uncertain: bool = False) -> None:
        """Approve keeps the hole; reject patches it (protocol §10). A patched opening
        disappears from the next detection, so it is kept in `rejected` for the log."""
        opening.decide(decision, uncertain)
        flat = opening.voxels
        self.rejected = [o for o in self.rejected if o is not opening]
        if decision == "reject":
            self.edits.patch.flat[flat] = True
            self.edits.remove.flat[flat] = False
            self.rejected.append(opening)
        else:
            self.edits.patch.flat[flat] = False

    def all_decided_openings(self) -> List[Opening]:
        current = {id(o) for o in self.openings}
        return self.openings + [o for o in self.rejected if id(o) not in current]

    def undecided(self) -> List[Opening]:
        return [o for o in self.openings if o.decision is None]

    # ------------------------------------------------------------ export
    def check_ready(self) -> List[str]:
        problems = []
        self.update()  # make sure the mask uses the final (locked) planes, not a preview
        for outflow, state in self.planes.items():
            if not state.resolved:
                problems.append(f"{outflow}: plane not set")
            elif not state.locked and not state.not_present:
                problems.append(f"{outflow}: plane not locked")
        # Recompute so that decisions always refer to the mask being exported.
        self.detect_openings()
        if self.undecided():
            problems.append(f"{len(self.undecided())} opening(s) without a decision")
        return problems

    def native_check(self, final: np.ndarray, native_mask: np.ndarray, match_mm: float = 5.0) -> dict:
        """Re-run the leak test on the native grid (cropped) to catch holes that
        closed or opened when transforming back."""
        if not final.any():
            return {"approved_working_grid": 0, "openings_native": 0, "new_on_native": [],
                    "lost_on_native": [], "differs": False}
        index, size = imaging.bbox_xyz(final | dilate6(final), margin_vox=3)
        n_index, n_size = imaging.physical_box_to_index_box(self.ref, index, size, self.native_seg)
        crop = sitk.RegionOfInterest(self.native_seg, n_size, n_index)
        labels = self.case.labels()
        arr = imaging.to_array(crop)
        lv, rv = arr == labels["lv"], arr == labels["rv"]
        sl = tuple(slice(n_index[i], n_index[i] + n_size[i]) for i in (2, 1, 0))
        septum = native_mask[sl]
        domain_img = imaging.from_array(self.domain().astype(np.uint8), self.ref)
        domain = imaging.resample_mask_to(domain_img, crop)
        native = detect_openings(septum, lv, rv, domain, crop)
        approved = [o for o in self.openings if o.decision == "approve"]
        # Match by location: one defect can split into several native pieces.
        tol = match_mm + max(self.ref.GetSpacing())

        def near(a, others):
            return any(np.linalg.norm(np.subtract(a.centroid_lps, b.centroid_lps)) <= tol for b in others)

        new = [o for o in native if not near(o, approved)]
        lost = [o for o in approved if not near(o, native)]
        return {
            "approved_working_grid": len(approved),
            "openings_native": len(native),
            "new_on_native": [{"centroid_lps_mm": [round(float(v), 2) for v in o.centroid_lps],
                               "size_mm3": round(o.size_mm3, 2)} for o in new],
            "lost_on_native": [o.id for o in lost],
            "differs": bool(new or lost),
        }

    def export(self, out_dir: str, notes: str = "") -> dict:
        problems = self.check_ready()
        if problems:
            raise ExportBlocked("; ".join(problems))
        self.mark("exported")
        os.makedirs(out_dir, exist_ok=True)
        cid = self.case.case_id
        final = self.final_mask()

        working_path = os.path.join(out_dir, f"{cid}_septum_1mm.nii.gz")
        imaging.write_mask(final, self.ref, working_path)
        native_mask = imaging.resample_mask_to(imaging.from_array(final.astype(np.uint8), self.ref), self.native_seg)
        native_path = os.path.join(out_dir, f"{cid}_septum.nii.gz")
        imaging.write_mask(native_mask, self.native_seg, native_path)

        log = {
            "meta": {
                "case_id": cid,
                "protocol_version": PROTOCOL_VERSION,
                "tool_version": __version__,
                "git_hash": _git_hash(),
                "date": _dt.datetime.now().isoformat(timespec="seconds"),
                "seconds_since_load": self.events,
                "config": self.case.to_dict(),
            },
            "seed": {
                "strategy": self.case.seed_strategy,
                "seg_source": self.case.seg_source,
                "region": self.region_kind,
                "d_mm": self.d_mm,
                "sheetness_threshold": self.sheetness_threshold if self.sheetness is not None else None,
                "seed_voxels": int(self.seed.sum()),
                "final_voxels": int(final.sum()),
                **self.edits.counts(),
            },
            "planes": {k: v.to_log() for k, v in self.planes.items()},
            "openings": [o.to_log() for o in self.all_decided_openings()],
            "native_check": self.native_check(final, native_mask),
            "notes": notes,
            "outputs": {"native": os.path.basename(native_path), "working_grid": os.path.basename(working_path)},
        }
        with open(os.path.join(out_dir, f"{cid}_log.json"), "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
        return log


def _git_hash() -> Optional[str]:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(__file__),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None
