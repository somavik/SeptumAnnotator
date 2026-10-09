"""Create a small synthetic heart case to try the tool without real data.

Two box-shaped ventricles separated by a myocardium wall (with one VSD hole), an
aorta above the LV and a PA above the RV, a matching CT, skeletons for both
outflows and a grind-mode config. Labels follow the ImageCHD convention.

    python tools/make_synthetic_case.py OUTPUT_DIR [--cases 3]
"""

from __future__ import annotations

import argparse
import csv
import os

import numpy as np
import SimpleITK as sitk

SPACING = (0.5, 0.5, 0.7)  # anisotropic on purpose, (x, y, z) mm
SHAPE_ZYX = (110, 140, 160)


def make_case(out_dir: str, case_id: str, with_vsd: bool, rng: np.random.Generator) -> dict:
    lab = np.zeros(SHAPE_ZYX, np.uint8)
    z0, z1 = 15, 70           # ventricle extent (slices)
    y0, y1 = 35, 105
    lv_x, wall_x, rv_x = (30, 74), (74, 84), (84, 128)  # septum is x 74..84 (5 mm thick)

    # myocardium shell first, chambers carved inside
    lab[z0 - 6:z1, y0 - 8:y1 + 8, lv_x[0] - 8:rv_x[1] + 8] = 5
    lab[z0:z1, y0:y1, lv_x[0]:lv_x[1]] = 1
    lab[z0:z1, y0:y1, rv_x[0]:rv_x[1]] = 2
    lab[z1:, :, :] = np.where(lab[z1:, :, :] == 5, 0, lab[z1:, :, :])

    # great arteries: cylinders going up (superior = +z) from the ventricles' upper ends
    zz, yy, xx = np.mgrid[0:SHAPE_ZYX[0], 0:SHAPE_ZYX[1], 0:SHAPE_ZYX[2]]
    ao_c, pa_c = (70, 52), (70, 106)  # (y, x)
    r = 14
    lab[(zz >= z1) & ((yy - ao_c[0]) ** 2 + (xx - ao_c[1]) ** 2 < r ** 2)] = 6
    lab[(zz >= z1) & ((yy - pa_c[0]) ** 2 + (xx - pa_c[1]) ** 2 < r ** 2)] = 7

    vsd = None
    if with_vsd:
        cz, cy = int(rng.integers(30, 55)), int(rng.integers(55, 85))
        hole = (wall_x[0] <= xx) & (xx < wall_x[1]) & ((zz - cz) ** 2 + (yy - cy) ** 2 < 7 ** 2)
        lab[hole & (xx < 79)] = 1   # the defect's blood is labelled as chamber, as in GT
        lab[hole & (xx >= 79)] = 2
        vsd = (cz, cy)

    hu = np.full(SHAPE_ZYX, -60.0)
    hu[lab == 5] = 90
    hu[np.isin(lab, (1, 2, 6, 7))] = 320
    hu += rng.normal(0, 25, SHAPE_ZYX)

    def img(arr, dtype):
        im = sitk.GetImageFromArray(arr.astype(dtype))
        im.SetSpacing(SPACING)
        im.SetOrigin((-40.0, -35.0, -30.0))
        return im

    ct_path = os.path.join(out_dir, f"{case_id}_ct.nii.gz")
    seg_path = os.path.join(out_dir, f"{case_id}_seg.nii.gz")
    ct_img, seg_img = img(hu, np.int16), img(lab, np.uint8)
    sitk.WriteImage(ct_img, ct_path, useCompression=True)
    sitk.WriteImage(seg_img, seg_path, useCompression=True)

    row = {"case_id": case_id, "ct_path": os.path.basename(ct_path), "seg_path": os.path.basename(seg_path),
           "seg_source": "gt", "label_map": "imagechd", "seed_strategy": "myocardium", "region": "band"}
    for outflow, (cy, cx), cx_v in (("lv_aorta", ao_c, 52), ("rv_pa", pa_c, 106)):
        # skeleton from the ventricle centre straight up through the artery; normals = +z
        zs = np.arange(30, SHAPE_ZYX[0] - 5, 1.0)
        pts = np.array([seg_img.TransformContinuousIndexToPhysicalPoint((float(cx), float(cy), float(z))) for z in zs])
        nrm = np.tile([0.0, 0.0, 1.0], (len(pts), 1)) + rng.normal(0, 0.03, (len(pts), 3))
        path = os.path.join(out_dir, f"{case_id}_skeleton_{outflow}.npz")
        np.savez(path, points=pts, normals=nrm)
        row[f"skeleton_{outflow}"] = os.path.basename(path)
    row["_truth_vsd_center_zy"] = vsd
    return row


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out_dir")
    p.add_argument("--cases", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    rows = [make_case(args.out_dir, f"synth_{i:02d}", with_vsd=(i % 2 == 0), rng=rng) for i in range(args.cases)]
    cols = [c for c in rows[0] if not c.startswith("_")]
    with open(os.path.join(args.out_dir, "config.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(r["case_id"], "VSD at (z, y) =" if r["_truth_vsd_center_zy"] else "no VSD", r["_truth_vsd_center_zy"] or "")
    print("config:", os.path.join(args.out_dir, "config.csv"))


if __name__ == "__main__":
    main()
