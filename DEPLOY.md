# Deploying RSS Feed Builder

Your RSS Feed Builder is a Python/Flask app. Once deployed, it runs at a public URL
so Feedly and other readers can fetch your feeds anytime.

---

## Option A — Railway (Recommended, free)

Railway gives you $5 of free credit per month — more than enough for this app.

1. **Create a free account** at [railway.app](https://railway.app)

2. **Push this folder to GitHub**
   - Create a new repo at [github.com/new](https://github.com/new)
   - In this folder, run:
     ```bash
     git init
     git add .
     git commit -m "Initial RSS Feed Builder"
     git remote add origin https://github.com/YOUR_USERNAME/rss-feed-builder.git
     git push -u origin main
     ```

3. **Deploy on Railway**
   - Go to [railway.app/new](https://railway.app/new)
   - Click **Deploy from GitHub repo**
   - Select your `rss-feed-builder` repo
   - Railway auto-detects Python and deploys it

4. **Get your URL**
   - In Railway's dashboard, click your service → **Settings** → **Domains**
   - Click **Generate Domain** — you'll get something like `rss-feed-builder.up.railway.app`

5. **Done!** Open that URL in your browser — you'll see the RSS Feed Builder UI.

---

## Option B — Render (free tier available)

1. Create a free account at [render.com](https://render.com)

2. Push to GitHub (same steps as above)

3. In Render dashboard: **New** → **Web Service** → connect your GitHub repo

4. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --timeout 120 --workers 2`
   - **Python version:** 3.11

5. Click **Deploy** — Render gives you a `*.onrender.com` URL

> **Note:** Render's free tier "spins down" after 15 min of inactivity. The first request after sleep takes ~30s to wake up. Feedly usually retries, so this is generally fine.

---

## Option C — Fly.io (always-on free tier)

Fly.io has a generous always-on free tier (3 small VMs).

1. Install the Fly CLI: `brew install flyctl` (Mac) or see [fly.io/docs/hands-on/install-flyctl](https://fly.io/docs/hands-on/install-flyctl/)

2. In this folder:
   ```bash
   flyctl auth signup   # create account
   flyctl launch        # follow prompts, choose a name and region
   flyctl deploy
   ```

3. Your app will be at `https://YOUR-APP-NAME.fly.dev`

---

## Using Your Feed URL

Once deployed, your feed URLs look like:

```
https://YOUR-APP-URL/feed?url=https%3A%2F%2Fwww.washingtonpost.com%2Fpeople%2Freporter%2F
```

The web UI at `https://YOUR-APP-URL` will generate these automatically — just paste
a URL and click Generate Feed, then copy the result into Feedly.

### Adding to Feedly
1. Open Feedly
2. Click **Add Content** (bottom left)
3. Paste the feed URL
4. Click **Follow**

---

## What It Supports

| URL Type | Example | Notes |
|---|---|---|
| News author page | `washingtonpost.com/people/jane/` | Full article text extracted |
| YouTube search | `youtube.com/results?search_query=AI` | Embedded video player in feed |
| YouTube channel | `youtube.com/@veritasium` | Full video embeds |
| Substack | `authorname.substack.com` | Full text including paid post previews |
| Any page with RSS | Most WordPress blogs | Uses existing feed, enhances with full text |
| Generic webpage | Any listing/archive page | Scrapes article links, extracts text |

### Known Limitations
- **Paywalled articles** — can only extract what's publicly visible without a login
- **Heavy JavaScript sites** — pages that render entirely in JS (React/Next.js apps with no SSR) may only return headlines. This includes some modern news sites.
- **Anti-scraping measures** — sites like NYT actively block automated requests
- **YouTube full descriptions** — yt-dlp's flat extraction gives titles and video IDs; full descriptions require a separate fetch per video

---

## Running Locally (for testing)

```bash
cd "RSS Feed Builder"
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000

---

## Keeping It Updated

The app itself never needs updating — it re-scrapes pages fresh on every feed request
(with 30-minute caching). Your feed URL stays the same forever.

If you want to add features or fix scrapers for specific sites, edit `app.py`.
