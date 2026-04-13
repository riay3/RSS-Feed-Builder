"""
RSS Feed Builder
Turn any webpage into a full-text RSS feed for Feedly and other readers.
Handles: YouTube search/channels, Substack, news author pages, and generic sites.
"""

import os
import re
import json
import time
import logging
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode, parse_qs, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
import trafilatura
import feedparser
from flask import Flask, request, Response, render_template, jsonify

# Optional yt-dlp for robust YouTube support
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False
    logging.warning("yt-dlp not installed. YouTube scraping will use fallback method.")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_ITEMS = 15          # Max feed entries
FULL_TEXT_MAX = 3       # Max articles to fetch full text for (keep low to avoid Feedly timeouts)
REQUEST_TIMEOUT = 15    # Per-request timeout in seconds
CACHE_TTL = 1800        # Cache for 30 minutes


# ─── In-memory cache ────────────────────────────────────────────────────────

_cache: dict = {}


def cache_get(key: str):
    entry = _cache.get(key)
    if entry:
        data, ts = entry
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def cache_set(key: str, data):
    _cache[key] = (data, time.time())
    if len(_cache) > 300:
        cutoff = time.time() - CACHE_TTL
        expired = [k for k, (_, ts) in _cache.items() if ts < cutoff]
        for k in expired:
            _cache.pop(k, None)


# ─── HTTP helper ────────────────────────────────────────────────────────────

def fetch_html(url: str, timeout: int = REQUEST_TIMEOUT) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"fetch_html failed [{url}]: {e}")
        return None


# ─── URL type detection ──────────────────────────────────────────────────────

