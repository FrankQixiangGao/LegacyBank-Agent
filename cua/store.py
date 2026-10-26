"""Artifacts are YAML files under artifacts/<capability id>/v<N>.yaml — diffable and
reviewable in a PR, like any other code that runs against production systems."""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import AppProfile, Capability


def save_capability(cap: Capability, root: str | Path = "artifacts") -> Path:
    d = Path(root) / cap.id
    d.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(p.stem[1:]) for p in d.glob("v*.yaml"))
    cap.version = (existing[-1] + 1) if existing else 1
    path = d / f"v{cap.version}.yaml"
    path.write_text(yaml.safe_dump(cap.model_dump(mode="json", exclude_none=True), sort_keys=False, width=110))
    return path


def load_capability(path: str | Path) -> Capability:
    p = Path(path)
    if p.is_dir():  # latest version
        p = max(p.glob("v*.yaml"), key=lambda q: int(q.stem[1:]))
    return Capability.model_validate(yaml.safe_load(p.read_text()))


def load_profile(id_or_path: str, root: str | Path = "profiles") -> AppProfile:
    p = Path(id_or_path)
    if not p.exists():
        for cand in Path(root).glob("*.yaml"):
            data = yaml.safe_load(cand.read_text())
            if data.get("id") == id_or_path:
                return AppProfile.model_validate(data)
        raise FileNotFoundError(f"profile {id_or_path}")
    return AppProfile.model_validate(yaml.safe_load(p.read_text()))
