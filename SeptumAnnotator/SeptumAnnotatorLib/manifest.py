"""Per-output-folder progress file (manifest.csv): case_id, status, updated_at.

The config table is treated as read-only input; status lives here, so an open
Excel file can never block saving.
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
import tempfile
from typing import Dict, Iterable, Optional

STATUSES = ("todo", "in_progress", "done")


class Manifest:
    COLUMNS = ("case_id", "status", "updated_at")

    def __init__(self, path: str, case_ids: Iterable[str] = ()):
        self.path = path
        self.rows: Dict[str, Dict[str, str]] = {}
        if os.path.isfile(path):
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self.rows[row["case_id"]] = {k: row.get(k, "") for k in self.COLUMNS}
        changed = False
        for case_id in case_ids:
            if case_id not in self.rows:
                self.rows[case_id] = {"case_id": case_id, "status": "todo", "updated_at": ""}
                changed = True
        if changed or not os.path.isfile(path):
            self.save()

    def status(self, case_id: str) -> str:
        return self.rows.get(case_id, {}).get("status", "todo")

    def set_status(self, case_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        now = _dt.datetime.now().isoformat(timespec="seconds")
        self.rows[case_id] = {"case_id": case_id, "status": status, "updated_at": now}
        self.save()

    def next_case(self, ordered_ids: Iterable[str]) -> Optional[str]:
        """First case (in config order) that is not done. An interrupted
        'in_progress' case is therefore resumed first."""
        for case_id in ordered_ids:
            if self.status(case_id) != "done":
                return case_id
        return None

    def counts(self) -> Dict[str, int]:
        result = {s: 0 for s in STATUSES}
        for row in self.rows.values():
            result[row["status"]] = result.get(row["status"], 0) + 1
        return result

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
            writer.writeheader()
            for row in self.rows.values():
                writer.writerow(row)
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written manifest
