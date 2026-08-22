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
import shutil
import sys
import time
import html
import urllib.request
import urllib.error
from urllib.parse import urlparse, urlsplit, urlunsplit, quote, unquote
from datetime import datetime, timezone

DOMAIN = "theverticaladventurer.com"
CANONICAL_HOST = "www.theverticaladventurer.com"
SITE_ORIGIN = "https://theverticaladventurer.com"
CDX_API = "http://web.archive.org/cdx/search/cdx"
OUT_DIR = "site"
CACHE_DIR = ".cache"
# Files supplied by the author rather than recovered from the Wayback
# Machine, committed to the repo and copied into the build. Currently the
# free PDF book, which the original site delivered by email after a signup
# form, so no crawler ever captured it.
LOCAL_ASSETS_DIR = "assets-local"
# The contact form posted to Wix's (now gone) backend. Worse, it carried
# no action/method, so a live submission would GET the visitor's name and
# email into the URL query string - and its textarea had no name, so the
# message itself was never included. It's replaced with a plain address.
CONTACT_PAGE = "/contact"
CONTACT_EMAIL = "hello@theverticaladventurer.com"
# The original author's personal address, which appears in the archived
# privacy policy and terms of use (as text and in mailto: links). Rewritten
# to the domain address everywhere so the archive never points at an inbox
# that is no longer the right contact.
LEGACY_CONTACT_EMAIL_RE = re.compile(
    r"theverticaladventurer@gmail\.com", re.IGNORECASE
)
PDF_BOOK_PAGE = "/get-outdoors-and-start-adventures"
# (filename, link label) for the downloads this page originally promised.
# The checklists were a further Google Drive link from inside the book.
PDF_BOOK_DOWNLOADS = [
    ("get-outdoors-say-yes-to-adventure.pdf",
     "Get Outdoors &amp; Say Yes to Adventure! (PDF book, 19&nbsp;MB)"),
    ("hiking-gear-packing-checklists.pdf",
     "Printable hiking gear checklists (day &amp; overnight, 0.7&nbsp;MB)"),
]
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
# Tag listings are mirrored so tag links keep visitors on the domain, but
# only in the site's final URL scheme: the two earlier schemes
# (/blog/tag/<Slug>, /blog/search/.hash.<Tag>) list the same posts and are
# redirected here instead of stored again. They're also kept out of the
# sitemap and marked noindex - 75 re-slices of posts that already appear
# on the category pages is exactly the thin, duplicated content that
# search engines discount, and the posts themselves should rank instead.
BLOG_TAG_RE = re.compile(r"^/blog/tags/[a-z0-9\-]+(/page/\d+)?$")
LEGACY_TAG_RE = re.compile(
    r"^/blog/(?:tag/(?P<a>[^/]+)|search/\.hash\.(?P<b>[^/]+))(?:/page/\d+)?$"
)
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


_negative_cache = None


def _negative_cache_path():
    return os.path.join(CACHE_DIR, "confirmed-missing.json")


def load_negative_cache(reset=False):
    """Keys the CDX index confirmed as having nothing archived.

    Recorded only after the full retry sequence below has failed, so a
    transient empty response never lands here - just genuine misses (e.g.
    the ~300 images the Wayback Machine never captured). Without this,
    every rebuild re-queries each of them four times with backoff, which
    is by far the slowest part of a rebuild and never yields anything.
    """
    global _negative_cache
    if reset:
        _negative_cache = {}
        _save_negative_cache()
        return _negative_cache
    if _negative_cache is None:
        try:
            with open(_negative_cache_path(), encoding="utf-8") as f:
                _negative_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _negative_cache = {}
    return _negative_cache


def _save_negative_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_negative_cache_path(), "w", encoding="utf-8") as f:
        json.dump(_negative_cache, f, indent=1, sort_keys=True)


