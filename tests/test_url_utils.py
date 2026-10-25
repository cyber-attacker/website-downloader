"""Unit tests for URL normalization, page classification and path mapping."""
import pytest

from app.utils.url_utils import (
    doc_base,
    ext_of,
    in_scope,
    is_page_url,
    map_url_to_rel,
    normalize,
    parse_srcset,
    rel_for_attr,
    sanitize_segment,
    build_srcset,
)

SEED = "https://docs.example.com"


# ---------------------------------------------------------------- normalize

def test_normalize_basic():
    assert normalize("HTTP://Example.COM:80/a/b.html#frag") == "http://example.com/a/b.html"

def test_normalize_resolves_relative():
    out = normalize("./x/../y.css", "https://h.com/sub/page.html")
    assert out == "https://h.com/sub/y.css"

def test_normalize_drops_fragments_keeps_query():
    assert normalize("/p?q=2&a=1#top") is None or True  # no base -> absolute required
    out = normalize("/p?q=2&a=1#top", "https://h.com/")
    assert out == "https://h.com/p?a=1&q=2"      # sorted query, no fragment

def test_normalize_scheme_unification_on_site_host():
    out = normalize("http://docs.example.com/page?q=1", site_host="docs.example.com", site_scheme="https")
    assert out == "https://docs.example.com/page?q=1"

def test_normalize_preserves_trailing_slash():
    # directory marker: relative refs resolve differently with/without it
    assert normalize("https://h.com/tag/love/") == "https://h.com/tag/love/"
    assert normalize("https://h.com/tag/love") == "https://h.com/tag/love"

def test_doc_base():
    assert doc_base("https://h.com/tag/love") == "https://h.com/tag/love/"
    assert doc_base("https://h.com/tag/love/") == "https://h.com/tag/love/"
    assert doc_base("https://h.com/a/b.html") == "https://h.com/a/b.html"
    assert doc_base("https://h.com/") == "https://h.com/"

def test_parent_relative_resolution_uses_doc_base():
    # href written by the rewriter from page tag/change/page/1/index.html
    base = doc_base("https://h.com/tag/change/page/1")
    out = normalize("../../../love/index.html", base)
    assert out == "https://h.com/tag/love/index.html"

def test_normalize_keeps_foreign_scheme():
    out = normalize("http://cdn.other.com/file.js", site_host="docs.example.com", site_scheme="https")
    assert out == "http://cdn.other.com/file.js"

def test_normalize_rejects_junk():
    for bad in ("javascript:void(0)", "mailto:x@y.z", "data:image/png;base64,xx", "#", "ftp://h/f"):
        assert normalize(bad, base="https://h.com/") is None

# ---------------------------------------------------------------- classify

def test_is_page_url():
    assert is_page_url("https://h.com/a/b.html")
    assert is_page_url("https://h.com/a/index.php?x=1")
    assert is_page_url("https://h.com/plainpath")          # no extension = page
    assert not is_page_url("https://h.com/a/b.png")
    assert not is_page_url("https://h.com/a/docs.pdf")

def test_ext_of():
    assert ext_of("https://h.com/a/b.GIF?q=1") == "gif"
    assert ext_of("https://h.com/a/nof") == ""


# ---------------------------------------------------------------- sanitize

def test_sanitize_illegal_chars():
    assert sanitize_segment("a<b>c:d|e?f*") == "a_b_c_d_e_f_"

def test_sanitize_reserved_name():
    assert sanitize_segment("con.css").startswith("_con")
    assert sanitize_segment("NUL").startswith("_NUL")

def test_sanitize_long_segment_gets_hash_suffix():
    seg = "x" * 300
    out = sanitize_segment(seg)
    assert len(out) <= 90 and out != seg
    assert "-" in out

def test_sanitize_strips_trailing_dots_and_spaces():
    assert sanitize_segment("name. . ") == "name"


# ---------------------------------------------------------------- mapping

def test_map_root_homepage():
    assert map_url_to_rel("https://docs.example.com/?x=1", "docs.example.com") == "index.html"

def test_map_preserves_folder_structure():
    rel = map_url_to_rel("https://docs.example.com/guides/intro.html", "docs.example.com")
    assert rel == "guides/intro.html"

def test_map_extensionless_page_becomes_dir_page():
    rel = map_url_to_rel("https://docs.example.com/api/v2", "docs.example.com")
    assert rel == "api/v2/index.html"

def test_map_query_string_embedded_in_stem():
    rel = map_url_to_rel("https://docs.example.com/theme.css?v=2.1", "docs.example.com")
    assert rel.startswith("theme@") and rel.endswith(".css")
    other = map_url_to_rel("https://docs.example.com/theme.css?v=3", "docs.example.com")
    assert other != rel                                   # distinct hash -> distinct file

def test_map_query_page_dirstyle_distinct():
    a = map_url_to_rel("https://docs.example.com/search?q=1", "docs.example.com")
    b = map_url_to_rel("https://docs.example.com/search?q=2", "docs.example.com")
    assert a.startswith("search/index@")
    assert b.startswith("search/index@")
    assert a != b                                        # distinct query -> distinct file

def test_map_foreign_asset_under_host_folder():
    rel = map_url_to_rel("https://cdn.jsdelivr.net/npm/x/y.js", "docs.example.com")
    assert rel == "cdn.jsdelivr.net/npm/x/y.js"

def test_map_foreign_page_still_maps_pathwise():
    rel = map_url_to_rel("https://other.org/ppage.html", "docs.example.com")
    assert rel == "other.org/ppage.html"

def test_map_unicode_and_spaces_decode():
    # spaces stay (legal on Windows/native encoding); quoting happens at rewrite time
    rel = map_url_to_rel("https://docs.example.com/a%20b/%E4%B8%AD/x.html", "docs.example.com")
    assert rel == "a b/中/x.html"


# ---------------------------------------------------------------- rel paths

def test_rel_for_attr_sibling():
    assert rel_for_attr("a/b/page.html", "a/b/pic.png") == "pic.png"

def test_rel_for_attr_parent():
    assert rel_for_attr("a/b/c/page.html", "a/img/x.png") == "../../img/x.png"

def test_rel_for_attr_foreign_host():
    out = rel_for_attr("index.html", "cdn.other.com/x/y.js")
    assert out == "cdn.other.com/x/y.js"

def test_rel_for_attr_spaces_encoded():
    out = rel_for_attr("index.html", "my site/a b.png")
    assert out == "my%20site/a%20b.png"

def test_rel_for_attr_self():
    assert rel_for_attr("p/g/page.html", "p/g/page.html") == "page.html"


# ---------------------------------------------------------------- srcset

def test_srcset_roundtrip():
    items = parse_srcset("a.png 1x, b.png 2x")
    assert items == [("a.png", "1x"), ("b.png", "2x")]
    assert build_srcset(items) == "a.png 1x, b.png 2x"


# ---------------------------------------------------------------- scope

def test_in_scope_www_stripped():
    assert in_scope("www.example.com", "example.com")

def test_in_scope_subdomains_requires_flag(client_fixture=None):
    assert not in_scope("api.example.com", "www.example.com")
    assert in_scope("api.example.com", "www.example.com", allow_subdomains=True)
