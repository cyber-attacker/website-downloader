"""URL normalization, classification and offline path mapping helpers.

The mapping from URL -> output path is a *pure* function of the normalized URL
(plus the seed host), so any component can compute an asset's on-disk location
without global state. Query strings are folded into the filename with a short
hash so distinct resources never collide, and Windows-illegal characters are
sanitized per segment.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import unicodedata
from urllib.parse import ParseResult, parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

# Extensions that make an <a>-style URL a *page* we crawl (and follow links on).
PAGE_EXTS = {
    "html", "htm", "xhtml", "shtml", "phtml", "php", "asp", "aspx", "asp",
    "jsp", "jspx", "do", "action", "cgi", "pl", "cfm", "shtm", "html5",
}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"com{i}" for i in range(1, 10)
} | {
    f"lpt{i}" for i in range(1, 10)
}
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEGMENT_MAX = 90
_REL_MAX = 235


def strip_www(host: str) -> str:
    return host[4:] if host.lower().startswith("www.") else host


def domain_base(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# ---------------------------------------------------------------------------

def normalize(
    url: str,
    base: str | None = None,
    site_host: str | None = None,
    site_scheme: str | None = None,
) -> str | None:
    """Normalize a URL; return None for anything not fetchable over http(s).

    - resolves relative URLs against *base*
    - lowercases the scheme/host, strips default ports and user info
    - drops fragments, sorts query params, collapses dot-segments
    - unifies the scheme onto *site_scheme* for URLs on *site_host* (the seed's
      own host), so http://seed and https://seed never crawl twice
    """
    if url is None:
        return None
    url = url.strip()
    if url.startswith("#"):
        return None
    if not url or not _SCHEME_RE.match(url):
        if base is None:
            return None
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not host and parts.netloc:
        return None
    if not host:
        return None
    if scheme not in ("http", "https"):
        return None
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    if site_scheme and site_host and same_host(host, site_host):
        scheme = site_scheme
    # path: resolve dot-segments
    path = _normalize_path(unquote(parts.path or "/"))
    # query: stable ordering
    try:
        qsl = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
        if qsl:
            query = "&".join(f"{k}={v}" for k, v in sorted(qsl))
        else:
            query = ""
    except Exception:
        query = parts.query
    out = urlunsplit((scheme, host, path or "/", query, ""))
    return out


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    # collapse ./ and ../ without touching percent triples already decoded;
    # the trailing slash (the directory marker) is preserved because relative
    # references resolve differently against dir-style URLs
    trailing = path.endswith("/")
    out: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    body = "/" + "/".join(out)
    if trailing and body != "/":
        return body + "/"
    return body


def doc_base(url: str) -> str:
    """Join-ready document base URL for attributes of a saved document.

    Mirrors browser semantics: a directory-style document (no extension in
    its path) is treated as ending in '/', an explicit filename is not.
    Python's urljoin pops the last path segment first for non-slash bases,
    which would otherwise shift every parent-relative reference by one
    segment."""
    path = urlsplit(url).path or "/"
    if not path.endswith("/") and ext_of(path) == "":
        return url + "/"
    return url


def same_host(a: str, b: str) -> bool:
    """Host comparison that ignores the leading www. — used for scope tests."""
    return strip_www(a.lower()) == strip_www(b.lower())


def in_scope(host: str, seed_host: str, allow_subdomains: bool = False) -> bool:
    if same_host(host, seed_host):
        return True
    if allow_subdomains:
        base = domain_base(strip_www(seed_host))
        h = strip_www(host.lower())
        return h == base or h.endswith("." + base)
    return False


def relative_resolve(url: str, base: str) -> str | None:
    normalized = normalize(url, base)
    return normalized


# ---------------------------------------------------------------------------

def ext_of(url_or_path: str) -> str:
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path
    name = posixpath.basename(path)
    if "." in name:
        return name.rsplit(".", 1)[1].lower()
    return ""


def is_page_url(url: str) -> bool:
    """A URL whose extension makes it a *page* (crawlable HTML document)."""
    e = ext_of(url)
    return e == "" or e in PAGE_EXTS


# ---------------------------------------------------------------------------
# Windows-safe path mapping

def sanitize_segment(seg: str) -> str:
    seg = unicodedata.normalize("NFC", seg)
    seg = _ILLEGAL_FS.sub("_", seg)
    seg = seg.strip()                                  # leading/trailing blanks
    # Windows reserved device names
    base = seg.split(".", 1)[0].lower()
    if base in _RESERVED_NAMES:
        seg = "_" + seg
    if len(seg) > _SEGMENT_MAX:
        h = hashlib.sha1(seg.encode("utf-8", "replace")).hexdigest()[:8]
        seg = seg[:_SEGMENT_MAX - 9].rstrip(". ") + "-" + h
    return seg.rstrip(". ") or "index"


def sanitize_query(query: str) -> str:
    """Fold a query string into a filename-safe tag."""
    q = _ILLEGAL_FS.sub("_", query)
    q = re.sub(r"\s+", "", q)
    return q[:40].strip("_= &.")  # type: ignore[return-value]


def map_url_to_rel(
    url: str,
    seed_host: str,
    allow_subdomains: bool = False,
    *,
    kind_override: str | None = None,
) -> str:
    """Pure mapping: normalized URL -> POSIX relpath under the output root.

    - same-site URLs (ignoring www.) map into the output root, preserving the
      site's own folder structure
    - foreign hosts (CDN assets) map under a folder named after the host
    - directory-style / extension-less *pages* become `<last>/index.html`
    - query strings are appended to the filename as `@<x>` with a short hash
      so distinct query variants never overwrite each other
    """
    parts = urlsplit(url)
    host = (parts.netloc or "").split("@")[-1].lower()
    raw_path = unquote(parts.path or "/")
    segs = [s for s in raw_path.split("/") if s]
    site = in_scope(host, seed_host, allow_subdomains)

    ext = ext_of(raw_path)
    kind = kind_override or ("page" if (not ext or ext in PAGE_EXTS) else "asset")

    rel_segs: list[str] = []
    if not site:
        rel_segs.append(sanitize_segment(host))

    # directory-style page? trailing slash, extension-less page target
    page_like = kind == "page"
    dir_style = page_like and (raw_path.endswith("/") or not ext)

    filename: str | None = None
    if segs:
        raw_segs = segs[:-1]
    else:
        raw_segs = []
    for s in raw_segs:
        rel_segs.append(sanitize_segment(s))

    if segs:
        last = segs[-1]
    else:
        last = ""
    if dir_style:
        if last:
            rel_segs.append(sanitize_segment(last))
        if parts.query and raw_path not in ("/", ""):
            h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:10]
            filename = f"index@{h}.html"      # directory-style page with a query
        else:
            filename = "index.html"
    elif ext:
        stem, dot, extension = last.rpartition(".")
        if not dot:
            stem, extension = last, ext
        tag = ""
        if parts.query:
            h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:10]
            tag = f"@{h}"
        filename = f"{sanitize_segment(stem + tag)}.{extension or ext}"
    else:
        # foreign asset with no extension (e.g. /api/endpoint?p=1)
        stem = last
        h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:10]
        filename = f"{sanitize_segment(stem + h)}"

    rel = "/".join([*rel_segs, filename])
    # last-resort length guard (247 is NTFS-friendly without long-path opt-in)
    if len(rel) > _REL_MAX:
        h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:12]
        trimmed = rel[:_REL_MAX - 14]
        slash = trimmed.rfind("/")
        if slash <= 0:
            rel = f"__long/{h}"
        else:
            rel = trimmed[:slash] + f"__long/{h}-{trimmed[slash+1:][:40]}"
    return rel


def rel_for_attr(from_rel: str, to_rel: str) -> str:
    """Relative link string from one output file to another, URL-encoded.

    Both arguments are POSIX relpaths under the output root. Implemented
    manually because posixpath.relpath() is cwd-dependent for relative inputs.
    """
    a = from_rel.split("/")[:-1] if from_rel else []   # directory of containing file
    b = to_rel.split("/")
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    ups = len(a) - i
    rest = b[i:]
    if not rest:
        return posixpath.basename(to_rel) or "."
    parts = [".."] * ups + list(rest)
    if from_rel.strip("/") == to_rel.strip("/"):
        return posixpath.basename(to_rel)
    segs = [quote(p, safe="@-._~") if p not in ("..", ".") else p for p in parts]
    return "/".join(segs) if segs else "."


SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")
SRCSET_ITEM_RE = re.compile(r"^(\S+)(?:\s+(.*))?$")


def parse_srcset(value: str) -> list[tuple[str, str]]:
    """Parse a srcset attribute into (url, descriptor) pairs."""
    items: list[tuple[str, str]] = []
    for chunk in SRCSET_SPLIT_RE.split(value.strip()):
        if not chunk:
            continue
        m = SRCSET_ITEM_RE.match(chunk)
        if m:
            items.append((m.group(1), (m.group(2) or "").strip()))
    return items


def build_srcset(items: list[tuple[str, str]]) -> str:
    return ", ".join(u if not d else f"{u} {d}" for u, d in items)


NON_FETCHABLE_PREFIXES = (
    "data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "sms:",
    "ftp:", "magnet:", "#", "ws:", "wss:", "file:", "chrome:", "intent:",
)
