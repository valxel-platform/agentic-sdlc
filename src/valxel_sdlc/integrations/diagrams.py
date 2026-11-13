"""Architecture diagrams: the architect writes one Mermaid flowchart in the HLD, and the
product's diagram provider publishes it where people review designs.

- lucid: the Lucid MCP server imports the Mermaid source as an editable Lucidchart document,
  then creates a view-only share link restricted to the Lucid account.
- drawio: the flowchart is converted to a native .drawio file and committed to the feature
  branch of the product's provider repo, so the diagram is reviewed and merged with the code.
  The link opens that diagram in the draw.io editor (the same #create link the draw.io MCP
  server builds). draw.io's MCP servers run beside a person's client and store nothing, so a
  server-side pipeline writes the file itself.

Mermaid is the exchange format because models write it reliably, Lucid imports it, GitHub
renders it, and its flowchart subset maps cleanly onto draw.io cells.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import threading
import zlib
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote
from xml.sax.saxutils import escape

from .mcp_client import McpError, McpTools

# --------------------------------------------------------------------------- Mermaid flowcharts


@dataclass
class Flowchart:
    direction: str = "LR"
    nodes: dict[str, dict] = field(default_factory=dict)  # id -> {"label", "group", "shape"}
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (source, target, label)
    groups: dict[str, str] = field(default_factory=dict)  # id -> label, in order of appearance


_HEADER = re.compile(r"^(?:flowchart|graph)\s+(TB|TD|BT|LR|RL)\s*$")
_SUBGRAPH = re.compile(r'^subgraph\s+([A-Za-z0-9_-]+)\s*(?:\[\s*"?(.*?)"?\s*\])?\s*$')
_NODE = r'([A-Za-z0-9_-]+)\s*(\[\(|\(\(|\[|\(|\{)?\s*(?:"([^"]*)"|([^\]\)\}"]*))?\s*(?:\)\]|\)\)|\]|\)|\})?'
_NODE_RE = re.compile(rf"^{_NODE}$")
_EDGE = re.compile(r"\s*(-->|-\.->|==>|---)\s*(?:\|\s*\"?([^|\"]*)\"?\s*\|)?\s*")
_LABELED_EDGE = re.compile(r'\s*--\s+"?([^">-][^">]*?)"?\s+-->\s*')
_SHAPES = {"[(": "database", "((": "circle", "(": "rounded", "{": "decision", "[": "box"}


def extract_mermaid(markdown: str) -> str:
    m = re.search(r"```mermaid\n(.*?)\n```", markdown, re.S)
    return m.group(1).strip() if m else ""


def parse_flowchart(source: str) -> Flowchart:
    """Parse the flowchart subset agents are asked to write: one direction header, subgraphs,
    node declarations and edges, optionally labelled. Other lines are ignored."""
    fc, group = Flowchart(), None

    def node(token: str) -> str | None:
        m = _NODE_RE.match(token.strip())
        if not m:
            return None
        nid, opener, quoted, bare = m.groups()
        label = quoted if quoted is not None else (bare or "").strip()
        existing = fc.nodes.get(nid)
        if existing is None:
            fc.nodes[nid] = {"label": label or nid, "group": group, "shape": _SHAPES.get(opener or "[", "box")}
        elif label:
            existing["label"] = label
            existing["shape"] = _SHAPES.get(opener or "[", existing["shape"])
        return nid

    for raw in source.splitlines():
        line = raw.strip().rstrip(";")
        if not line or line.startswith("%%") or line.startswith(("classDef", "class ", "style ", "linkStyle")):
            continue
        if m := _HEADER.match(line):
            fc.direction = m.group(1).replace("TD", "TB")
            continue
        if m := _SUBGRAPH.match(line):
            group = m.group(1)
            fc.groups[group] = m.group(2) or group
            continue
        if line == "end":
            group = None
            continue
        line = _LABELED_EDGE.sub(lambda m: f' -->|{m.group(1).strip()}| ', line)
        parts = _EDGE.split(line)
        # parts: node, arrow, label, node, arrow, label, node ...
        ids = [node(parts[i]) for i in range(0, len(parts), 3)]
        labels = [parts[i + 2] or "" for i in range(0, len(parts) - 1, 3)]
        for i, label in enumerate(labels):
            if ids[i] and ids[i + 1]:
                fc.edges.append((ids[i], ids[i + 1], label.strip()))
    return fc


def to_mermaid(fc: Flowchart) -> str:
    out = [f"flowchart {fc.direction}"]
    for gid, glabel in fc.groups.items():
        out.append(f'  subgraph {gid}["{glabel}"]')
        out += [f'    {nid}["{n["label"]}"]' for nid, n in fc.nodes.items() if n["group"] == gid]
        out.append("  end")
    out += [f'  {nid}["{n["label"]}"]' for nid, n in fc.nodes.items() if not n["group"]]
    out += [f'  {a} -->|"{lbl}"| {b}' if lbl else f"  {a} --> {b}" for a, b, lbl in fc.edges]
    return "\n".join(out)


# --------------------------------------------------------------------------- draw.io

_W, _H, _GAP, _PAD_TOP, _PAD = 200, 60, 40, 40, 20
_STYLE = {
    "box": "rounded=0;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "rounded": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "database": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;size=12;fillColor=#fff2cc;strokeColor=#d6b656;",
    "circle": "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
    "decision": "rhombus;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;",
}
_GROUP_STYLE = "swimlane;whiteSpace=wrap;html=1;startSize=30;fillColor=#f5f5f5;strokeColor=#666666;rounded=1;"
_EDGE_STYLE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;"


def to_drawio(fc: Flowchart, title: str) -> str:
    """A native .drawio file: one column (LR) or row (TB) per subgraph, nodes stacked inside."""
    horizontal = fc.direction in ("LR", "RL")
    buckets: list[tuple[str | None, list[str]]] = [
        (gid, [n for n, v in fc.nodes.items() if v["group"] == gid]) for gid in fc.groups]
    loose = [n for n, v in fc.nodes.items() if not v["group"]]
    if loose:
        buckets.append((None, loose))
    cells, pos = [], {}
    offset = _PAD
    for gid, members in buckets:
        inner = len(members) * _H + max(len(members) - 1, 0) * _GAP
        if gid is not None:
            w = _W + 2 * _PAD if horizontal else inner + 2 * _PAD
            h = inner + _PAD_TOP + _PAD if horizontal else _H + _PAD_TOP + _PAD
            x, y = (offset, _PAD) if horizontal else (_PAD, offset)
            cells.append(f'<mxCell id="g_{escape(gid)}" value="{escape(fc.groups[gid], {chr(34): "&quot;"})}" '
                         f'style="{_GROUP_STYLE}" vertex="1" parent="1">'
                         f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
            parent, base_x, base_y = f"g_{escape(gid)}", _PAD, _PAD_TOP
            offset += (w if horizontal else h) + _GAP * 2
        else:
            parent = "1"
            base_x, base_y = (offset, _PAD_TOP + _PAD) if horizontal else (_PAD, offset)
            offset += (_W if horizontal else _H) + _GAP * 2
        for i, nid in enumerate(members):
            step = i * (_H + _GAP)
            x, y = (base_x, base_y + step) if horizontal else (base_x + i * (_W + _GAP), base_y)
            n = fc.nodes[nid]
            pos[nid] = parent
            cells.append(f'<mxCell id="n_{escape(nid)}" value="{escape(n["label"], {chr(34): "&quot;"})}" '
                         f'style="{_STYLE.get(n["shape"], _STYLE["box"])}" vertex="1" parent="{parent}">'
                         f'<mxGeometry x="{x}" y="{y}" width="{_W}" height="{_H}" as="geometry"/></mxCell>')
    for i, (a, b, label) in enumerate(fc.edges):
        cells.append(f'<mxCell id="e_{i}" value="{escape(label, {chr(34): "&quot;"})}" style="{_EDGE_STYLE}" '
                     f'edge="1" parent="1" source="n_{escape(a)}" target="n_{escape(b)}">'
                     f'<mxGeometry relative="1" as="geometry"/></mxCell>')
    model = ('<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
             'arrows="1" fold="1" page="1" pageScale="1" pageWidth="1169" pageHeight="827"><root>'
             '<mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(cells) + "</root></mxGraphModel>")
    return (f'<mxfile host="valxel-sdlc"><diagram id="architecture" '
            f'name="{escape(title, {chr(34): "&quot;"})}">{model}</diagram></mxfile>\n')


def _js_encode(text: str) -> str:
    return quote(text, safe="-_.!~*'()")  # JavaScript encodeURIComponent


def drawio_editor_url(xml: str, base: str = "https://app.diagrams.net/") -> str:
    """Open the diagram in the draw.io editor: #create with deflateRaw + base64 data, as
    draw.io's Graph.compress produces it."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = deflate.compress(_js_encode(xml).encode()) + deflate.flush()
    payload = {"type": "xml", "compressed": True, "data": base64.b64encode(raw).decode()}
    return f"{base}?grid=0&pv=0&border=10&edit=_blank#create={_js_encode(json.dumps(payload, separators=(',', ':')))}"