def detect_url_type(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    query = parsed.query.lower()

    if "youtube.com" in domain or "youtu.be" in domain:
        if "/results" in path or "search_query" in query:
            return "youtube_search"
        if "/playlist" in path:
            return "youtube_playlist"
        return "youtube_channel"

    if "substack.com" in domain:
        return "substack"

    # Already an RSS/Atom feed
    if any(url.rstrip("/").endswith(x) for x in [".xml", ".rss", "/feed", "/rss"]):
        return "direct_rss"

    return "generic"


# ─── Full-text extraction ────────────────────────────────────────────────────

def _deep_find_content(obj, depth: int = 0) -> str:
    """Recursively search nested JSON for a substantial text/html field."""
    if depth > 6:
        return ""
    if isinstance(obj, dict):
        for key in ["articleBody", "body", "content", "html", "text", "description"]:
            val = obj.get(key, "")
            if isinstance(val, str) and len(val) > 500:
                return val
        for val in obj.values():
            result = _deep_find_content(val, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj[:10]:
            result = _deep_find_content(item, depth + 1)
            if result:
                return result
    return ""


def extract_jsonld_content(html: str) -> str:
    """Extract article body from JSON-LD structured data embedded in the page.
    Many news sites (Vox Media, Atlantic, WaPo, etc.) embed the full article
    text here even when the rendered page is paywalled or JS-only."""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or ""
            if not raw.strip():
                continue
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                body = item.get("articleBody", "")
                if body and len(body) > 300:
                    # Convert plain text paragraphs to HTML
                    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
                    if paras:
                        return "\n".join(f"<p>{p}</p>" for p in paras)
                # Also check nested @graph arrays (used by some sites)
                for graph_item in item.get("@graph", []):
                    body = graph_item.get("articleBody", "")
                    if body and len(body) > 300:
                        paras = [p.strip() for p in body.split("\n\n") if p.strip()]
                        if paras:
                            return "\n".join(f"<p>{p}</p>" for p in paras)
        except Exception:
            continue
    return ""


def extract_nextjs_content(html: str) -> str:
    """Extract article content from Next.js __NEXT_DATA__ JSON.
    Next.js sites (NY Mag, The Atlantic, many modern news sites) embed the full
    page data as JSON — including article body — in a <script> tag."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html, re.DOTALL
    )
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
        props = data.get("props", {}).get("pageProps", {})
        content = _deep_find_content(props)
        if content and len(content) > 300:
            # If it's plain text, wrap in paragraphs
            if not content.strip().startswith("<"):
                paras = [p.strip() for p in content.split("\n\n") if p.strip()]
                content = "\n".join(f"<p>{p}</p>" for p in paras)
            return content
    except Exception as e:
        logger.debug(f"Next.js extraction failed: {e}")
    return ""


def get_full_text(url: str) -> str:
    """Extract the full article body as HTML.
    Tries four strategies in order of reliability:
    1. JSON-LD structured data (works for many Vox/Atlantic/WaPo articles)
    2. Next.js __NEXT_DATA__ JSON (NY Mag, Atlantic, many modern news sites)
    3. trafilatura (best general-purpose extractor)
    4. BeautifulSoup naive fallback
    """
    html = fetch_html(url)
    if not html:
        return ""

    # 1. JSON-LD — most reliable for news sites that support it
    jsonld = extract_jsonld_content(html)
    if jsonld:
        logger.info(f"JSON-LD extraction succeeded [{url}]")
        return jsonld

    # 2. Next.js page data — catches JS-rendered news sites
    nextjs = extract_nextjs_content(html)
    if nextjs:
        logger.info(f"Next.js extraction succeeded [{url}]")
        return nextjs

    # 3. trafilatura — best-in-class general extractor
    try:
        result = trafilatura.extract(
            html,
            url=url,
            include_images=True,
            include_links=True,
            include_tables=True,
            output_format="html",
            favor_recall=True,
            no_fallback=False,
        )
        if result and len(result) > 200:
            return result
    except Exception as e:
        logger.warning(f"trafilatura failed [{url}]: {e}")

    # 4. BeautifulSoup fallback
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["nav", "header", "footer", "aside", "script", "style", "noscript"]):
            tag.decompose()
        article = (
            soup.find("article")
            or soup.find(class_=re.compile(r"article|content|post|story|body", re.I))
            or soup.find("main")
        )
        if article:
            return str(article)
    except Exception as e:
        logger.warning(f"BS4 fallback failed [{url}]: {e}")

    return ""


def get_full_texts_parallel(urls: list[str], max_workers: int = 5) -> dict[str, str]:
    """Fetch full text for multiple URLs concurrently."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(get_full_text, u): u for u in urls}
        try:
            for future in as_completed(future_map, timeout=60):
                url = future_map[future]
                try:
                    results[url] = future.result(timeout=REQUEST_TIMEOUT)
                except Exception:
                    results[url] = ""
        except Exception:
            pass
    return results


# ─── Link scoring & extraction ───────────────────────────────────────────────

DATE_IN_URL = re.compile(r"/\d{4}/\d{2}/")
ARTICLE_PATH = re.compile(
    r"/(article|story|post|news|features?|columns?|opinions?|review|analysis)s?/", re.I
)
SKIP_PATH = re.compile(
    r"/(tag|tags|category|categories|author|authors|search|page|feed|rss|login|"
    r"subscribe|contact|about|privacy|terms|newsletter|sitemap|podcast|photo|"
    r"gallery|video|videos|topics?|section|sections?)/?$",
    re.I,
)
MIN_TITLE_LEN = 15


def _score_link(href: str, text: str) -> int:
    score = 0
    if DATE_IN_URL.search(href):
        score += 4
    if ARTICLE_PATH.search(href):
        score += 3
    if len(text) > 50:
        score += 2
    elif len(text) > MIN_TITLE_LEN:
        score += 1
    parts = urlparse(href).path.strip("/").split("/")
    score += min(len(parts), 3)
    return score


def extract_links_from_page(html: str, base_url: str) -> list[dict]:
    """Extract likely article links from an author/listing page."""
    soup = BeautifulSoup(html, "lxml")
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc
    seen: set[str] = set()
    candidates: list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        text = a.get_text(" ", strip=True)

        if not href or not text or len(text) < MIN_TITLE_LEN:
            continue

        # Resolve relative URLs
        if href.startswith("//"):
            href = parsed_base.scheme + ":" + href
        elif href.startswith("/"):
            href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        elif not href.startswith("http"):
            continue

        link_domain = urlparse(href).netloc
        if link_domain != base_domain and not link_domain.endswith("." + base_domain):
            continue

        link_path = urlparse(href).path
        if SKIP_PATH.search(link_path):
            continue

        clean = f"{urlparse(href).netloc}{urlparse(href).path.rstrip('/')}"
        if clean in seen:
            continue
        seen.add(clean)

        candidates.append({
            "url": href.split("#")[0],
            "title": text[:200],
            "score": _score_link(href, text),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:MAX_ITEMS]


# ─── Detect existing RSS link in page <head> ─────────────────────────────────

def find_rss_in_page(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link"):
        if re.search(r"(rss|atom|feed)", link.get("type", ""), re.I):
            href = link.get("href", "")
            if not href:
                continue
            if href.startswith("http"):
                return href
            if href.startswith("/"):
                p = urlparse(base_url)
                return f"{p.scheme}://{p.netloc}{href}"
    return None


# ─── RSS / Atom feed parser (for existing feeds) ─────────────────────────────

def parse_rss_feed(feed_url: str, enhance_full_text: bool = True, fetch_limit: int = MAX_ITEMS) -> list[dict]:
    """Fetch and parse an existing RSS/Atom feed, optionally enhancing with full text."""
    try:
        feed = feedparser.parse(feed_url, request_headers=HEADERS)
        if not feed.entries:
            return []

        items: list[dict] = []
        needs_full_text: list[str] = []

        for entry in feed.entries[:fetch_limit]:
            content = ""
            if hasattr(entry, "content") and entry.content:
                content = entry.content[0].get("value", "")
            if not content and hasattr(entry, "summary"):
                content = entry.summary or ""

            date = datetime.now(timezone.utc)
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

            url = entry.get("link", "")
            # Capture author — feedparser exposes dc:creator as entry.author
            author = ""
            if hasattr(entry, "author"):
                author = entry.author or ""
            elif hasattr(entry, "authors") and entry.authors:
                author = entry.authors[0].get("name", "")
            item = {
                "title": entry.get("title", "Untitled"),
                "url": url,
                "description": entry.get("summary", ""),
                "date": date,
                "full_text": content,
                "author": author,
                "type": "article",
            }
            items.append(item)

            if enhance_full_text and len(content) < 600 and url:
                needs_full_text.append(url)

        if needs_full_text:
            full_texts = get_full_texts_parallel(needs_full_text[:FULL_TEXT_MAX])
            for item in items:
                if item["url"] in full_texts and full_texts[item["url"]]:
                    item["full_text"] = full_texts[item["url"]]

        return items
    except Exception as e:
        logger.error(f"parse_rss_feed error [{feed_url}]: {e}")
        return []


# ─── YouTube ─────────────────────────────────────────────────────────────────

def _yt_date(date_str: str) -> datetime:
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def get_yt_items_ytdlp(yt_url: str, max_items: int = MAX_ITEMS) -> list[dict]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{max_items}",
        "socket_timeout": 15,
    }
    items: list[dict] = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(yt_url, download=False)
            if not info:
                return []
            entries = info.get("entries", [info])
            for entry in (entries or [])[:max_items]:
                if not entry:
                    continue
                vid_id = entry.get("id", "")
                vid_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
                items.append({
                    "title": entry.get("title", "Untitled"),
                    "url": vid_url,
                    "description": entry.get("description") or "",
                    "date": _yt_date(entry.get("upload_date", "")),
                    "thumbnail": entry.get("thumbnail", ""),
                    "video_id": vid_id,
                    "channel": entry.get("uploader") or entry.get("channel") or "",
                    "type": "youtube",
                })
    except Exception as e:
        logger.error(f"yt-dlp error [{yt_url}]: {e}")
    return items


def get_yt_items_scrape(search_query: str) -> list[dict]:
    """Fallback YouTube search scraper (no yt-dlp required)."""
    url = f"https://www.youtube.com/results?search_query={quote(search_query)}"
    html = fetch_html(url)
    if not html:
        return []

    m = re.search(r"var ytInitialData\s*=\s*({.+?});\s*</script>", html, re.DOTALL)
    if not m:
        return []

    items: list[dict] = []
    try:
        data = json.loads(m.group(1))
        sections = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )
        for section in sections:
            for item_data in section.get("itemSectionRenderer", {}).get("contents", []):
                r = item_data.get("videoRenderer", {})
                if not r:
                    continue
                vid_id = r.get("videoId", "")
                if not vid_id:
                    continue
                title = "".join(x.get("text", "") for x in r.get("title", {}).get("runs", []))
                snip = (r.get("detailedMetadataSnippets") or [{}])[0]
                desc = "".join(
                    x.get("text", "")
                    for x in snip.get("snippetText", {}).get("runs", [])
                )
                thumb = (r.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url", "")
                channel = (r.get("ownerText", {}).get("runs") or [{}])[0].get("text", "")
                items.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "description": desc,
                    "date": datetime.now(timezone.utc),
                    "thumbnail": thumb,
                    "video_id": vid_id,
                    "channel": channel,
                    "type": "youtube",
                })
                if len(items) >= MAX_ITEMS:
                    break
            if len(items) >= MAX_ITEMS:
                break
    except Exception as e:
        logger.error(f"YouTube scrape error: {e}")
    return items


def yt_search_url_by_date(query: str) -> str:
    """Return a YouTube search URL sorted by upload date.
    &sp=CAI%3D is YouTube's encoded filter for 'Sort by: Upload date'."""
    return f"https://www.youtube.com/results?search_query={quote(query)}&sp=CAI%3D"


def sort_by_date(items: list[dict]) -> list[dict]:
    """Sort feed items newest-first, treating missing dates as oldest."""
    def _key(item):
        d = item.get("date")
        if not d:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(d, str):
            try:
                d = datetime.fromisoformat(d)
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    return sorted(items, key=_key, reverse=True)


def get_yt_channel_items(url: str) -> list[dict]:
    """Get YouTube channel videos — tries yt-dlp first, then official RSS."""
    if HAS_YTDLP:
        return get_yt_items_ytdlp(url)

    # Fallback: find channel ID and use YouTube's official RSS
    html = fetch_html(url) or ""
    for pattern in [
        r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]+)"',
        r"/channel/(UC[a-zA-Z0-9_-]+)",
        r"channel_id=(UC[a-zA-Z0-9_-]+)",
    ]:
        match = re.search(pattern, html)
        if match:
            channel_id = match.group(1)
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            items = parse_rss_feed(rss_url, enhance_full_text=False)
            if items:
                for item in items:
                    # Attach video_id for embed
                    m = re.search(r"v=([a-zA-Z0-9_-]{11})", item.get("url", ""))
                    if m:
                        item["video_id"] = m.group(1)
                    item["type"] = "youtube"
                return items
    return []


