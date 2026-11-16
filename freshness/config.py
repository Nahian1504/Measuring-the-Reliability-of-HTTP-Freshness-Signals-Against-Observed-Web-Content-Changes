from __future__ import annotations
import os
from types import SimpleNamespace
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str | None = None, **overrides) -> SimpleNamespace:
    path = path or os.path.join(ROOT, "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.update({k: v for k, v in overrides.items() if v is not None})
    for key in ("db_path", "tranco_csv", "results_dir"):
        if key in data and not os.path.isabs(data[key]):
            data[key] = os.path.join(ROOT, data[key])
    return SimpleNamespace(**data)
