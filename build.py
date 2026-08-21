#!/usr/bin/env python3
"""
Rebuild ./site as a static archive of theverticaladventurer.com, sourced
entirely from the Internet Archive Wayback Machine.

The original site (a Wix blog, active roughly 2017-2022) went offline and
the domain lapsed; between 2022 and 2025 it was held by a parking/spam
service. This script pulls only the real site content from before the
domain lapsed, discards the parking-era junk, rewrites internal links and
images to work as a self-contained static site, and generates sitemap.xml
and llms.txt for the result.

Usage:
    python build.py             # (re)build ./site
    python build.py --dry-run   # print the page/asset plan, download nothing
    python build.py --refresh-cdx  # re-query the Wayback CDX index instead
                                    # of reusing .cache/cdx.json

Everything fetched from the Wayback Machine is cached under .cache/ (not
committed) so re-runs are fast and don't re-hit archive.org.
"""

import argparse
import json
import os
import re
import sys
import time
import html
import urllib.request
import urllib.error
from urllib.parse import urlparse, urlsplit, urlunsplit, quote
from datetime import datetime, timezone

DOMAIN = "theverticaladventurer.com"
CANONICAL_HOST = "www.theverticaladventurer.com"
SITE_ORIGIN = "https://theverticaladventurer.com"
CDX_API = "http://web.archive.org/cdx/search/cdx"
OUT_DIR = "site"
CACHE_DIR = ".cache"
USER_AGENT = (
    "Mozilla/5.0 (compatible; VerticalAdventurerArchiveBot/1.0; "
    "+https://github.com/Luen/The-Vertical-Adventurer; archival mirror build)"
)
REQUEST_DELAY = 0.2  # be polite to archive.org between requests
MEDIA_HOSTS = ("static.wixstatic.com", "video.wixstatic.com")
FILESUSR_HOST = "www-theverticaladventurer-com.filesusr.com"

# ---------------------------------------------------------------------------
# Page selection rules
# ---------------------------------------------------------------------------
# Static, single-instance pages worth preserving as-is.
FIXED_PAGES = {
    "/", "/about-me", "/adventures-and-trips", "/australia", "/canyoning",
    "/canyoning-gear", "/climbing", "/contact", "/disclaimer", "/gallery",
    "/gear", "/get-outdoors-and-start-adventures", "/guest-post-guidelines",
    "/hiking-and-outdoors", "/hiking-gear", "/ice-and-mixed-climbing-gear",
    "/new-zealand", "/outdoor-skills-and-advice", "/privacy-policy",
    "/rock-climbing-gear", "/subscribe", "/support", "/terms-of-use",
    "/thanks", "/travel", "/vanlife", "/work-with-me",
}
BLOG_LISTING_RE = re.compile(r"^/blog(/page/\d+)?$")
BLOG_CATEGORY_RE = re.compile(r"^/blog/categories/[a-z0-9\-]+(/page/\d+)?$")
SINGLE_POST_RE = re.compile(r"^/single-post/([^/]+)$")
# single-post/<hash>~mv2....jpg style entries are mis-resolved image URLs
# from the old site, not real pages - keep the underlying image, not the page.
SINGLE_POST_IS_IMAGE_RE = re.compile(
    r"(~mv2|\.(?:jpe?g|png|svg|gif|webp))$", re.IGNORECASE
)

log_lines = []


def log(msg):
    print(msg)
    log_lines.append(msg)


# ---------------------------------------------------------------------------
# HTTP + cache helpers
# ---------------------------------------------------------------------------
def _cache_path(key):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:180]
    return os.path.join(CACHE_DIR, safe + ".bin")


def fetch(url, cache_key=None, retries=4):
    """GET a URL with on-disk caching and retry/backoff."""
    cache_key = cache_key or url
    path = _cache_path(cache_key)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    os.makedirs(CACHE_DIR, exist_ok=True)
    delay = 1.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
            time.sleep(REQUEST_DELAY)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001 - network flakiness
            last_err = e
        time.sleep(delay)
        delay *= 2
    log(f"  ! failed to fetch {url}: {last_err}")
    return None


def _parse_cdx_bytes(data):
    if not data:
        return []
    try:
        rows = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        return []
    if not rows:
        return []
    header, *body = rows
    return [dict(zip(header, r)) for r in body]


