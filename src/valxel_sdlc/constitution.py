"""Constitutions: the platform constitution (tools org) plus a product constitution (product org).

Precedence: platform, then product, then ADRs, then PRD and task instructions. A product
constitution may add articles but may not redefine platform (VX-) articles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FRONT = re.compile(r"^---\n(.*?)\n---\n", re.S)
_ARTICLE = re.compile(r"\*\*([A-Z]{2,4}-\d+)\*\*")


class ConstitutionError(ValueError):
    pass


@dataclass(frozen=True)
class Constitution:
    text: str
    version: str  # e.g. "platform@1.0.0#a1b2c3d+cln@1.0.0#9f8e7d6"
    article_ids: tuple[str, ...]

    def product_articles(self) -> tuple[str, ...]:
        return tuple(a for a in self.article_ids if not a.startswith("VX-"))


def split_front_matter(raw: str) -> tuple[dict, str]:
    meta: dict = {}
    m = _FRONT.match(raw)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    return meta, raw.strip()


def compose(platform_text: str, product_text: str, *, platform_sha: str, product_sha: str,
            dependencies: list[tuple[str, str, str]] = ()) -> Constitution:
    """Platform first, then the product, then each dependency lane's constitution (label, raw
    text, commit SHA), which applies to changes the feature makes in that lane's repos."""
    p_meta, p_body = split_front_matter(platform_text)
    meta, body = split_front_matter(product_text)
    deps = [(label, *split_front_matter(text), sha) for label, text, sha in dependencies]

    platform_ids = _ARTICLE.findall(p_body)
    product_ids = _ARTICLE.findall(body)
    lane_ids = product_ids + [a for _, _, b, _ in deps for a in _ARTICLE.findall(b)]
    problems = []
    if not platform_ids:
        problems.append("platform constitution defines no articles")
    if not product_ids:
        problems.append("product constitution defines no articles")
    for label, _, dep_body, _ in deps:
        if not _ARTICLE.findall(dep_body):
            problems.append(f"{label} constitution defines no articles")
    if overriding := sorted({a for a in lane_ids if a.startswith("VX-")}):
        problems.append(f"product constitution may not define platform articles: {', '.join(overriding)}")
    dupes = sorted({a for a in lane_ids if lane_ids.count(a) > 1})
    if dupes:
        problems.append(f"duplicate article ids: {', '.join(dupes)}")
    if problems:
        raise ConstitutionError("; ".join(problems))

    version = (
        f"{p_meta.get('name', 'platform')}@{p_meta.get('version', '0.0.0')}#{platform_sha[:7]}"
        f"+{meta.get('name', 'product')}@{meta.get('version', '0.0.0')}#{product_sha[:7]}"
    )
    combined = f"{p_body}\n\n{body}\n"
    for label, d_meta, d_body, sha in deps:
        version += f"+{d_meta.get('name', label)}@{d_meta.get('version', '0.0.0')}#{sha[:7]}"
        combined += f"\n\n## {label}: applies to changes in its repos\n\n{d_body}\n"
    return Constitution(text=combined, version=version,
                        article_ids=tuple(dict.fromkeys(platform_ids + lane_ids)))
