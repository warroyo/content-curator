import pytest

from content_curator.normalize import content_hash, normalize_url


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://Example.com/post/", "https://example.com/post"),
        ("http://www.example.com/post", "https://example.com/post"),
        ("https://example.com:443/post#comments", "https://example.com/post"),
        ("https://example.com/post?utm_source=rss&utm_medium=feed", "https://example.com/post"),
        ("https://example.com/post?b=2&a=1&fbclid=x", "https://example.com/post?a=1&b=2"),
        ("https://example.com/blog/index.html", "https://example.com/blog"),
        ("https://example.com//a//b/", "https://example.com/a/b"),
    ],
)
def test_equivalent_urls_normalize_the_same(a, b):
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://example.com/post?id=1", "https://example.com/post?id=2"),
        ("https://example.com:8443/post", "https://example.com/post"),
        ("https://blog.example.com/post", "https://example.com/post"),
    ],
)
def test_different_urls_stay_different(a, b):
    assert normalize_url(a) != normalize_url(b)


def test_root_keeps_slash():
    assert normalize_url("https://example.com") == "https://example.com/"


def test_content_hash_ignores_case_and_whitespace():
    text = " ".join(f"word{i}" for i in range(150))
    assert content_hash(text) == content_hash("  " + text.upper().replace(" ", "\n  "))


def test_content_hash_skips_short_text():
    assert content_hash("too short to be distinctive") is None
    assert content_hash(None) is None