def cdx_query(params, empty_retries=3):
    """Query the CDX API, with caching.

    archive.org occasionally returns a valid HTTP 200 with an empty result
    list under load, indistinguishable from "genuinely nothing archived".
    A cached empty result is therefore never trusted outright: it's
    re-verified (and the retry itself re-verified again if still empty)
    before being accepted, so a transient rate-limit blip can't
    permanently poison the on-disk cache. Only once every retry has come
    back empty is the miss recorded (see load_negative_cache), so later
    rebuilds can skip it instead of paying for the retries again.
    """
    qs = "&".join(f"{k}={quote(str(v), safe='*:/')}" for k, v in params.items())
    url = f"{CDX_API}?{qs}"
    cache_key = "cdx_" + re.sub(r"\W+", "_", qs)
    cache_path = _cache_path(cache_key)

    negative = load_negative_cache()
    if cache_key in negative:
        return []

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

    negative[cache_key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _save_negative_cache()
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
    if (BLOG_LISTING_RE.match(path) or BLOG_CATEGORY_RE.match(path)
            or BLOG_TAG_RE.match(path)):
        return path
    m = SINGLE_POST_RE.match(path)
    if m:
        slug = m.group(1)
        if SINGLE_POST_IS_IMAGE_RE.search(slug):
            return None  # it's an image, not a page - handled as an asset
        return "/single-post/" + slug.lower()
    return None


def normalize_path(path):
    path = path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def nearest_kept_ancestor(path, kept_keys):
    """Walk up the path until a page we actually kept is found."""
    parts = [p for p in (path or "").split("/") if p]
    while parts:
        parts.pop()
        candidate = "/" + "/".join(parts) if parts else "/"
        key = classify_path(candidate)
        if key and key in kept_keys:
            return key
    return "/"


def discover_pages(refresh):
    """Returns (pages, any_capture).

    `pages` is the curated set of pages this build mirrors locally.
    `any_capture` covers every URL the Wayback Machine captured at all
    (regardless of our curation rules) so that links to deliberately
    excluded content (tag pages, old URL schemes, pagination pages beyond
    what we kept) can be pointed at their Wayback capture instead of a
    dead link back to the live domain, which would 404.
    """
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

    kept_groups = {}     # classified key -> candidate rows
    any_groups = {}      # raw normalized path -> candidate rows
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
        any_groups.setdefault(normalize_path(u.path), []).append(r)
        key = classify_path(u.path)
        if key is not None:
            kept_groups.setdefault(key, []).append(r)

    def _best(groups):
        return {k: max(v, key=lambda r: int(r.get("length", 0)))
                for k, v in groups.items()}

    return _best(kept_groups), _best(any_groups)


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
    r"/media/([A-Za-z0-9_%.~\-]+?)(?:~mv2|%7[Ee]mv2)"
    r"(?:_d_[0-9]+_[0-9]+(?:_s_[0-9]+(?:_[0-9]+)?)?)?"
    r"\.([A-Za-z0-9]+)"
    r"(/v1/[^\"'\s)]*)?",
)
FILESUSR_RE = re.compile(
    r"https?://www-theverticaladventurer-com\.filesusr\.com/[^\"'\s)]+",
)
# Documents (PDF checklists/guides) the author uploaded to Wix and linked
# from blog posts. Served from Wix's document CDN rather than the media
# CDN, so they need handling separate from images.
WIX_DOC_RE = re.compile(
    r"https?://(?:docs|static)\.wixstatic\.com/ugd/"
    r"([A-Za-z0-9_]+)\.([A-Za-z0-9]{2,5})",
)
SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
SCRIPT_SELF_CLOSE_RE = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)
WAYBACK_BANNER_RE = re.compile(
    r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT.*?END WAYBACK TOOLBAR INSERT\s*-->",
    re.IGNORECASE | re.DOTALL,
)
NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL)
# Internal links are rewritten only inside real href/src attributes. An
# earlier version matched any quoted run beginning with "/", which also
# matched the "/" starting a self-closing tag - so markup like
#   <img alt="x"/></a></li><li id="y"
# was read as a URL and replaced wholesale, destroying the tags between.
# Excluding <, > and whitespace from the value keeps a match inside one
# attribute.
LINK_ATTR_RE = re.compile(
    r'(?P<pre>\b(?:href|src)\s*=\s*)(?P<q>["\'])(?P<url>[^"\'<>\s]*)(?P=q)',
    re.IGNORECASE,
)
# Wix's runtime JS is stripped, so these preload hints only trigger
# fetches for scripts that can never run (~39 per page, all CSP-blocked).
PRELOAD_SCRIPT_RE = re.compile(
    r'<link\b[^>]*\bas=["\']script["\'][^>]*>', re.IGNORECASE
)
BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
BODY_TAG_RE = re.compile(r"<body\b(?P<attrs>[^>]*)>", re.IGNORECASE)
# Wix also ships the structural page wrappers with an inline
# visibility:hidden and reveals them from JS once hydrated. Only these
# wrappers are unhidden: visibility inherits, so descendants carrying
# their own visibility:hidden stay hidden - which is what we want for the
# dead PayPal button, chat widget, back-to-top, nav dropdowns and the
# offscreen font-measuring rulers.
PREHYDRATE_CONTAINER_RE = re.compile(
    r'<(?:div|main)\b[^>]*?'
    r'(?:id="(?:masterPage|SITE_ROOT|PAGES_CONTAINER|SITE_CONTAINER)"'
    r'|data-is-mesh-layout=)'
    r'[^>]*>',
    re.IGNORECASE,
)
INLINE_HIDDEN_RE = re.compile(r"visibility\s*:\s*hidden\s*;?", re.IGNORECASE)


