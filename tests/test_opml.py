import yaml

from content_curator.opml import merge_into_sources

OPML = """<?xml version="1.0"?>
<opml version="1.0"><head><title>subs</title></head><body>
  <outline text="Kubernetes" title="Kubernetes">
    <outline type="rss" text="Kubernetes blog" xmlUrl="https://kubernetes.io/feed.xml"/>
    <outline type="rss" text="New Blog" xmlUrl="https://new.test/feed"/>
  </outline>
  <outline type="rss" text="New Blog" xmlUrl="https://other.test/feed"/>
</body></opml>"""


def test_merge_skips_known_and_dedupes_ids(tmp_path):
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": [
        {"id": "kubernetes", "name": "Kubernetes blog", "url": "http://www.kubernetes.io/feed.xml/"}
    ]}))
    opml = tmp_path / "subs.opml"
    opml.write_text(OPML)

    assert merge_into_sources(opml, sources) == (2, 1)
    data = yaml.safe_load(sources.read_text())["sources"]
    assert [s["id"] for s in data] == ["kubernetes", "new-blog", "new-blog-2"]
    assert data[1]["topics"] == ["kubernetes"] and data[1]["discovery"] == "opml"
    assert "topics" not in data[2]

    assert merge_into_sources(opml, sources) == (0, 3)
