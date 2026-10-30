"""Load a product's governance (platform constitution + product manifest, constitution and context)
and pin it by commit SHA.

At feature intake the engine calls `snapshot(product_id)`, which resolves the configured refs to
commit SHAs and returns the pins. Every later phase calls `snapshot(product_id, pins)`, so the
feature runs under one constitution from intake to release even if a product org amends its
governance repo meanwhile. Snapshots are cached by SHA.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import ProductConfig, Settings
from ..constitution import Constitution, ConstitutionError, compose
from .errors import GovernanceError
from .manifest import parse_manifest
from .sources import (
    EnvTokenProvider,
    GitHubAppTokenProvider,
    GitHubSource,
    GovernanceSource,
    LocalSource,
)

MANIFEST = "product.yaml"


@dataclass(frozen=True)
class GovernanceSnapshot:
    product: ProductConfig
    constitution: Constitution
    product_context: str
    pins: dict  # {"platform": {...}, "product": {...}} each with org, repo, sha

    @property
    def version(self) -> str:
        return self.constitution.version


class GovernanceLoader:
    def __init__(self, settings: Settings, source: GovernanceSource) -> None:
        self.settings = settings
        self.source = source
        self._cache: dict[tuple[str, str, str], GovernanceSnapshot] = {}
        self._lock = threading.Lock()

    def current_pins(self, product_id: str) -> dict:
        entry = self.settings.entry(product_id)
        plat, prod = self.settings.platform, entry.governance
        return {
            "platform": {"org": plat.org, "repo": plat.repo, "ref": plat.ref,
                         "sha": self.source.resolve(plat.org, plat.repo, plat.ref)},
            "product": {"org": prod.org, "repo": prod.repo, "ref": prod.ref,
                        "sha": self.source.resolve(prod.org, prod.repo, prod.ref)},
        }

    def snapshot(self, product_id: str, pins: dict | None = None) -> GovernanceSnapshot:
        entry = self.settings.entry(product_id)
        pins = pins or self.current_pins(product_id)
        plat, prod = pins["platform"], pins["product"]
        if (prod["org"], prod["repo"]) != (entry.governance.org, entry.governance.repo):
            raise GovernanceError(
                f"{product_id} is pinned to {prod['org']}/{prod['repo']}, but the registry now points at "
                f"{entry.governance.full_name}"
            )
        key = (product_id, plat["sha"], prod["sha"])
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        s = self.source
        manifest = s.read(prod["org"], prod["repo"], MANIFEST, prod["sha"])
        product, files = parse_manifest(manifest, entry, self.settings.limits)
        try:
            constitution = compose(
                s.read(plat["org"], plat["repo"], self.settings.platform_constitution_path, plat["sha"]),
                s.read(prod["org"], prod["repo"], files["constitution"], prod["sha"]),
                platform_sha=plat["sha"], product_sha=prod["sha"],
            )
        except ConstitutionError as exc:
            raise GovernanceError(f"{entry.governance.full_name}: {exc}") from exc
        context = s.read(prod["org"], prod["repo"], files["context"], prod["sha"], optional=True) or ""
        snap = GovernanceSnapshot(product=product, constitution=constitution, product_context=context,
                                  pins={"platform": dict(plat), "product": dict(prod)})
        with self._lock:
            self._cache[key] = snap
        return snap


def build_source(settings: Settings) -> GovernanceSource:
    if settings.governance_source == "local":
        return LocalSource(settings.governance_root)
    if settings.governance_source == "github":
        app_id = os.environ.get("VRIXEL_GITHUB_APP_ID")
        key_file = os.environ.get("VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE")
        if app_id and key_file:
            tokens = GitHubAppTokenProvider(app_id, Path(key_file).read_text(), api=settings.github_api)
        else:
            tokens = EnvTokenProvider()
        return GitHubSource(tokens, api=settings.github_api)
    raise GovernanceError(f"Unknown governance source {settings.governance_source!r}; use local or github")