def format_youtube_html(item: dict) -> str:
    """Render a YouTube item as embeddable HTML for the feed content."""
    vid_id = item.get("video_id", "")
    desc = (item.get("description") or "").replace("\n", "<br>\n")
    thumb = item.get("thumbnail", "")
    channel = item.get("channel", "")

    html = '<div style="font-family: sans-serif; max-width: 800px; line-height: 1.6;">\n'

    if vid_id:
        html += (
            '<div style="position:relative; padding-bottom:56.25%; height:0; '
            'overflow:hidden; max-width:100%; margin-bottom:16px; border-radius:8px;">\n'
            f'  <iframe style="position:absolute; top:0; left:0; width:100%; height:100%; border:0;" '
            f'src="https://www.youtube.com/embed/{vid_id}" '
            'allowfullscreen loading="lazy"></iframe>\n'
            "</div>\n"
        )
    elif thumb:
        html += f'<img src="{thumb}" style="width:100%; max-width:640px; border-radius:8px;" />\n'

    if channel:
        html += f'<p><strong>Channel:</strong> {channel}</p>\n'
    if desc:
        html += f"<p>{desc}</p>\n"

    html += "</div>"
    return html


# ─── Substack ────────────────────────────────────────────────────────────────

def get_substack_items(url: str) -> list[dict]:
    parsed = urlparse(url)
    feed_url = f"{parsed.scheme}://{parsed.netloc}/feed"
    return parse_rss_feed(feed_url, enhance_full_text=True)