def cdx_query(params, empty_retries=3):
    """Query the CDX API, with caching.

    archive.org occasionally returns a valid HTTP 200 with an empty result
    list under load, indistinguishable from "genuinely nothing archived".
    A cached empty result is therefore never trusted outright: it's
    re-verified (and the retry itself re-verified again if still empty)
    before being accepted, so a transient rate-limit blip can't
    permanently poison the on-disk cache.
    """
    qs = "&".join(f"{k}={quote(str(v), safe='*:/')}" for k, v in params.items())
    url = f"{CDX_API}?{qs}"
    cache_key = "cdx_" + re.sub(r"\W+", "_", qs)
    cache_path = _cache_path(cache_key)

    for attempt in range(empty_retries + 1):
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                rows = _parse_cdx_bytes(f.read())
            if rows:
                return rows
            os.remove(cache_path)  # cached empty result - don't trust it blindly
        data = fetch(url, cache_key=cache_key, retries=5)
        rows = _parse_cdx_bytes(data)
        if rows:
            return rows
        if os.path.exists(cache_path):
            os.remove(cache_path)  # don't let a possibly-transient empty persist
        if attempt < empty_retries:
            time.sleep(1.5 * (attempt + 1))
    return []


def wayback_raw_url(timestamp, original_url):
    return f"http://web.archive.org/web/{timestamp}id_/{original_url}"


# ---------------------------------------------------------------------------
# Step 1: discover pages worth keeping from the domain-wide CDX index
# ---------------------------------------------------------------------------
def classify_path(path):
    """Return a group key for a path, or None if it should be discarded."""
    path = path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    if path in FIXED_PAGES or (path == "" and "/" in FIXED_PAGES):
        return "/" if path == "" else path
    if path == "/home":
        return "/"
    if BLOG_LISTING_RE.match(path) or BLOG_CATEGORY_RE.match(path):
        return path
    m = SINGLE_POST_RE.match(path)
    if m:
        slug = m.group(1)
        if SINGLE_POST_IS_IMAGE_RE.search(slug):
            return None  # it's an image, not a page - handled as an asset
        return "/single-post/" + slug.lower()
    return None


def discover_pages(refresh):
    cache_file = os.path.join(CACHE_DIR, "cdx_domain.json")
    if refresh and os.path.exists(cache_file):
        os.remove(cache_file)
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            rows = json.load(f)
    else:
        rows = cdx_query({
            "url": DOMAIN,
            "output": "json",
            "matchType": "domain",
        })
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(rows, f)

    groups = {}  # group_key -> list of candidate rows
    for r in rows:
        if r.get("statuscode") != "200":
            continue
        try:
            length = int(r.get("length", 0))
        except ValueError:
            length = 0
        if length < 500:
            continue  # too small to be a real rendered page
        u = urlsplit(r["original"])
        key = classify_path(u.path)
        if key is None:
            continue
        groups.setdefault(key, []).append(r)

    pages = {}
    for key, candidates in groups.items():
        best = max(candidates, key=lambda r: int(r.get("length", 0)))
        pages[key] = best
    return pages


def find_robots_and_favicon():
    robots = cdx_query({
        "url": f"{CANONICAL_HOST}/robots.txt", "output": "json",
    })
    favicon = cdx_query({
        "url": f"{CANONICAL_HOST}/favicon.ico", "output": "json",
    })
    best_robots = max(
        (r for r in robots if r.get("statuscode") == "200"),
        key=lambda r: int(r.get("length", 0)), default=None,
    )
    best_favicon = max(
        (r for r in favicon if r.get("statuscode") == "200"),
        key=lambda r: int(r.get("length", 0)), default=None,
    )
    return best_robots, best_favicon


# ---------------------------------------------------------------------------
# Step 2: download pages, discover + download media assets, rewrite HTML
# ---------------------------------------------------------------------------
WIX_MEDIA_RE = re.compile(
    r"https?://(?:static\.wixstatic\.com|video\.wixstatic\.com)"
    r"/media/([A-Za-z0-9_%.~\-]+?)~mv2"
    r"(?:_d_[0-9]+_[0-9]+(?:_s_[0-9]+(?:_[0-9]+)?)?)?"
    r"\.([A-Za-z0-9]+)"
    r"(/v1/[^\"'\s)]*)?",
)
FILESUSR_RE = re.compile(
    r"https?://www-theverticaladventurer-com\.filesusr\.com/[^\"'\s)]+",
)
SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
SCRIPT_SELF_CLOSE_RE = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)
WAYBACK_BANNER_RE = re.compile(
    r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT.*?END WAYBACK TOOLBAR INSERT\s*-->",
    re.IGNORECASE | re.DOTALL,
)
NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL)


def asset_local_name(media_id, ext):
    ext = ext.lstrip(".") or "jpg"
    return f"assets/media/{media_id}.{ext}"


def discover_media(html_text):
    """Return {full_matched_url_pattern: (media_id, ext)} for wixstatic media."""
    found = {}
    for m in WIX_MEDIA_RE.finditer(html_text):
        media_id, ext = m.group(1), m.group(2)
        found[media_id] = ext.lower() if ext else "jpg"
    return found


