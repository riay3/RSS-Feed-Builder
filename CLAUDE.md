# RSS Feed Builder — Project Briefing

A Flask web app that turns any URL into a full-text RSS feed compatible with Feedly and other readers. Built for Yair Rosenberg (yrosenberg@theatlantic.com) on Windows. Deployed on Fly.io at **https://rss-feed-builder.fly.dev**.

---

## What It Does

Paste any URL into the web UI → get a feed link you can subscribe to in Feedly. The app detects what kind of URL it is and uses the right strategy to build the feed:

- **YouTube search** (e.g. `youtube.com/results?search_query=...`) → sorted by date via yt-dlp
- **YouTube channel** → via YouTube's native Atom feed
- **Substack** → via the site's `/feed` endpoint
- **Author pages** (e.g. `site.com/author/name`) → WordPress REST API first, then per-author RSS, then site-wide RSS filtered by author name
- **Direct RSS/Atom feeds** → parsed and optionally enhanced with full text
- **Generic pages** → RSS link in `<head>`, then WordPress `/feed/`, then link scraping

Feed URL format: `https://rss-feed-builder.fly.dev/feed?url=ENCODED_URL`

---

## Architecture

Single-file Flask app (`app.py`, ~1080 lines) with a Jinja2 template (`templates/index.html`).

### Key constants (top of app.py)
```python
MAX_ITEMS = 15       # Max feed entries returned
FULL_TEXT_MAX = 3    # Max articles to fetch full text for — kept LOW to avoid Feedly timeouts
REQUEST_TIMEOUT = 15 # Per-request HTTP timeout (content fetching)
PROBE_TIMEOUT = 5    # Short timeout for existence checks (WordPress REST API, per-author feed probe)
CACHE_TTL = 1800     # In-memory cache TTL (30 min)
```

**FULL_TEXT_MAX = 3 is intentional.** It was originally 10. Feedly has a short timeout window; fetching full text for 10 articles in parallel took 45+ seconds and caused Feedly to time out and fall back to showing the app homepage instead of the feed. 3 keeps generation under ~10 seconds.

### Full-text extraction pipeline (in order)
1. JSON-LD `articleBody` extraction (`extract_jsonld_content`)
2. Next.js `__NEXT_DATA__` JSON parsing (`extract_nextjs_content`)
3. `trafilatura`
4. BeautifulSoup fallback

### Author page strategy (in priority order)
1. **WordPress REST API** (`try_wordpress_api`) — queries `/wp-json/wp/v2/users?slug=` to get author ID, then `/wp-json/wp/v2/posts?author=ID`. Most reliable; bypasses RSS feed size limits.
2. **Per-author RSS feed** — tries `url/feed/` directly
3. **Site-wide RSS + author filter** — fetches up to 50 entries from `/feed`, filters by `dc:creator` matching the author slug. `fetch_limit=50` is important: at the default of 15, a busy site would crowd out the target author's recent articles.
4. **Page scraping** — fallback for sites with no RSS at all (works for server-rendered pages; fails silently for JS-rendered ones)

**Critical ordering note:** Author detection must happen *before* the generic "find RSS link in `<head>`" step. Most WordPress sites embed a site-wide RSS link in every page's `<head>`, including author pages. If we check `<head>` first, we'd return all authors' articles unfiltered.

### YouTube
- Search URLs get `&sp=CAI%3D` appended to sort by date before being passed to yt-dlp
- yt-dlp is tried first; falls back to HTML scraping if unavailable
- Results are re-sorted by date after fetching (`sort_by_date`)

### Caching
In-memory dict with 30-minute TTL. Cache key is MD5 of the source URL. Auto-prunes when over 300 entries. Feed responses include `Cache-Control: public, max-age=1800` and `Last-Modified` headers so Feedly can handle caching on its end too.

---

## Deployment

**Platform:** Fly.io free tier (app name: `rss-feed-builder`, region: `iad`)

**Critical fly.toml settings:**
```toml
[http_service]
  auto_stop_machines = 'off'
  min_machines_running = 1
```
`auto_stop_machines = 'off'` is essential. Without it, Fly.io scales the app to zero when idle. When Feedly polls a sleeping app, it waits for boot time + feed generation time, which easily exceeds Feedly's timeout. The app then shows its homepage instead of the feed.

**To deploy after code changes (Windows PowerShell):**
```powershell
cd "C:\Users\ycube\Documents\Claude\Projects\RSS Feed Builder"
git add <files>
git commit -m "description"
git push
fly deploy
```
`git push` alone does NOT deploy — Fly.io requires an explicit `fly deploy`. The user is on Windows and uses PowerShell. GitHub Desktop is also in use but git commands run fine in PowerShell from the project directory.

**Health check:** `https://rss-feed-builder.fly.dev/health` returns `{"status":"ok","ytdlp":true}`

**Preview endpoint:** `https://rss-feed-builder.fly.dev/preview?url=ENCODED_URL` returns JSON — useful for debugging what the app finds before generating a full feed.

---

## Known Issues & Limitations

**Paywalled sites (e.g. NY Mag):** Full text extraction returns only headline and summary. The app cannot bypass paywalls. If the user has a subscription, they can read full text after clicking through from Feedly.

**JS-rendered author pages:** If a site renders its author page entirely in JavaScript, `extract_links_from_page` gets an empty shell and returns nothing. Jewish Currents is an example — it works via the WordPress REST API strategy, not scraping.

**Feedly preview vs. actual feed:** The "Add Sources" preview Feedly shows before you subscribe can be stale/cached and may show incorrect articles. This is a Feedly caching artifact. Add the feed anyway — once Feedly does a proper poll it will display correctly. If a previously-added feed shows wrong articles, remove it and re-add it to force a fresh fetch.

**Feed URL in Feedly:** Always paste the *full* feed URL including `?url=` parameter into Feedly's "Add Content" dialog. Don't search by domain — Feedly will find the app homepage, not the specific feed.

---

## Deployment History

Originally deployed on Railway (free tier, $5 credit). Credit exhausted after a few weeks; migrated to Fly.io. Fly.io required a credit card for account verification but the free tier is genuinely free (1 shared-CPU machine, 256MB RAM). The `fly.toml` was briefly corrupted mid-file during an edit, which caused several issues that looked like bugs in the app logic but were actually deployment failures. Always verify `fly.toml` is complete and valid before deploying.

---

## File Map

```
app.py                  Main Flask application (~1080 lines)
templates/index.html    Web UI (single file, embedded CSS/JS)
requirements.txt        Python dependencies
Dockerfile              Container build (python:3.11-slim, port 8080, gunicorn)
fly.toml                Fly.io configuration
Procfile                For Heroku/Render compatibility (not currently used)
render.yaml             Leftover from Render migration attempt (not used)
DEPLOY.md               Earlier deployment notes (may be outdated)
```