# ─── Generic author / listing page ───────────────────────────────────────────

def get_generic_items(url: str) -> list[dict]:
    html = fetch_html(url)
    if not html:
        return []

    parsed = urlparse(url)

    # 1. Author pages: always filter by author, regardless of which RSS source we find.
    #    Must run BEFORE the generic RSS-in-head check, because many sites embed a
    #    site-wide RSS link in <head> that would otherwise return all authors' articles.
    author_match = re.search(r"/author/([^/?#]+)", parsed.path, re.I)
    if author_match:
        author_slug = author_match.group(1)  # e.g. "alex-kane"
        author_name = author_slug.replace("-", " ").lower()

        # 1a. Try a per-author feed URL directly (works on many WordPress sites)
        per_author_feed = url.rstrip("/") + "/feed/"
        test = fetch_html(per_author_feed)
        if test and "<rss" in test:
            logger.info(f"Found per-author feed: {per_author_feed}")
            items = parse_rss_feed(per_author_feed, enhance_full_text=True)
            if items:
                return items

        # 1b. Fall back to site-wide RSS (from <head> or /feed) filtered by author name
        rss_url = find_rss_in_page(html, url)
        if not rss_url or rss_url == url:
            rss_url = f"{parsed.scheme}://{parsed.netloc}/feed"
        logger.info(f"Filtering '{rss_url}' by author '{author_name}'")
        # Fetch up to 50 entries so we don't miss recent articles in a busy site feed
        all_items = parse_rss_feed(rss_url, enhance_full_text=False, fetch_limit=50)
        filtered = [
            item for item in all_items
            if author_name in (item.get("author") or "").lower()
        ][:MAX_ITEMS]  # Cap results after filtering
        if filtered:
            logger.info(f"Author filter found {len(filtered)} items for '{author_name}'")
            urls_needed = [i["url"] for i in filtered if len(i.get("full_text", "")) < 600]
            if urls_needed:
                full_texts = get_full_texts_parallel(urls_needed[:FULL_TEXT_MAX])
                for item in filtered:
                    if item["url"] in full_texts and full_texts[item["url"]]:
                        item["full_text"] = full_texts[item["url"]]
            return filtered
        # RSS had no author metadata — fall through to page scraping below

    # 2. Non-author pages: check for an RSS link in <head>
    if not author_match:
        rss_url = find_rss_in_page(html, url)
        if rss_url and rss_url != url:
            logger.info(f"Found RSS in page: {rss_url}")
            items = parse_rss_feed(rss_url, enhance_full_text=True)
            if items:
                return items

    # 3. Try WordPress-style /feed/ URL for people/writer/contributor pages
    if re.search(r"/people/|/writer|/contributor", parsed.path, re.I):
        wp_feed = url.rstrip("/") + "/feed/"
        test = fetch_html(wp_feed)
        if test and "<rss" in test:
            items = parse_rss_feed(wp_feed, enhance_full_text=True)
            if items:
                return items

    # 4. Scrape article links directly from the page
    links = extract_links_from_page(html, url)
    if not links:
        return []

    urls = [lnk["url"] for lnk in links[:FULL_TEXT_MAX]]
    full_texts = get_full_texts_parallel(urls)

    return [
        {
            "title": lnk["title"],
            "url": lnk["url"],
            "description": "",
            "date": datetime.now(timezone.utc),
            "full_text": full_texts.get(lnk["url"], ""),
            "type": "article",
        }
        for lnk in links
    ]


