"""Load the platform constitution plus a product constitution (platform first, then product)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_FRONT = re.compile(r"^---\n(.*?)\n---\n", re.S)
_ARTICLE = re.compile(r"\*\*([A-Z]{2,4}-\d+)\*\*")


@dataclass(frozen=True)
class Constitution:
    text: str
    version: str  # e.g. "platform@1.0.0+emr@1.0.0"
    article_ids: tuple[str, ...]

    def product_articles(self) -> tuple[str, ...]:
        return tuple(a for a in self.article_ids if not a.startswith("VX-"))


def _read(path: Path) -> tuple[str, str, str]:
    raw = path.read_text(encoding="utf-8")
    m = _FRONT.match(raw)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    return meta.get("name", path.stem), meta.get("version", "0.0.0"), raw.strip()


@lru_cache(maxsize=64)
def load_constitution(directory: str, product_key: str) -> Constitution:
    d = Path(directory)
    p_name, p_ver, p_text = _read(d / "platform.md")
    name, ver, text = _read(d / f"{product_key}.md")
    combined = f"{p_text}\n\n{text}\n"
    return Constitution(
        text=combined,
        version=f"{p_name}@{p_ver}+{name}@{ver}",
        article_ids=tuple(dict.fromkeys(_ARTICLE.findall(combined))),
    )
