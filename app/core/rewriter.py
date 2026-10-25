"""HTML/CSS parsing: extract every reference, then rewrite them to relative paths.

Extraction and rewriting share the same attribute registry, so the set of URLs
the crawler discovers is exactly the set the rewriter can fix. The resolver is
injected by the caller (crawler) and answers "give me the output-relative path
of this absolute URL, or None to leave the attribute untouched".

Inline CSS (<style> blocks and style="..." attributes) is rewritten with a
regex pass on the serialized document instead of through the DOM, which avoids
entity-escaping issues in text nodes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:            # older selectolax
    from selectolax.parser import HTMLParser

from app.utils.url_utils import build_srcset, parse_srcset, rel_for_attr

# tag -> attribute that is a *page-ish* reference (crawled, may be followed)
LINK_ATTRS: dict[str, str] = {
    "a": "href",
    "area": "href",
    "iframe": "src",        # page-like: crawled when same-site
    "form": "action",
}
# tag -> attributes that are assets (downloaded, not crawled)
ASSET_ATTRS: dict[str, frozenset[str]] = {
    "img": frozenset({"src", "data-src", "srcset", "data-srcset", "lowsrc"}),
    "source": frozenset({"src", "srcset"}),
    "video": frozenset({"src", "poster"}),
    "audio": frozenset({"src"}),
    "track": frozenset({"src"}),
    "embed": frozenset({"src"}),
    "object": frozenset({"data"}),
    "script": frozenset({"src"}),
    "input": frozenset({"src"}),
    "body": frozenset({"background"}),
}
_LINK_PAGE_RELS = {"canonical"}
_CSS_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)</style\s*>", re.I | re.S)
_STYLE_ATTR_RE = re.compile(r"style\s*=\s*([\"'])(.*?)\1", re.I)
_DOCTYPE_RE = re.compile(r"^\s*(<!DOCTYPE[^>]*>)", re.I)

_CSS_URL_RE = re.compile(r"url\(\s*([\"']?)([^)\"']*)\1\s*\)", re.I)
_CSS_IMPORT_RE = re.compile(r"@import\s+([\"'])([^\"']+)\1", re.I)

SKIP_PREFIXES = (
    "data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "sms:",
    "magnet:", "ftp:", "file:", "ws:", "wss:", "chrome:", "intent:", "view-source:",
)


@dataclass
class LinkRef:
    url: str          # raw attribute text
    kind: str         # "page" | "asset"
    tag: str
    attr: str
    rel: str = ""


@dataclass
class Extraction:
    links: list[LinkRef] = field(default_factory=list)
    assets: list[LinkRef] = field(default_factory=list)
    base_href: str | None = None


def _skippable(url: str) -> bool:
    u = (url or "").strip().lower()
    return (not u) or u == "#" or u.startswith(SKIP_PREFIXES)


def extract_html(html: str) -> Extraction:
    """Discover every reference in an HTML document (raw attr values)."""
    try:
        tree = HTMLParser(html)
    except Exception:
        return Extraction()

    out = Extraction()
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        out.base_href = (base_node.attrs.get("href") or "").strip() or None

    for tag, attr in LINK_ATTRS.items():
        for node in tree.css(f"{tag}[{attr}]"):
            raw = (node.attrs.get(attr) or "").strip()
            if raw and not _skippable(raw):
                out.links.append(LinkRef(raw, "page", tag, attr))

    for node in tree.css("link[href]"):
        raw = (node.attrs.get("href") or "").strip()
        if not raw or _skippable(raw):
            continue
        rel = (node.attrs.get("rel") or "").strip().lower()
        kind = "page" if rel in _LINK_PAGE_RELS else "asset"
        out.assets.append(LinkRef(raw, kind, "link", "href", rel))

    for tag, attrs in ASSET_ATTRS.items():
        for node in tree.css(tag):
            for attr in attrs:
                raw = node.attrs.get(attr) or ""
                if not raw.strip() or _skippable(raw):
                    continue
                if attr.endswith("srcset"):
                    for u, _desc in parse_srcset(raw):
                        out.assets.append(LinkRef(u, "asset", tag, attr))
                else:
                    out.assets.append(LinkRef(raw, "asset", tag, attr))

    for node in tree.css("meta[http-equiv]"):
        if (node.attrs.get("http-equiv") or "").strip().lower() == "refresh":
            content = node.attrs.get("content") or ""
            m = re.search(r"url\s*=\s*[\"']?\s*([^\"'>\s;]+)", content, re.I)
            if m:
                dm = re.match(r"\s*(\d+)", content)
                try:
                    delay = int(dm.group(1)) if dm else 0
                except ValueError:
                    delay = 0
                if delay <= 15:
                    out.links.append(LinkRef(m.group(1), "page", "meta", "refresh"))
    return out


# resolver: (abs_url, kind) -> output-rel path (str) | None (leave untouched)

def rewrite_html(
    html: str,
    page_url: str,
    page_rel: str,
    resolver,
    doctype: str | None = None,
) -> str:
    """Return the offline copy of *html* with every URL rewritten.

    *page_rel* is the output path of the saved HTML file itself; all rewritten
    URLs are relative to its directory. The <base> tag is removed, otherwise
    relative URLs would resolve against its href instead of the file's dir.
    """
    try:
        tree = HTMLParser(html)
    except Exception:
        return html

    base_url = page_url
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        bh = (base_node.attrs.get("href") or "").strip()
        if bh:
            base_url = absolutize(bh, page_url) or page_url
        base_node.decompose()

    def rewrite_attr(node, attr: str, kind: str, srcset: bool = False) -> None:
        raw = node.attrs.get(attr)
        if raw is None:
            return
        raw = str(raw).strip()
        if srcset:
            items = parse_srcset(raw)
            if not items:
                return
            rebuilt = []
            for u, descriptor in items:
                if _skippable(u):
                    rebuilt.append((u, descriptor))
                    continue
                absu = absolutize(u, base_url)
                if not absu:
                    rebuilt.append((u, descriptor))
                    continue
                tgt = resolver(absu, kind)
                rebuilt.append((rel_for_attr(page_rel, tgt) if tgt else u, descriptor))
            node.attrs[attr] = build_srcset(rebuilt)
            return
        if _skippable(raw):
            return
        absu = absolutize(raw, base_url)
        if not absu:
            return
        tgt = resolver(absu, kind)
        if tgt is not None:
            node.attrs[attr] = rel_for_attr(page_rel, tgt)
        elif not raw.lower().startswith(("http://", "https://", "//")):
            # In-mirror-or-not decision failed, yet the attribute was relative:
            # keep it absolute so it doesn't break against the mirror's layout.
            node.attrs[attr] = absu

    for tag, attr in LINK_ATTRS.items():
        for node in tree.css(f"{tag}[{attr}]"):
            rewrite_attr(node, attr, "page")
    for node in tree.css("link[href]"):
        raw = (node.attrs.get("href") or "").strip()
        if not raw or _skippable(raw):
            continue
        rel = (node.attrs.get("rel") or "").strip().lower()
        kind = "page" if rel in _LINK_PAGE_RELS else "asset"
        absu = absolutize(raw, base_url)
        if absu:
            tgt = resolver(absu, kind)
            if tgt is not None:
                node.attrs["href"] = rel_for_attr(page_rel, tgt)
    for tag, attrs in ASSET_ATTRS.items():
        for node in tree.css(tag):
            for attr in attrs:
                rewrite_attr(node, attr, "asset", srcset=attr in ("srcset", "data-srcset"))

    out = tree.html

    # inline CSS via regex on the serialized document
    out = _CSS_BLOCK_RE.sub(
        lambda m: m.group(1) + rewrite_css(m.group(2), base_url, resolver, page_rel) + "</style>",
        out,
    )
    out = _STYLE_ATTR_RE.sub(
        lambda m: f"style={m.group(1)}{rewrite_css(m.group(2), base_url, resolver, page_rel)}{m.group(1)}",
        out,
    )

    if doctype is None:
        m = _DOCTYPE_RE.match(html)
        doctype = m.group(1) if m else "<!DOCTYPE html>"
    if not out.lstrip().lower().startswith("<!doctype"):
        out = doctype + "\n" + out
    return out


def extract_css(css_text: str) -> list[str]:
    urls: list[str] = []
    for regex in (_CSS_URL_RE, _CSS_IMPORT_RE):
        for m in regex.finditer(css_text):
            u = m.group(2).strip()
            if u and not u.lower().startswith(SKIP_PREFIXES):
                urls.append(u)
    return urls


def extract_style_urls(html: str) -> list[str]:
    """URLs referenced from inline CSS: <style> blocks + style=\"...\" attrs."""
    out: list[str] = []
    for m in _CSS_BLOCK_RE.finditer(html):
        out.extend(extract_css(m.group(2)))
    for m in _STYLE_ATTR_RE.finditer(html):
        out.extend(extract_css(m.group(2)))
    return out