# ─── Feed title helper ───────────────────────────────────────────────────────

def get_feed_title(url: str, html: str | None = None) -> str:
    if html is None:
        html = fetch_html(url) or ""
    try:
        soup = BeautifulSoup(html, "lxml")
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"][:100]
        title_tag = soup.find("title")
        if title_tag:
            return title_tag.get_text(strip=True)[:100]
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)[:100]
    except Exception:
        pass
    return urlparse(url).netloc


# ─── RSS builder ─────────────────────────────────────────────────────────────

def build_rss(items: list[dict], title: str, source_url: str, self_url: str) -> str:
    fg = FeedGenerator()
    fg.id(source_url)
    fg.title(title or "RSS Feed")
    fg.link(href=source_url)
    fg.link(href=self_url, rel="self")
    fg.description(f"Full-text RSS feed for {source_url}")
    fg.language("en")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for item in items:
        fe = fg.add_entry()
        item_url = item.get("url") or source_url
        fe.id(item_url)
        fe.title(item.get("title") or "Untitled")
        fe.link(href=item_url)

        date = item.get("date") or datetime.now(timezone.utc)
        if isinstance(date, str):
            try:
                date = datetime.fromisoformat(date)
            except Exception:
                date = datetime.now(timezone.utc)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        fe.published(date)

        # Full content — goes into <content:encoded> for Feedly
        if item.get("type") == "youtube":
            content_html = format_youtube_html(item)
        elif item.get("full_text"):
            content_html = item["full_text"]
        else:
            content_html = item.get("description", "")

        if content_html:
            fe.content(content_html, type="html")

        summary = item.get("description", "")
        if not summary and content_html:
            try:
                soup = BeautifulSoup(content_html, "lxml")
                summary = soup.get_text(" ")[:500].strip()
            except Exception:
                summary = ""
        fe.description(summary or item.get("title", ""))

    return fg.rss_str(pretty=True).decode("utf-8")