def decode_editor_url(url: str) -> str:
    """Inverse of drawio_editor_url, for tests and debugging."""
    from urllib.parse import unquote

    payload = json.loads(unquote(url.split("#create=", 1)[1]))
    return unquote(zlib.decompress(base64.b64decode(payload["data"]), -15).decode())


# --------------------------------------------------------------------------- publishers


@dataclass
class DiagramRequest:
    feature_id: str
    title: str  # e.g. "FARM-FEAT-2 Offline crop log: architecture v1"
    mermaid: str
    repo: str  # the provider repo whose feature branch receives drawio files
    branch: str
    version: int
    settings: dict = field(default_factory=dict)  # the product's tools.diagrams block


class DiagramPublisher(Protocol):
    provider: str

    def publish(self, req: DiagramRequest) -> dict: ...


class DrawioPublisher:
    provider = "drawio"

    def __init__(self, github) -> None:
        self.github = github

    def publish(self, req: DiagramRequest) -> dict:
        fc = parse_flowchart(req.mermaid)
        if not fc.nodes:
            raise ValueError("the HLD's diagram has no nodes")
        xml = to_drawio(fc, req.title)
        folder = req.settings.get("path", "docs/architecture").strip("/")
        path = f"{folder}/{req.feature_id}.drawio"
        commit = self.github.put_file(repo=req.repo, branch=req.branch, path=path, content=xml,
                                      message=f"{req.feature_id}: architecture diagram v{req.version}")
        return {"provider": self.provider, "title": req.title, "version": req.version,
                "url": drawio_editor_url(xml), "file": f"{req.repo}/blob/{req.branch}/{path}",
                "file_url": f"https://github.com/{req.repo}/blob/{req.branch}/{path}",
                "id": commit.get("sha", "")}


