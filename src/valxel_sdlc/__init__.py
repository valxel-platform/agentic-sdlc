"""Valxel SDLC: a LangGraph pipeline of eight phases and six human gates."""

__version__ = "0.1.0"

# Settings named before the rename (VRIXEL_*) still work for now (valxel_sdlc.legacy).
from .legacy import environment as _legacy_environment  # noqa: E402

_legacy_environment()