def reveal_page_containers(text):
    return PREHYDRATE_CONTAINER_RE.sub(
        lambda m: INLINE_HIDDEN_RE.sub("", m.group(0)), text
    )


def hydrate_body_tag(text):
    """Mark the page hydrated, as Wix's stripped runtime JS would.

    Wix hides content until its JS has booted, via
        body:not([data-js-loaded]) [data-hide-prejs] {visibility:hidden}
    and by removing the prewarmup/warmup classes from <body> once loaded.
    That JS is stripped here, so the attribute was never set and every
    blog post body stayed invisible - fully laid out, just not painted.
    Setting it at build time is what the runtime would have done, and is
    safer than force-overriding visibility, which would also reveal
    genuinely hidden UI (tooltips, dropdowns, unsent-form messages).
    """
    def _sub(m):
        attrs = m.group("attrs")

        def _strip_warmup(cm):
            kept = [c for c in cm.group(2).split()
                    if c.lower() not in ("prewarmup", "warmup")]
            return f'class={cm.group(1)}{" ".join(kept)}{cm.group(1)}'

        attrs = re.sub(r'class=(["\'])(.*?)\1', _strip_warmup, attrs,
                       flags=re.IGNORECASE | re.DOTALL)
        if "data-js-loaded" not in attrs.lower():
            attrs = attrs.rstrip() + " data-js-loaded"
        return f"<body{attrs}>"

    return BODY_TAG_RE.sub(_sub, text, count=1)
# Wix ships most images as <wix-image data-image-info='{"imageData":
# {"uri": ...}}'><img></wix-image>, with no src on the <img> - its runtime
# JS reads the JSON and fills src in. That JS is stripped here, so the
# build does the same substitution statically; without it those images
# never load at all.
DATA_IMAGE_INFO_RE = re.compile(r'data-image-info="([^"]*)"', re.IGNORECASE)
WIX_IMAGE_WRAPPER_RE = re.compile(
    # Post pages wrap some images in <div data-image-info> rather than the
    # <wix-image> custom element, so match any wrapper tag.
    r'<[a-z][a-z0-9-]*\b[^>]*?data-image-info="(?P<info>[^"]*)"[^>]*?>\s*<img\b'
    r'(?P<attrs>[^>]*?)(?P<close>/?>)',
    re.IGNORECASE,
)


def media_ref_from_uri(uri):
    """Split a Wix media uri into the (id, extension) this build stores."""
    uri = uri.strip()
    if not uri:
        return None, None
    m = re.match(r"^(?P<id>.+?)(?:~mv2|%7[Ee]mv2).*?\.(?P<ext>[A-Za-z0-9]+)$", uri)
    if not m:
        m = re.match(r"^(?P<id>.+)\.(?P<ext>[A-Za-z0-9]+)$", uri)
    if not m:
        return None, None
    return m.group("id"), m.group("ext").lower()


