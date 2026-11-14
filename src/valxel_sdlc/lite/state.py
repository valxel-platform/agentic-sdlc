"""What the lite runtime remembers between calls: components, releases, bindings, environments.

OpenChoreo keeps this in the control plane's resources. In lite mode it is one JSON document,
saved whole after each change, in memory (tests) or in a file next to the tool's data.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol


def empty() -> dict:
    return {"components": {}, "runs": {}, "releases": {}, "bindings": {}, "environments": {},
            "projects": {}, "pipelines": {}, "resources": {}, "resource_bindings": [], "rollbacks": []}


class State(Protocol):
    def load(self) -> dict: ...
    def save(self, data: dict) -> None: ...


class MemoryState:
    def __init__(self) -> None:
        self._data = empty()

    def load(self) -> dict:
        return json.loads(json.dumps(self._data))

    def save(self, data: dict) -> None:
        self._data = json.loads(json.dumps(data))


class FileState:
    """A JSON file, replaced atomically so a crash mid-write never leaves half a document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return empty()
        data = json.loads(self.path.read_text())
        return {**empty(), **data}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".lite-state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