def fetch_best_media(media_id, ext):
    rows = cdx_query({
        "url": f"static.wixstatic.com/media/{media_id}",
        "output": "json",
        "matchType": "prefix",
    })
    ok = [r for r in rows if r.get("statuscode") == "200"]
    if not ok:
        return None
    best = max(ok, key=lambda r: int(r.get("length", 0)))
    data = fetch(
        wayback_raw_url(best["timestamp"], best["original"]),
        cache_key=f"media_{media_id}",
    )
    return data


def fetch_filesusr(url):
    rows = cdx_query({"url": url, "output": "json"})
    ok = [r for r in rows if r.get("statuscode") == "200"]
    if not ok:
        return None
    best = max(ok, key=lambda r: int(r.get("length", 0)))
    return fetch(
        wayback_raw_url(best["timestamp"], best["original"]),
        cache_key="filesusr_" + re.sub(r"\W+", "_", url),
    )


def page_out_path(key):
    if key == "/":
        return "index.html"
    return key.strip("/") + "/index.html"


def rel_path_between(from_key, to_key):
    from_path = page_out_path(from_key)
    to_path = page_out_path(to_key)
    from_dir = os.path.dirname(from_path)
    rel = os.path.relpath(to_path, from_dir or ".")
    return rel.replace(os.sep, "/")


def build():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-cdx", action="store_true")
    args = ap.parse_args()

    log(f"Discovering pages for {DOMAIN} from the Wayback CDX index...")
    pages = discover_pages(refresh=args.refresh_cdx)
    robots_row, favicon_row = find_robots_and_favicon()
    log(f"Keeping {len(pages)} pages (junk/parking-era captures excluded).")

    if args.dry_run:
        for key in sorted(pages):
            r = pages[key]
            log(f"  {key}  <- {r['timestamp']} ({r['length']} bytes)")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    media_manifest = {}  # media_id -> ext
    filesusr_seen = set()
    page_html = {}       # key -> raw html text
    page_meta = {}        # key -> dict(title, description, timestamp)

    log("Downloading pages...")
    for i, (key, row) in enumerate(sorted(pages.items()), 1):
        raw = fetch(
            wayback_raw_url(row["timestamp"], row["original"]),
            cache_key=f"page_{key}",
        )
        if not raw:
            log(f"  ! could not fetch {key}, skipping")
            continue
        text = raw.decode("utf-8", errors="replace")
        page_html[key] = text
        media_manifest.update(discover_media(text))
        for m in FILESUSR_RE.finditer(text):
            filesusr_seen.add(m.group(0))
        title_m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        desc_m = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            text, re.I,
        )
        page_meta[key] = {
            "title": html.unescape(title_m.group(1)).strip() if title_m else key,
            "description": html.unescape(desc_m.group(1)).strip() if desc_m else "",
            "timestamp": row["timestamp"],
        }
        if i % 20 == 0:
            log(f"  ...{i}/{len(pages)} pages fetched")
    log(f"Fetched {len(page_html)} pages. Found {len(media_manifest)} unique images "
        f"and {len(filesusr_seen)} filesusr assets referenced.")

    log("Downloading images (this is the slow part)...")
    downloaded_media = {}
    for i, (media_id, ext) in enumerate(sorted(media_manifest.items()), 1):
        data = fetch_best_media(media_id, ext)
        if data:
            downloaded_media[media_id] = ext
            out_path = os.path.join(OUT_DIR, asset_local_name(media_id, ext))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(data)
        if i % 50 == 0:
            log(f"  ...{i}/{len(media_manifest)} images processed")
    log(f"Saved {len(downloaded_media)}/{len(media_manifest)} images "
        f"({len(media_manifest) - len(downloaded_media)} not archived, skipped).")

    log("Downloading filesusr assets (docs/fonts/misc files)...")
    filesusr_local = {}
    for i, url in enumerate(sorted(filesusr_seen), 1):
        data = fetch_filesusr(url)
        if not data:
            continue
        name = os.path.basename(urlsplit(url).path) or f"file{i}"
        rel = f"assets/files/{name}"
        out_path = os.path.join(OUT_DIR, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        filesusr_local[url] = rel

    log("Rewriting HTML (internal links + image references)...")
    for key, text in page_html.items():
        text = WAYBACK_BANNER_RE.sub("", text)
        text = SCRIPT_TAG_RE.sub("", text)
        text = SCRIPT_SELF_CLOSE_RE.sub("", text)
        text = NOSCRIPT_RE.sub("", text)

        # Rewrite wixstatic media references (all resize variants) to the
        # single locally-saved original file.
        def _media_sub(m, key=key):
            media_id, ext = m.group(1), (m.group(2) or "").lstrip(".") or "jpg"
            if media_id not in downloaded_media:
                return m.group(0)
            local = asset_local_name(media_id, downloaded_media[media_id])
            depth = page_out_path(key).count("/")
            prefix = "../" * depth
            return prefix + local

        text = WIX_MEDIA_RE.sub(_media_sub, text)

        for url, rel in filesusr_local.items():
            depth = page_out_path(key).count("/")
            text = text.replace(url, "../" * depth + rel)

        # Rewrite internal links (both raw and any leftover /web/<ts>/ forms)
        # to relative local paths for pages we kept; leave everything else
        # (external sites, images already handled above) untouched.
        def _link_sub(m, key=key):
            full = m.group(0)
            quote_char = full[0]
            href = m.group(2)
            href = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*/", "", href)
            parsed = urlsplit(href)
            if parsed.netloc and parsed.netloc.lower() not in (
                CANONICAL_HOST, DOMAIN, "www." + DOMAIN,
            ):
                return full  # external link, leave alone
            path = parsed.path or "/"
            target_key = classify_path(path)
            if target_key and target_key in page_html:
                rel = rel_path_between(key, target_key)
                return quote_char + rel + quote_char
            return full

        text = re.sub(
            r'(["\'])((?:https?://(?:www\.)?theverticaladventurer\.com|'
            r'https?://web\.archive\.org/web/\d+[a-z_]*/https?://(?:www\.)?'
            r'theverticaladventurer\.com|/)[^"\']*)\1'.replace("\\1", "\\1"),
            lambda m: _link_sub(m),
            text,
        )

        banner = (
            f'<div style="background:#222;color:#eee;font:13px/1.5 -apple-system,'
            f'sans-serif;text-align:center;padding:8px 12px">'
            f'Historical archive of theverticaladventurer.com, captured '
            f'{page_meta[key]["timestamp"][:4]}-{page_meta[key]["timestamp"][4:6]}-'
            f'{page_meta[key]["timestamp"][6:8]} via the '
            f'<a href="https://web.archive.org/web/{page_meta[key]["timestamp"]}/'
            f'{pages[key]["original"]}" style="color:#9cf" target="_blank" '
            f'rel="noopener">Wayback Machine</a>. Not the live site.</div>'
        )
        text = re.sub(r"(<body[^>]*>)", r"\1" + banner, text, count=1, flags=re.I)

        out_path = os.path.join(OUT_DIR, page_out_path(key))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)

    if robots_row:
        data = fetch(wayback_raw_url(robots_row["timestamp"], robots_row["original"]),
                      cache_key="robots_txt_src")
    write_robots(pages)

    if favicon_row:
        data = fetch(wayback_raw_url(favicon_row["timestamp"], favicon_row["original"]),
                      cache_key="favicon_src")
        if data:
            with open(os.path.join(OUT_DIR, "favicon.ico"), "wb") as f:
                f.write(data)

    write_sitemap(pages, page_meta)
    write_llms_txt(pages, page_meta)
    log("Done. Output written to ./site")