def rewrite_css(css_text: str, base_url: str, resolver, css_rel: str = "") -> str:
    """Rewrite url()/​@import targets. *css_rel* is the containing file's
    output path ("" for content rewritten relative to the root)."""

    def _map(match: re.Match) -> str:
        url = match.group(2).strip()
        if not url or url.lower().startswith(SKIP_PREFIXES):
            return match.group(0)
        absu = absolutize(url, base_url)
        if not absu:
            return match.group(0)
        tgt = resolver(absu, "asset")
        if tgt is None:
            # not in the mirror (out-of-scope or failed download): like the
            # HTML rewriter, turn a relative ref into its absolute URL so it
            # still works against the live site instead of 404ing locally
            if not url.lower().startswith(("http://", "https://", "//")):
                return f"url({absu})"
            return match.group(0)
        q = match.group(1) if match.group(1) in ("'", '"') else ""
        return f"url({q}{rel_for_attr(css_rel or '', tgt)}{q})"

    text = _CSS_URL_RE.sub(_map, css_text)

    def _map_import(match: re.Match) -> str:
        url = match.group(2).strip()
        if not url or url.lower().startswith(SKIP_PREFIXES):
            return match.group(0)
        absu = absolutize(url, base_url)
        if not absu:
            return match.group(0)
        tgt = resolver(absu, "asset")
        if tgt is None:
            if not url.lower().startswith(("http://", "https://", "//")):
                return f"url({absu})"
            return match.group(0)
        return f"url({rel_for_attr(css_rel or '', tgt)})"

    return _CSS_IMPORT_RE.sub(_map_import, text)


# --- helpers ----------------------------------------------------------------

def absolutize(url: str, base_url: str) -> str | None:
    from urllib.parse import urljoin

    if not base_url:
        return None
    try:
        joined = urljoin(base_url, url.strip())
    except Exception:
        return None
    return joined if joined.lower().startswith(("http://", "https://")) else None