# ─── Orchestrators ───────────────────────────────────────────────────────────

def generate_feed_for_url(url: str, self_url: str) -> tuple[str, str]:
    """Build a full RSS feed for the given URL. Returns (rss_xml, feed_title)."""
    url_type = detect_url_type(url)
    logger.info(f"generate_feed [{url_type}] {url}")

    if url_type == "youtube_search":
        parsed = urlparse(url)
        query = parse_qs(parsed.query).get("search_query", [""])[0]
        items = (
            sort_by_date(get_yt_items_ytdlp(yt_search_url_by_date(query)))
            if HAS_YTDLP
            else get_yt_items_scrape(query)
        )
        title = f"YouTube: {query}"

    elif url_type == "youtube_channel":
        items = get_yt_channel_items(url)
        channel = next((i.get("channel") for i in items if i.get("channel")), None)
        title = channel or "YouTube Channel"

    elif url_type == "youtube_playlist":
        items = get_yt_items_ytdlp(url) if HAS_YTDLP else []
        title = "YouTube Playlist"

    elif url_type == "substack":
        items = get_substack_items(url)
        parsed = urlparse(url)
        title = parsed.netloc.replace(".substack.com", "").replace("-", " ").title()

    elif url_type == "direct_rss":
        items = parse_rss_feed(url, enhance_full_text=True)
        title = "RSS Feed"

    else:
        items = get_generic_items(url)
        title = get_feed_title(url)

    if not items:
        items = [{
            "title": "No items found",
            "url": url,
            "description": (
                f"Could not find any items at {url}. "
                "The page may use JavaScript to render content, or no article "
                "links were detected. Check if the site has a native RSS feed."
            ),
            "date": datetime.now(timezone.utc),
            "full_text": (
                f"<p>Could not extract items from <a href='{url}'>{url}</a>.</p>"
                "<p>Possible reasons:</p><ul>"
                "<li>The page requires JavaScript to render (React, Next.js, etc.)</li>"
                "<li>The site blocks automated requests</li>"
                "<li>No article links were found on the page</li>"
                "</ul><p>Try providing the site's direct RSS URL if one is available.</p>"
            ),
            "type": "article",
        }]

    rss_xml = build_rss(items, title, url, self_url)
    return rss_xml, title


