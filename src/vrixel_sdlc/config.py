"""Settings loaded from config/*.yaml and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _repo_root() -> Path:
    env = os.environ.get("VRIXEL_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RepoSpec:
    name: str
    role: str  # provider | consumer | library


@dataclass(frozen=True)
class ProductConfig:
    product_id: str
    name: str
    data_class: str
    constitution: str
    workspace: str
    budget_usd: float
    critic_threshold: float
    max_loops: int
    repos: tuple[RepoSpec, ...]
    approvers: dict[str, list[str]]
    high_risk_paths: tuple[str, ...]


@dataclass(frozen=True)
class RoleConfig:
    runtime: str  # claude_code | generic
    model: str  # "<provider>/<model-name>"
    ttl: str = "1h"

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        return self.model.split("/", 1)[-1]


@dataclass
class Settings:
    mode: str  # stub | live
    database_url: str | None
    artifact_dir: Path
    config_dir: Path
    constitution_dir: Path
    products: dict[str, ProductConfig]
    roles: dict[str, RoleConfig]
    data_classes: dict[str, list[str]]
    failover: dict[str, list[str]] = field(default_factory=dict)

    def product(self, product_id: str) -> ProductConfig:
        try:
            return self.products[product_id]
        except KeyError as exc:
            raise KeyError(f"Unknown product {product_id!r}; known: {sorted(self.products)}") from exc

    def role(self, role: str) -> RoleConfig:
        return self.roles[role]


def load_settings(
    *,
    mode: str | None = None,
    database_url: str | None = None,
    artifact_dir: str | Path | None = None,
    config_dir: str | Path | None = None,
    constitution_dir: str | Path | None = None,
) -> Settings:
    root = _repo_root()
    cfg_dir = Path(config_dir or os.environ.get("VRIXEL_CONFIG_DIR") or root / "config")
    con_dir = Path(constitution_dir or os.environ.get("VRIXEL_CONSTITUTION_DIR") or root / "constitutions")
    art_dir = Path(artifact_dir or os.environ.get("VRIXEL_ARTIFACT_DIR") or root / ".artifacts")

    products_raw = yaml.safe_load((cfg_dir / "products.yaml").read_text())["products"]
    models_raw = yaml.safe_load((cfg_dir / "models.yaml").read_text())

    products = {
        pid: ProductConfig(
            product_id=pid,
            name=p["name"],
            data_class=p["data_class"],
            constitution=p["constitution"],
            workspace=p["workspace"],
            budget_usd=float(p["budget_usd"]),
            critic_threshold=float(p.get("critic_threshold", 0.8)),
            max_loops=int(p.get("max_loops", 3)),
            repos=tuple(RepoSpec(**r) for r in p["repos"]),
            approvers={k: list(v) for k, v in p.get("approvers", {}).items()},
            high_risk_paths=tuple(p.get("high_risk_paths", [])),
        )
        for pid, p in products_raw.items()
    }
    roles = {name: RoleConfig(**spec) for name, spec in models_raw["roles"].items()}
    data_classes = {
        name: list(spec["allowed_providers"]) for name, spec in models_raw["data_classes"].items()
    }
    return Settings(
        mode=(mode or os.environ.get("VRIXEL_MODE") or "stub").lower(),
        database_url=database_url if database_url is not None else os.environ.get("DATABASE_URL"),
        artifact_dir=art_dir,
        config_dir=cfg_dir,
        constitution_dir=con_dir,
        products=products,
        roles=roles,
        data_classes=data_classes,
        failover=models_raw.get("failover") or {},
    )
