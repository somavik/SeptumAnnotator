# SeptumAnnotator
Handmade version

3D Slicer extension for semi-automatic annotation of the interventricular septum on cardiac CT
(BSc internship, BME MIT). It implements annotation protocol v0.2: automatic seed → outflow planes
(bidirectional search) → correction → hole QA → export.

## Layout

```
CMakeLists.txt                    extension
SeptumAnnotator/
  SeptumAnnotator.py              Slicer module: GUI + scene <-> array conversion
  SeptumAnnotatorLib/             core, no Slicer dependency (numpy, SimpleITK, scipy)
    config.py                     case table (grind mode), label maps
    imaging.py                    I/O, resampling to the 1 mm grid and back, distance maps
    seed.py                       septal band, convex hull, Plan A/B seeds, edit layers
    planes.py                     skeleton, bidirectional search, tilt, free-hand
    openings.py                   LV–RV leak detection (6-connectivity), decisions
    session.py                    one case end to end + export/log
    manifest.py                   progress file
tools/make_synthetic_case.py      synthetic demo cases (no real data needed)
```

## Install (development)

1. Slicer 5.x (tested with 5.12.4): *Edit → Application Settings → Modules → Additional module paths* →
   add `<repo>/SeptumAnnotator` and restart.
2. The module is under *Segmentation → Septum Annotator*.
3. numpy, SimpleITK and scipy ship with Slicer. `openpyxl` is only needed for `.xlsx` tables; the module
   offers to install it.

Try it without real data:

```
<Slicer python> tools/make_synthetic_case.py ~/septum_demo --cases 3
```

then pick `~/septum_demo/config.csv` in grind mode. The module self-test (*Reload and Test* in developer
mode) runs the same synthetic case end to end.

## Inputs

**Config table** (`.csv` or `.xlsx`, one row per case; relative paths are resolved from the table's folder):

| Column | Required | Values |
|---|---|---|
| `case_id` | yes | |
| `ct_path`, `seg_path` | yes | NIfTI/NRRD with corrected headers |
| `seg_source` | | `gt` (default) / `generated` |
| `label_map` | | `imagechd` (default) / `totalseg_heartchambers` |
| `seed_strategy` | | `myocardium` (Plan A, default) / `sheetness` (Plan B) |
| `region` | | `band` (default) / `hull` |
| `sheetness_path` | Plan B | precomputed sheetness map |
| `skeleton_lv_aorta`, `skeleton_rv_pa` | | `.npz` skeletons (below) |
| `mitral_path` | | segmentation shown as a visual aid only |

Other columns are ignored. **Do not use the dataset's diagnosis Excel as the config** — the tool is blind
by design. Progress is stored in `manifest.csv` in the output folder, never in the table.

**Label maps** are defined in `config.py` (`LABEL_MAPS`). **Check them against your files before the
first real case** (ImageCHD is assumed to be LV 1, RV 2, LA 3, RA 4, Myo 5, Ao 6, PA 7).

**Skeletons** (`.npz`): `points` (N×3) and `normals` (N×3), physical LPS mm, from the existing
ValveLocatorCNN feature generation; index 0 in the ventricle, N−1 at the artery end. Normal signs are
fixed automatically. To convert voxel-space output:
`planes.save_skeleton_npz(path, points_zyx, normals_zyx, reference_image)`. If a skeleton is missing,
the plane is set in free-hand mode (`skeleton_failed`).

## Workflow (per case)

1. **Case** — grind mode loads the next unfinished case from the table (an interrupted case is resumed
   first); custom mode loads one case from the form.
2. **Seed** — region (band/hull) and *d* (and *t* for Plan B); edits are kept when these change.
3. **Planes** — per outflow: *Start at MAX* → slide down → *Set upper*; *Start at MIN* → slide up →
   *Set lower*. The GT index is the midpoint. Optional tilt override, free-hand plane, "connection not
   present". Then *Lock*.
4. **Correction** — embedded Segment Editor (Islands first). Chamber voxels painted here are dropped.
5. **Hole QA** — *Detect openings*, then approve (keep the hole) or reject (patch). The first decision
   is stored as the blind decision.
6. **Lock & export** — blocked until both planes are locked and every opening is decided; openings are
   recomputed first, so decisions always refer to the exported mask.

## Outputs (output folder)

- `{case_id}_septum.nii.gz` — mask on the native grid (official GT).
- `{case_id}_septum_1mm.nii.gz` — mask on the 1 mm working grid.
- `{case_id}_log.json` — versions, timing, seed parameters and edit counts, both plane bounds,
  GT index, range width (index and mm), plane point/normal, flags, every opening with blind/final
  decision, and `native_check` (openings re-detected on the native grid; `differs: true` means look again).
- `manifest.csv` — `todo` / `in_progress` / `done`.

## Using the core without Slicer

```python
from SeptumAnnotatorLib.config import load_config
from SeptumAnnotatorLib.session import CaseSession
s = CaseSession(load_config("config.csv")[0], d_mm=5)
```

## Known limitations (prototype)

- Only tested on synthetic cases so far.
- Plan B needs a precomputed sheetness map; the Frangi filter itself is not part of the tool yet.
- The convex hull uses scipy Delaunay on the bounding box; it may be slow on large volumes.
- The opening origin `manual_edit` vs `seed_gap` is a heuristic.
- Patching a direct LV–RV contact overrides GT chamber voxels; it is logged as `gt_override`.