def generate_preview_for_url(url: str, base_url: str) -> dict:
    """Quick preview (no full-text fetching) for the web UI."""
    url_type = detect_url_type(url)
    preview_items: list[dict] = []
    title = ""

    if url_type == "youtube_search":
        parsed = urlparse(url)
        query = parse_qs(parsed.query).get("search_query", [""])[0]
        items = (
            sort_by_date(get_yt_items_ytdlp(yt_search_url_by_date(query), max_items=5))
            if HAS_YTDLP
            else get_yt_items_scrape(query)[:5]
        )
        title = f"YouTube: {query}"
        for item in items:
            preview_items.append({
                "title": item["title"],
                "url": item["url"],
                "has_full_text": True,
                "type": "youtube",
            })

    elif url_type in ("youtube_channel", "youtube_playlist"):
        items = get_yt_channel_items(url)[:5] if url_type == "youtube_channel" else []
        title = "YouTube Channel"
        for item in items:
            preview_items.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "has_full_text": True,
                "type": "youtube",
            })

    elif url_type == "substack":
        parsed = urlparse(url)
        feed_url = f"{parsed.scheme}://{parsed.netloc}/feed"
        try:
            feed = feedparser.parse(feed_url, request_headers=HEADERS)
            title = feed.feed.get("title", parsed.netloc)
            for entry in feed.entries[:5]:
                content = ""
                if hasattr(entry, "content"):
                    content = entry.content[0].get("value", "")
                preview_items.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "has_full_text": len(content) > 500,
                    "type": "article",
                })
        except Exception:
            pass

    elif url_type == "direct_rss":
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            title = feed.feed.get("title", "RSS Feed")
            for entry in feed.entries[:5]:
                preview_items.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "has_full_text": False,
                    "type": "article",
                })
        except Exception:
            pass

    else:
        html = fetch_html(url) or ""
        rss_url = find_rss_in_page(html, url) if html else None
        title = get_feed_title(url, html)

        if rss_url:
            try:
                feed = feedparser.parse(rss_url, request_headers=HEADERS)
                for entry in feed.entries[:5]:
                    content = ""
                    if hasattr(entry, "content"):
                        content = entry.content[0].get("value", "")
                    preview_items.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "has_full_text": len(content) > 500,
                        "type": "article",
                    })
            except Exception:
                pass

        if not preview_items and html:
            for lnk in extract_links_from_page(html, url)[:5]:
                preview_items.append({
                    "title": lnk["title"],
                    "url": lnk["url"],
                    "has_full_text": False,
                    "type": "article",
                })

    feed_url = base_url.rsplit("/preview", 1)[0] + "/feed?" + urlencode({"url": url})

    return {
        "url_type": url_type,
        "feed_title": title,
        "items_found": len(preview_items),
        "preview_items": preview_items,
        "feed_url": feed_url,
    }


# ─── Flask routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/feed")
def feed_route():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    cache_key = "feed:" + hashlib.md5(url.encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        resp = Response(cached, mimetype="application/rss+xml")
        resp.headers["X-Cache"] = "HIT"
        resp.headers["Cache-Control"] = "public, max-age=1800"
        resp.headers["Last-Modified"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return resp

    try:
        rss, _ = generate_feed_for_url(url, request.url)
        cache_set(cache_key, rss)
        resp = Response(rss, mimetype="application/rss+xml")
        resp.headers["X-Cache"] = "MISS"
        resp.headers["Cache-Control"] = "public, max-age=1800"
        resp.headers["Last-Modified"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return resp
    except Exception as e:
        logger.exception(f"Feed generation error [{url}]")
        return jsonify({"error": str(e)}), 500


@app.route("/preview")
def preview_route():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    cache_key = "preview:" + hashlib.md5(url.encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        result = generate_preview_for_url(url, request.url)
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Preview error [{url}]")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ytdlp": HAS_YTDLP})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