def _first(d: Any, *keys: str) -> str:
    if isinstance(d, dict):
        for k in keys:
            if d.get(k):
                return str(d[k])
        for v in d.values():  # some servers nest the document under a key
            found = _first(v, *keys)
            if found:
                return found
    return ""


class LucidPublisher:
    """Lucid MCP server: import the Mermaid as a Lucidchart document, tag it with the feature,
    and share it view-only within the Lucid account so gate reviewers can open it."""

    provider = "lucid"
    CREATE = "lucid_create_diagram_from_mermaid"
    SHARE = "lucid_create_document_share_link"
    UPDATE = "lucid_update_document"

    def __init__(self, server: McpTools) -> None:
        self.server = server

    def publish(self, req: DiagramRequest) -> dict:
        res = self.server.call(self.CREATE, {"title": req.title, "mermaid_markup": req.mermaid,
                                             "product": "lucidchart"})
        doc_id = _first(res, "documentId", "document_id", "id")
        edit_url = _first(res, "editUrl", "edit_url", "url")
        if not doc_id:
            raise McpError(f"lucid: no document id in {str(res)[:200]}")
        try:  # tags let people find every diagram of a feature; not required for the gate
            self.server.call(self.UPDATE, {"document_id": doc_id, "custom_tags": ["valxel", req.feature_id]})
        except McpError:
            pass
        url = edit_url
        role = req.settings.get("share", "view")
        if role:
            link = self.server.call(self.SHARE, {"document_id": doc_id, "role": role, "restrict_to_account": True})
            url = link if isinstance(link, str) and link.startswith("http") else _first(link, "acceptUrl", "url") or url
        return {"provider": self.provider, "title": req.title, "version": req.version, "url": url,
                "edit_url": edit_url, "id": doc_id}


class StubLucidPublisher:
    """Offline stand-in that records requests and returns Lucid-shaped links."""

    provider = "lucid"

    def __init__(self) -> None:
        self.published: list[DiagramRequest] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def publish(self, req: DiagramRequest) -> dict:
        with self._lock:
            n = next(self._seq)
            self.published.append(req)
        doc = f"00000000-0000-4000-8000-{n:012d}"
        return {"provider": self.provider, "title": req.title, "version": req.version,
                "url": f"https://lucid.app/lucidchart/{doc}/view", "edit_url": f"https://lucid.app/lucidchart/{doc}/edit",
                "id": doc, "stub": True}
