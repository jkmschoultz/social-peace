"""Load and resolve config/config.yaml + config/templates.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` until we find a dir containing config/config.yaml."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "config" / "config.yaml").is_file():
            return parent
    raise FileNotFoundError("could not locate repo root (config/config.yaml not found)")


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class Config:
    root: Path
    raw: dict
    templates: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    # ---- loading -----------------------------------------------------------
    @classmethod
    def load(cls, root: str | os.PathLike | None = None) -> "Config":
        root_path = Path(root) if root else find_repo_root()
        load_dotenv(root_path / ".env")

        raw = _read_yaml(root_path / "config" / "config.yaml")
        templates = (_read_yaml(root_path / "config" / "templates.yaml") or {}).get("templates", [])
        if not templates:
            raise ValueError("config/templates.yaml has no `templates:` entries")

        manifest: dict = {}
        man_rel = raw.get("assets_manifest")
        if man_rel:
            man_path = root_path / man_rel
            if man_path.is_file():
                manifest = _read_yaml(man_path) or {}

        cfg = cls(root=root_path, raw=raw, templates=templates, manifest=manifest)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        for key in ("render", "paths", "overlay_text", "metadata"):
            if key not in self.raw:
                raise ValueError(f"config.yaml missing required section: {key!r}")
        if not self.raw["overlay_text"]:
            raise ValueError("config.yaml `overlay_text` is empty")
        for p in ("video_assets", "audio_assets", "fonts", "output", "logs"):
            if p not in self.raw["paths"]:
                raise ValueError(f"config.yaml paths.{p} is required")

    # ---- accessors -------------------------------------------------------
    def path(self, key: str) -> Path:
        """Resolve a configured path (paths.<key>) against the repo root."""
        return (self.root / self.raw["paths"][key]).resolve()

    @property
    def render(self) -> dict:
        return self.raw["render"]

    def env(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)

    def tags_for(self, kind: str, filename: str) -> list[str]:
        """kind is 'video' or 'audio'. Returns manifest tags for a filename, or []."""
        entry = (self.manifest.get(kind) or {}).get(filename) or {}
        return list(entry.get("tags", []))

    def source_for(self, kind: str, filename: str) -> str | None:
        """kind is 'video' or 'audio'. Returns the manifest provenance URL, or None."""
        entry = (self.manifest.get(kind) or {}).get(filename) or {}
        return entry.get("url") or None

    def _entry(self, kind: str, filename: str) -> dict:
        return (self.manifest.get(kind) or {}).get(filename) or {}

    def favourite(self, kind: str, filename: str) -> bool:
        return bool(self._entry(kind, filename).get("favourite"))

    def label_for(self, kind: str, filename: str) -> str | None:
        return self._entry(kind, filename).get("label") or None
