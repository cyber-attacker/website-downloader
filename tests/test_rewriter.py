"""Rewriter tests against fixture HTML/CSS + a deterministic resolver."""
import os

import pytest

from app.core.rewriter import (
    absolutize,
    extract_css,
    extract_html,
    extract_style_urls,
    rewrite_css,
    rewrite_html,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PAGE = "http://site.test/docs/index.html"


def read(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def resolver():
    """Deterministic mapping: absolute URL -> output-rel path or None (leave)."""
    mapping = {
        "http://site.test/docs/css/style.css": "docs/css/style.css",
        "http://site.test/docs/page2.html": "docs/page2.html",
        "http://site.test/docs/sub/deeper page.html": "docs/sub/deeper page.html",
        "http://site.test/docs/img/hero.png": "docs/img/hero.png",
        "http://site.test/docs/img/hero@2x.png": "docs/img/hero@2x.png",
        "http://site.test/docs/img/lazy.png": "docs/img/lazy.png",
        "http://site.test/docs/js/app.js": "docs/js/app.js",
        "http://site.test/docs/favicon.ico": "docs/favicon.ico",
        "http://site.test/docs/img/bg.png": "docs/img/bg.png",
        "http://site.test/docs/img/inline-styled.png": "docs/img/inline-styled.png",
        "http://site.test/docs/img/style-block.png": "docs/img/style-block.png",
        "http://site.test/docs/more.css": "docs/more.css",
        "http://site.test/docs/doc.pdf": "docs/doc.pdf",
        # the mapping collapses the sanitize-stripped space segment the way a
        # real crawl would (rel_paths are produced by map_url_to_rel)
        "http://site.test/docs/sub/ deeper page.html": "docs/sub/deeper page.html",
    }

    def resolver(abs_url: str, kind: str) -> str | None:
        if abs_url.startswith("http://site.test/deep/"):
            return None                      # out of scope
        if abs_url.rstrip("/") == "http://example.com":
            return None                      # foreign page
        if abs_url == "http://example.com/external":
            return None
        return mapping.get(abs_url)          # unknown -> None (leave untouched)

    return resolver


# -------------------------------------------------------------- extraction

def test_extract_finds_links_and_assets():
    html = read("page_relative.html")
    out = extract_html(html)
    hrefs = {r.url for r in out.links}
    assert {"page2.html", "https://example.com/external", "doc.pdf"} <= hrefs
    LINK = "sub/ deeper page.html"
    assert LINK in hrefs
    assets = {r.url for r in out.assets}
    # <link> (stylesheet/icon) are assets too; imgs (incl. srcset + lazy) are assets
    assert {"css/style.css", "favicon.ico", "img/hero.png", "img/hero@2x.png",
            "img/lazy.png", "js/app.js", "img/bg.png"} <= assets


def test_extract_ignores_data_and_js_urls():
    html = '<a href="javascript:void(0)">x</a><img src="data:image/gif;base64,R0=">'
    out = extract_html(html)
    assert out.links == []
    assert out.assets == []


def test_extract_base_href():
    html = '<base href="/prefix/"><p><a href="x.html">rel</a></p>'
    out = extract_html(html)
    assert out.base_href == "/prefix/"


def test_extract_css_urls():
    css = 'body{background:url("bg.png")} @import "theme.css"; @import url(other.css);'
    assert set(extract_css(css)) == {"bg.png", "theme.css", "other.css"}


def test_extract_style_urls_inline():
    html = '<style>p{background:url(a.png)}</style><div style="background:url(b.png)"></div>'
    assert set(extract_style_urls(html)) == {"a.png", "b.png"}


def test_extract_broken_html_does_not_crash():
    html = read("page_broken.html")
    out = extract_html(html)                      # malformed <body / unclosed head
    # robustness goal: no exception; whatever is salvageable gets returned
    assert out is not None and hasattr(out, "links")


# -------------------------------------------------------------- rewriting

def test_rewrite_relative_links(resolver):
    out = rewrite_html(read("page_relative.html"), PAGE, "docs/index.html", resolver)
    assert 'href="css/style.css"' in out
    assert 'href="page2.html"' in out
    assert 'href="sub/deeper%20page.html"' in out          # space percent-encoded
    assert 'href="https://example.com/external"' in out    # foreign page stays absolute
    assert 'href="doc.pdf"' in out                         # now a relative sibling


def test_rewrite_assets_and_srcset(resolver):
    out = rewrite_html(read("page_relative.html"), PAGE, "docs/index.html", resolver)
    import re as _re
    assert 'src="img/hero.png"' in out
    m = _re.search(r'srcset="([^"]+)"', out)
    assert m, out
    parts = m.group(1)
    assert "img/hero.png 1x" in parts and "img/hero@2x.png 2x" in parts
    assert 'data-src="img/lazy.png"' in out
    assert 'src="js/app.js"' in out
    assert 'background="img/bg.png"' in out


def test_rewrite_doctype_and_base_removed(resolver):
    out = rewrite_html(read("page_relative.html"), PAGE, "docs/index.html", resolver)
    assert out.lower().startswith("<!doctype")
    assert "<base" not in out


def test_rewrite_inline_css(resolver):
    out = rewrite_html(read("page_relative.html"), PAGE, "docs/index.html", resolver)
    assert "url(img/style-block.png)" in out        # <style> block
    assert "img/inline-styled.png" in out           # style attribute (quotes preserved)
    assert "url(more.css)" in out                   # @import target mapped (sibling)


def test_rewrite_out_of_scope_left_absolute(resolver):
    html = '<a href="/deep/beyond.html">x</a>'
    out = rewrite_html(html, PAGE, "docs/index.html", resolver)
    # relative attr for an out-of-scope target is converted to its absolute URL
    assert 'href="http://site.test/deep/beyond.html"' in out


def test_rewrite_css_file(resolver):
    css = 'body{background:url(../img/logo.png)}\n@import "parts/a.css";'
    mapping = {"http://site.test/css/parts/a.css": "docs/css/parts/a.css"}

    def res(absu, kind):
        if absu in mapping:
            return mapping[absu]
        return None                          # unmapped stays as-is

    out = rewrite_css(css, "http://site.test/css/root.css", res, "docs/css/root.css")
    assert "url(parts/a.css)" in out                        # mapped, relative to css dir
    # unmapped + relative -> made absolute so it does not 404 against the mirror
    assert "url(http://site.test/img/logo.png)" in out


def test_rewrite_css_import_quote_preserved():
    css = '@import "x.css";'
    called = []

    def res(absu, kind):
        called.append(absu)
        return "d/x.css"

    out = rewrite_css(css, "http://s/a/page.html", res, "d/page.html")
    assert called == ["http://s/a/x.css"]     # resolved against /a/
    assert 'url(x.css)' in out


# ------------------------------------------------------------- absolutize

def test_absolutize_basics():
    assert absolutize("x.png", "http://s/a/b.html") == "http://s/a/x.png"
    assert absolutize("http://o/x.js", "http://s/") == "http://o/x.js"
    assert absolutize("x.png", "") is None
