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
        """The thread's folder in its workspace's (valxel_sdlc.tenancy: files)."""
        from .tenancy import thread_folder

        if not _SAFE.match(feature_id):
            raise ValueError(f"Unsafe feature id {feature_id!r}")
        d = thread_folder(self.root, feature_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_of(self, ref: ArtifactRef | dict) -> Path:
        """Where a stored version is, once this request's scope may read its thread's workspace."""
        from .tenancy import check_file

        return check_file(self.root, ref["uri"], str(ref["id"]).split(".", 1)[0])

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
        return self.path_of(ref).read_text(encoding="utf-8")
