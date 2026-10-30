"""Artifact store: versioned artifact bodies on disk; the graph keeps only refs (VX-2)."""

from __future__ import annotations

import re
from pathlib import Path

from .state import ArtifactRef

_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, feature_id: str) -> Path:
        if not _SAFE.match(feature_id):
            raise ValueError(f"Unsafe feature id {feature_id!r}")
        d = self.root / feature_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(
        self,
        *,
        feature_id: str,
        kind: str,
        phase: str,
        content: str,
        parents: list[str],
        constitution_version: str,
    ) -> ArtifactRef:
        if not _SAFE.match(kind):
            raise ValueError(f"Unsafe artifact kind {kind!r}")
        d = self._dir(feature_id)
        version = len(list(d.glob(f"{kind}.v*.md"))) + 1
        path = d / f"{kind}.v{version}.md"
        path.write_text(content, encoding="utf-8")
        return ArtifactRef(
            id=f"{feature_id}.{kind}.v{version}",
            kind=kind,
            phase=phase,
            uri=str(path),
            version=version,
            parents=list(parents),
            constitution_version=constitution_version,
        )

    def load(self, ref: ArtifactRef | dict) -> str:
        return Path(ref["uri"]).read_text(encoding="utf-8")
