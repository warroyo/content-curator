"""Apple Podcasts through the iTunes Search API: free, no key, about 20 requests a minute.

Used for two things: an episode link that opens in Apple Podcasts (matched by the RSS GUID,
which Apple returns as episodeGuid), and finding new shows by topic.
"""

from __future__ import annotations

import logging
import time

import httpx

from content_curator.normalize import normalize_url

log = logging.getLogger(__name__)

API = "https://itunes.apple.com"
TECHNOLOGY_GENRE = "1318"
# Stay well under the ~20 requests/minute limit.
MIN_INTERVAL_S = 3.5


class Apple:
    def __init__(self, client: httpx.Client, base: str = API, min_interval: float = MIN_INTERVAL_S):
        self.client = client
        self.base = base
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, path: str, params: dict) -> list[dict]:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.client.get(f"{self.base}/{path}", params=params)
        if resp.status_code in (403, 429):
            log.warning("apple rate limited", extra={"status": resp.status_code})
            return []
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_show(self, name: str, feed_url: str) -> int | None:
        """Apple's id for a show, by searching its name and matching the feed URL."""
        want = normalize_url(feed_url)
        for r in self._get("search", {"term": name, "entity": "podcast", "limit": 25}):
            if r.get("feedUrl") and normalize_url(r["feedUrl"]) == want:
                return int(r["collectionId"])
        return None

    def episode_links(self, show_id: int) -> dict[str, str]:
        """Apple Podcasts URL per episode GUID, for the show's most recent episodes (up to 200)."""
        out = {}
        for r in self._get("lookup", {"id": show_id, "entity": "podcastEpisode", "limit": 200}):
            if r.get("wrapperType") == "podcastEpisode" and r.get("episodeGuid") and r.get("trackViewUrl"):
                out[r["episodeGuid"]] = r["trackViewUrl"].replace("&uo=4", "").replace("?uo=4", "")
        return out

    def search_shows(self, term: str, limit: int = 15) -> list[dict]:
        """Technology shows matching a topic, in Apple's relevance order."""
        return [
            r for r in self._get("search", {"term": term, "entity": "podcast", "limit": limit})
            if r.get("feedUrl") and TECHNOLOGY_GENRE in [str(g) for g in r.get("genreIds", [])]
        ]

    def top_chart_ids(self, genre: str = TECHNOLOGY_GENRE, limit: int = 100) -> list[int]:
        """Show ids on Apple's US top chart for a genre (the legacy RSS chart, no key)."""
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.client.get(f"{self.base}/us/rss/toppodcasts/limit={limit}/genre={genre}/json")
        if resp.status_code != 200:
            return []
        entries = resp.json().get("feed", {}).get("entry", [])
        return [int(e["id"]["attributes"]["im:id"]) for e in entries if e.get("id", {}).get("attributes")]

    def lookup_shows(self, ids: list[int]) -> list[dict]:
        """Full show records (feedUrl, trackCount, releaseDate, genres) for up to 200 ids per call."""
        out = []
        for i in range(0, len(ids), 150):
            chunk = ",".join(str(x) for x in ids[i : i + 150])
            out += [r for r in self._get("lookup", {"id": chunk}) if r.get("feedUrl")]
        return out

    def recent_episodes(self, show_id: int, limit: int = 8) -> list[dict]:
        return [
            r for r in self._get("lookup", {"id": show_id, "entity": "podcastEpisode", "limit": limit})
            if r.get("wrapperType") == "podcastEpisode"
        ]
