import json
import os


REQUIRED_STRUCTURES = {"lv", "rv", "aorta", "pa"}  # + "myo" a myocardium seed-stratégiához


def loadLabelConfig(path):
    """{címkeérték: név} szótárt ad vissza, ellenőrzés után."""
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