def write_robots(pages):
    with open(os.path.join(OUT_DIR, "robots.txt"), "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\n\nSitemap: "
                f"{SITE_ORIGIN}/sitemap.xml\n")


def write_sitemap(pages, page_meta):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for key in sorted(pages):
        ts = page_meta[key]["timestamp"]
        lastmod = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        loc = SITE_ORIGIN + ("/" if key == "/" else key + "/")
        lines.append("  <url>")
        lines.append(f"    <loc>{html.escape(loc)}</loc>")
        lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    with open(os.path.join(OUT_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_llms_txt(pages, page_meta):
    posts = sorted(k for k in pages if k.startswith("/single-post/"))
    site_pages = sorted(k for k in pages if k not in posts and k != "/")

    def entry(key):
        title = page_meta[key]["title"]
        desc = page_meta[key]["description"]
        loc = SITE_ORIGIN + ("/" if key == "/" else key + "/")
        line = f"- [{title}]({loc})"
        if desc:
            line += f": {desc}"
        return line

    lines = [
        "# The Vertical Adventurer (Archive)",
        "",
        "> A historical archive of theverticaladventurer.com, a hiking, "
        "climbing, canyoning and van-life blog covering New Zealand and "
        "Australia, active roughly 2017-2022. Content was recovered from "
        "the Internet Archive Wayback Machine and is preserved as-is for "
        "historical reference; it is not an active or maintained blog.",
        "",
        "## Site pages",
        "",
    ]
    for key in site_pages:
        lines.append(entry(key))
    lines += ["", "## Blog posts", ""]
    for key in posts:
        lines.append(entry(key))
    lines += [
        "",
        "## Notes",
        "",
        "- This archive excludes blog tag/category listing pages and "
        "duplicate legacy URL slugs to avoid redundant content.",
        "- Images are the original files recovered from the Wayback "
        "Machine where available; some low-traffic images were never "
        "archived by the Wayback Machine and are missing.",
        "- Source and rebuild instructions: "
        "https://github.com/Luen/The-Vertical-Adventurer",
    ]
    with open(os.path.join(OUT_DIR, "llms.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    build()