def discover_data_images(html_text):
    """{media_id: ext} for every <wix-image> on the page."""
    found = {}
    for raw in DATA_IMAGE_INFO_RE.findall(html_text):
        try:
            info = json.loads(html.unescape(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        uri = (info.get("imageData") or {}).get("uri") or info.get("uri")
        if not uri:
            continue
        mid, ext = media_ref_from_uri(uri)
        if mid:
            found[mid] = ext
    return found


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


def fetch_wix_doc(doc_id, ext):
    rows = cdx_query({
        "url": f"docs.wixstatic.com/ugd/{doc_id}.{ext}", "output": "json",
    })
    ok = [r for r in rows if r.get("statuscode") == "200"]
    if not ok:
        return None
    best = max(ok, key=lambda r: int(r.get("length", 0)))
    return fetch(
        wayback_raw_url(best["timestamp"], best["original"]),
        cache_key=f"doc_{doc_id}",
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
    ap.add_argument(
        "--recheck-missing", action="store_true",
        help="Forget which assets were confirmed missing and look for them "
             "again. The Wayback Machine does pick up new captures over "
             "time, so this is worth running occasionally - it just makes "
             "that one build slow again.",
    )
    args = ap.parse_args()
    if args.recheck_missing:
        load_negative_cache(reset=True)
        log("Cleared the confirmed-missing list; re-checking every asset.")

    log(f"Discovering pages for {DOMAIN} from the Wayback CDX index...")
    pages, any_capture = discover_pages(refresh=args.refresh_cdx)
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
    doc_manifest = {}    # doc_id -> ext
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
        media_manifest.update(discover_data_images(text))
        for m in FILESUSR_RE.finditer(text):
            filesusr_seen.add(m.group(0))
        for m in WIX_DOC_RE.finditer(text):
            doc_manifest[m.group(1)] = m.group(2).lower()
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

    log("Downloading linked documents (PDF guides/checklists)...")
    downloaded_docs = {}
    for doc_id, ext in sorted(doc_manifest.items()):
        data = fetch_wix_doc(doc_id, ext)
        if not data:
            log(f"  ! {doc_id}.{ext} not archived, leaving link as-is")
            continue
        downloaded_docs[doc_id] = ext
        out_path = os.path.join(OUT_DIR, f"assets/files/{doc_id}.{ext}")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
    log(f"Saved {len(downloaded_docs)}/{len(doc_manifest)} documents.")

    log("Rewriting HTML (internal links + image references)...")
    for key, text in page_html.items():
        text = WAYBACK_BANNER_RE.sub("", text)
        text = SCRIPT_TAG_RE.sub("", text)
        text = SCRIPT_SELF_CLOSE_RE.sub("", text)
        text = NOSCRIPT_RE.sub("", text)
        text = PRELOAD_SCRIPT_RE.sub("", text)
        text = BASE_TAG_RE.sub("", text)
        text = hydrate_body_tag(text)
        text = reveal_page_containers(text)
        text = LEGACY_CONTACT_EMAIL_RE.sub(CONTACT_EMAIL, text)

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

        # Do statically what Wix's (stripped) JS did at runtime: give each
        # <wix-image>'s inner <img> the src from its data-image-info JSON.
        def _wix_image_sub(m, key=key):
            full = m.group(0)
            if re.search(r'\bsrc\s*=', m.group("attrs"), re.IGNORECASE):
                return full
            try:
                info = json.loads(html.unescape(m.group("info")))
            except (json.JSONDecodeError, TypeError):
                return full
            uri = (info.get("imageData") or {}).get("uri") or info.get("uri")
            if not uri:
                return full
            mid, _ = media_ref_from_uri(uri)
            if mid not in downloaded_media:
                return full
            local = asset_local_name(mid, downloaded_media[mid])
            src = "../" * page_out_path(key).count("/") + local
            return full[:-len(m.group("close"))] + \
                f' src="{src}" loading="lazy"' + m.group("close")

        text = WIX_IMAGE_WRAPPER_RE.sub(_wix_image_sub, text)

        def _doc_sub(m, key=key):
            doc_id = m.group(1)
            if doc_id not in downloaded_docs:
                return m.group(0)
            depth = page_out_path(key).count("/")
            return ("../" * depth
                    + f"assets/files/{doc_id}.{downloaded_docs[doc_id]}")

        text = WIX_DOC_RE.sub(_doc_sub, text)

        for url, rel in filesusr_local.items():
            depth = page_out_path(key).count("/")
            text = text.replace(url, "../" * depth + rel)

        # Rewrite internal links (both raw and any leftover /web/<ts>/ forms).
        # An internal link must never be left pointing at the live domain:
        # this archive only hosts a curated subset, so anything else would
        # 404 once deployed. Resolution order:
        #   1. mirrored locally -> relative path
        #   2. captured by Wayback but not mirrored -> its Wayback capture
        #   3. neither -> nearest kept ancestor page
        def _link_sub(m, key=key):
            full, pre, q = m.group(0), m.group("pre"), m.group("q")
            href = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*/", "",
                          m.group("url"))
            parsed = urlsplit(href)
            if parsed.netloc and parsed.netloc.lower() not in (
                CANONICAL_HOST, DOMAIN, "www." + DOMAIN,
            ):
                return full  # external link, leave alone
            if not parsed.netloc and not href.startswith("/"):
                # Already relative (media/document rewrites run before this),
                # or a fragment, mailto:, data: URI - all fine as they are.
                return full
            path = normalize_path(parsed.path)
            target_key = classify_path(path)
            if target_key and target_key in page_html:
                return pre + q + rel_path_between(key, target_key) + q
            captured = any_capture.get(path)
            if captured:
                return (pre + q
                        + f"https://web.archive.org/web/{captured['timestamp']}/"
                        + captured["original"] + q)
            fallback = nearest_kept_ancestor(path, page_html)
            return pre + q + rel_path_between(key, fallback) + q

        text = LINK_ATTR_RE.sub(_link_sub, text)

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

        if key == CONTACT_PAGE:
            text = replace_contact_form(text)

        # These were the reward for a signup form that posted to Wix's
        # (now gone) backend, so the page's own call to action is dead.
        # Offer the files directly instead.
        if key == PDF_BOOK_PAGE:
            depth = page_out_path(key).count("/")
            available = [
                (fn, label) for fn, label in PDF_BOOK_DOWNLOADS
                if os.path.exists(os.path.join(LOCAL_ASSETS_DIR, fn))
            ]
            if available:
                links = " &nbsp;·&nbsp; ".join(
                    f'<a href="{"../" * depth}assets/files/{fn}" download '
                    f'style="color:#1a1a1a;font-weight:700">{label}</a>'
                    for fn, label in available
                )
                callout = (
                    '<div style="background:#f7941e;color:#1a1a1a;'
                    'font:15px/1.6 -apple-system,sans-serif;text-align:center;'
                    'padding:14px 12px">The email signup that used to deliver '
                    'these no longer works &mdash; download them directly:<br>'
                    + links + '</div>'
                )
                text = re.sub(r"(<body[^>]*>)", r"\1" + callout, text,
                              count=1, flags=re.I)

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

    copy_local_assets()
    write_redirects(pages, any_capture)
    write_sitemap(pages, page_meta)
    write_llms_txt(pages, page_meta)
    write_headers_file()
    log("Done. Output written to ./site")


def replace_contact_form(text):
    """Swap the dead contact form for the address, in place."""
    start = text.find("<form")
    if start == -1:
        return text
    end = text.find("</form>", start)
    if end == -1:
        return text
    end += len("</form>")
    replacement = (
        '<div style="font:16px/1.7 -apple-system,sans-serif;color:#2b2b2b;'
        'max-width:600px;margin:0 auto;padding:22px 18px;text-align:center;'
        'border:1px solid #e0e0e0;border-radius:4px;background:#fafafa">'
        'This is a historical archive, so the original contact form no '
        'longer works.<br><br>To get in touch, email '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:#c1720d;'
        f'font-weight:700">{CONTACT_EMAIL}</a></div>'
    )
    return text[:start] + replacement + text[end:]


def write_redirects(pages, any_capture):
    """Cloudflare Pages reads a _redirects file at the site root natively.

    The original site served the same post under several URL spellings -
    notably mixed-case slugs (/single-post/Summit-Fever-and-How-to-Avoid-It)
    alongside lowercase ones. This archive stores one lowercase copy of
    each, and static hosting is case-sensitive, so every old spelling would
    404. That matters beyond old inbound links and search results: the
    author's PDF book links to the mixed-case spellings throughout.
    """
    seen = {}
    for path in any_capture:
        key = classify_path(path)
        if not key or key not in pages:
            continue
        canonical = "/" if key == "/" else key + "/"
        if normalize_path(path) == normalize_path(canonical):
            continue
        seen[path] = canonical

    # The site's two earlier tag URL schemes list the same posts as the
    # mirrored current scheme, so they redirect there rather than being
    # stored again. Their slugs need normalising: /blog/tag/Skills-%26-Advice
    # is /blog/tags/skills-26-advice today.
    for path in any_capture:
        m = LEGACY_TAG_RE.match(normalize_path(path))
        if not m:
            continue
        slug = unquote(m.group("a") or m.group("b")).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug.replace("&", "26")).strip("-")
        target = f"/blog/tags/{slug}"
        if target in pages:
            seen.setdefault(normalize_path(path), target + "/")

    # A typo baked into the PDF book itself (missing hyphen), so it was
    # broken on the original site too - worth catching here.
    for path, key in list(seen.items()):
        if path.startswith("/single-post/"):
            seen.setdefault(path.replace("/single-post/", "/singlepost/", 1), key)
    for key in pages:
        if key.startswith("/single-post/"):
            seen.setdefault(key.replace("/single-post/", "/singlepost/", 1),
                            key + "/")

    lines = [f"{src}  {dst}  301" for src, dst in sorted(seen.items())]
    with open(os.path.join(OUT_DIR, "_redirects"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"Wrote {len(lines)} redirects for legacy URL spellings.")


def copy_local_assets():
    """Copy author-supplied files (see LOCAL_ASSETS_DIR) into the build."""
    if not os.path.isdir(LOCAL_ASSETS_DIR):
        return
    dest_dir = os.path.join(OUT_DIR, "assets", "files")
    os.makedirs(dest_dir, exist_ok=True)
    copied = 0
    for name in sorted(os.listdir(LOCAL_ASSETS_DIR)):
        src = os.path.join(LOCAL_ASSETS_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest_dir, name))
            copied += 1
    log(f"Copied {copied} author-supplied asset(s) from {LOCAL_ASSETS_DIR}/.")


def write_robots(pages):
    with open(os.path.join(OUT_DIR, "robots.txt"), "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\n\nSitemap: "
                f"{SITE_ORIGIN}/sitemap.xml\n")


def write_headers_file():
    """Cloudflare Pages reads a _headers file at the site root natively.

    The archive has no legitimate JavaScript at all (Wix's runtime JS was
    stripped during rewriting) and only two live third-party iframes
    (Google Maps / YouTube embeds in a couple of posts) - everything else
    that looks like a third-party widget (Wix chat, a comments plugin, a
    PayPal button, a "back to top" widget) is inert `data-src` markup left
    over from the original Wix export, since no script exists to promote it
    to a real `src`. This CSP makes that permanent: it blocks all script
    execution and restricts frames to just Google/YouTube, so none of that
    dormant markup can ever become live again.
    """
    csp = (
        "default-src 'none'; "
        # static.wixstatic.com is allowed as a fallback for the ~300 images
        # that were never archived by the Wayback Machine and so still
        # point at Wix's (still-operating) CDN instead of a local copy.
        "img-src 'self' data: https://static.wixstatic.com; "
        "style-src 'self' 'unsafe-inline'; "
        # static.wixstatic.com/ufonts/ serves the site's custom uploaded
        # fonts; without it headings fall back to a generic serif.
        "font-src 'self' data: https://static.parastorage.com "
        "https://fonts.gstatic.com https://static.wixstatic.com; "
        "frame-src https://www.google.com https://www.youtube.com "
        "https://www.youtube-nocookie.com; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    lines = [
        "/*",
        "  X-Content-Type-Options: nosniff",
        "  X-Frame-Options: SAMEORIGIN",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        f"  Content-Security-Policy: {csp}",
        "",
        # Mirrored so tag links stay on the domain, but kept out of search
        # results - they re-slice posts that already rank on their own.
        "/blog/tags/*",
        "  X-Robots-Tag: noindex, follow",
    ]
    with open(os.path.join(OUT_DIR, "_headers"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_sitemap(pages, page_meta):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for key in sorted(pages):
        if BLOG_TAG_RE.match(key):
            continue  # noindex - see BLOG_TAG_RE
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
    site_pages = sorted(
        k for k in pages
        if k not in posts and k != "/" and not BLOG_TAG_RE.match(k)
    )

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
