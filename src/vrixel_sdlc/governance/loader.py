"""Load a lane's governance (platform constitution + its manifest, constitution and context, plus
the governance of every lane it depends on) and pin all of it by commit SHA.

At feature intake the engine calls `snapshot(product_id)`, which resolves the configured refs to
commit SHAs and returns the pins. Every later phase calls `snapshot(product_id, pins)`, so the
feature runs under one set of constitutions from intake to release even if a lane amends its
governance repo meanwhile. Snapshots are cached by SHA.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
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
    pins: dict  # {"platform": {...}, "product": {...}, "dependencies": {lane: {...}}}
    constitution_text: str = ""  # this lane's own constitution file, as read
    dependencies: dict[str, "GovernanceSnapshot"] = field(default_factory=dict)
    aws_catalog: dict = field(default_factory=dict)  # the platform's AWS services (VX-19), empty if none
    alert_rules: dict = field(default_factory=dict)  # the platform's default OpenChoreo alert rules, empty if none
    remediation: dict = field(default_factory=dict)  # what the tool may do in production by itself (ops/remediation.py)

    @property
    def version(self) -> str:
        return self.constitution.version

    def lanes(self) -> dict[str, ProductConfig]:
        """This lane and every lane it depends on, by id."""
        return {self.product.product_id: self.product, **{k: s.product for k, s in self.dependencies.items()}}

    def lane_of_repo(self) -> dict[str, ProductConfig]:
        return {r.name: lane for lane in self.lanes().values() for r in lane.repos}


class GovernanceLoader:
    def __init__(self, settings: Settings, source: GovernanceSource) -> None:
        self.settings = settings
        self.source = source
        self._cache: dict[tuple, GovernanceSnapshot] = {}
        self._lock = threading.Lock()

    def _pin(self, pointer) -> dict:
        return {"org": pointer.org, "repo": pointer.repo, "ref": pointer.ref,
                "sha": self.source.resolve(pointer.org, pointer.repo, pointer.ref)}

    def current_pins(self, product_id: str) -> dict:
        entry = self.settings.entry(product_id)
        product_pin = self._pin(entry.governance)
        manifest = self.source.read(product_pin["org"], product_pin["repo"], MANIFEST, product_pin["sha"])
        product, _ = parse_manifest(manifest, entry, self.settings.limits, self.settings.registry,
                                    self.settings.tools, self.settings.quality, self.settings.repos)
        return {
            "platform": self._pin(self.settings.platform),
            "product": product_pin,
            "dependencies": {d: self._pin(self.settings.entry(d).governance) for d in product.depends_on},
        }

    def snapshot(self, product_id: str, pins: dict | None = None, *, _as_dependency: bool = False) -> GovernanceSnapshot:
        entry = self.settings.entry(product_id)
        pins = pins or self.current_pins(product_id)
        plat, prod = pins["platform"], pins["product"]
        deps_pins = pins.get("dependencies") or {}
        if (prod["org"], prod["repo"]) != (entry.governance.org, entry.governance.repo):
            raise GovernanceError(
                f"{product_id} is pinned to {prod['org']}/{prod['repo']}, but the registry now points at "
                f"{entry.governance.full_name}"
            )
        key = (product_id, plat["sha"], prod["sha"], tuple(sorted((d, p["sha"]) for d, p in deps_pins.items())),
               _as_dependency)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        s = self.source
        manifest = s.read(prod["org"], prod["repo"], MANIFEST, prod["sha"])
        product, files = parse_manifest(manifest, entry, self.settings.limits, self.settings.registry,
                                    self.settings.tools, self.settings.quality, self.settings.repos)
        own_text = s.read(prod["org"], prod["repo"], files["constitution"], prod["sha"])

        dependencies: dict[str, GovernanceSnapshot] = {}
        if not _as_dependency:  # one level: a dependency's own dependencies are not pulled in
            for dep in product.depends_on:
                if dep not in deps_pins:
                    raise GovernanceError(f"{product_id} depends on {dep}, but the feature has no pin for it")
                dependencies[dep] = self.snapshot(dep, {"platform": plat, "product": deps_pins[dep]},
                                                  _as_dependency=True)
        try:
            constitution = compose(
                s.read(plat["org"], plat["repo"], self.settings.platform_constitution_path, plat["sha"]),
                own_text,
                platform_sha=plat["sha"], product_sha=prod["sha"],
                dependencies=[(d.product.name, d.constitution_text, d.pins["product"]["sha"])
                              for d in dependencies.values()],
            )
        except ConstitutionError as exc:
            raise GovernanceError(f"{entry.governance.full_name}: {exc}") from exc
        context = s.read(prod["org"], prod["repo"], files["context"], prod["sha"], optional=True) or ""
        from ..resources import CatalogError, check_aws, load_catalog

        try:
            catalog = load_catalog(s.read(plat["org"], plat["repo"], self.settings.platform_resources_path,
                                          plat["sha"], optional=True) or "")
        except (CatalogError, ValueError) as exc:
            raise GovernanceError(f"{plat['org']}/{plat['repo']}: {exc}") from exc
        from ..ops.alert_rules import AlertRulesError, load as load_rules

        try:
            alert_rules = load_rules(s.read(plat["org"], plat["repo"], self.settings.platform_alert_rules_path,
                                            plat["sha"], optional=True) or "")
        except (AlertRulesError, ValueError) as exc:
            raise GovernanceError(f"{plat['org']}/{plat['repo']}: {exc}") from exc
        from ..ops import error_tracking

        try:
            tracking = error_tracking.load_policy(s.read(plat["org"], plat["repo"],
                                                         self.settings.platform_error_tracking_path,
                                                         plat["sha"], optional=True) or "")
        except (error_tracking.PolicyError, ValueError) as exc:
            raise GovernanceError(f"{plat['org']}/{plat['repo']}: {exc}") from exc
        from ..ops import remediation as remediation_mod

        try:
            remediation = remediation_mod.load_policy(s.read(plat["org"], plat["repo"],
                                                             self.settings.platform_remediation_path,
                                                             plat["sha"], optional=True) or "")
        except (remediation_mod.PolicyError, ValueError) as exc:
            raise GovernanceError(f"{plat['org']}/{plat['repo']}: {exc}") from exc
        runtime = product.runtime or {}
        problems = check_aws(runtime.get("aws"), product.data_class, list(runtime.get("pipeline") or []), catalog)
        problems += error_tracking.check(runtime, product.data_class, tracking)
        problems += remediation_mod.check(runtime)
        if problems:
            raise GovernanceError(f"{entry.governance.full_name}/product.yaml: " + "; ".join(problems))
        snap = GovernanceSnapshot(
            product=product, constitution=constitution, product_context=context,
            pins={"platform": dict(plat), "product": dict(prod),
                  "dependencies": {d: dict(p) for d, p in deps_pins.items()} if not _as_dependency else {}},
            constitution_text=own_text, dependencies=dependencies, aws_catalog=catalog, alert_rules=alert_rules,
            remediation=remediation_mod.for_product(remediation, runtime, product.data_class,
                                                    self.settings.data_classes),
        )
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
