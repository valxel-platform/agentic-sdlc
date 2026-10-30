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
    version: str  # e.g. "platform@1.0.0#a1b2c3d+emr@1.0.0#9f8e7d6"
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


def compose(platform_text: str, product_text: str, *, platform_sha: str, product_sha: str) -> Constitution:
    p_meta, p_body = split_front_matter(platform_text)
    meta, body = split_front_matter(product_text)

    platform_ids = _ARTICLE.findall(p_body)
    product_ids = _ARTICLE.findall(body)
    problems = []
    if not platform_ids:
        problems.append("platform constitution defines no articles")
    if not product_ids:
        problems.append("product constitution defines no articles")
    if overriding := sorted({a for a in product_ids if a.startswith("VX-")}):
        problems.append(f"product constitution may not define platform articles: {', '.join(overriding)}")
    dupes = sorted({a for a in product_ids if product_ids.count(a) > 1})
    if dupes:
        problems.append(f"duplicate article ids: {', '.join(dupes)}")
    if problems:
        raise ConstitutionError("; ".join(problems))

    version = (
        f"{p_meta.get('name', 'platform')}@{p_meta.get('version', '0.0.0')}#{platform_sha[:7]}"
        f"+{meta.get('name', 'product')}@{meta.get('version', '0.0.0')}#{product_sha[:7]}"
    )
    combined = f"{p_body}\n\n{body}\n"
    return Constitution(text=combined, version=version, article_ids=tuple(dict.fromkeys(platform_ids + product_ids)))
