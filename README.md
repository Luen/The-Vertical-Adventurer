# The Vertical Adventurer — Archive

This repository is a historical archive of **theverticaladventurer.com**, a
hiking, climbing, canyoning and van-life blog covering New Zealand and
Australia. The original site ran on Wix from roughly 2017 to 2022, then went
offline; the domain later lapsed and was held by a domain-parking service
until it was bought back in 2026. This repo restores the original content
from the [Wayback Machine](https://web.archive.org/) so it can be hosted
again on the same domain for historical/archival purposes — it is **not**
an active or maintained blog.

## What's here

- [`build.py`](build.py) — regenerates the [`site/`](site/) directory from
  the Wayback Machine's CDX index. It fetches raw (unmodified) captures of
  every real page and image, rewrites internal links and image references to
  point at the local copies, strips Wix's runtime JavaScript (which can't
  function without Wix's backend anyway), and adds a small banner to every
  page noting it's an archive.
- [`site/`](site/) — the built static site: ~168 pages (all blog posts,
  category pages and site pages that existed on the live site) plus every
  image that the Wayback Machine had archived for them, `sitemap.xml`,
  `llms.txt`, and `robots.txt`.

## Rebuilding

```bash
pip install -r requirements.txt   # none required beyond the standard library
python build.py --dry-run         # preview which pages will be kept
python build.py                   # fetch everything and rebuild ./site
```

Downloads are cached under `.cache/` (not committed) so re-runs are fast.
Use `python build.py --refresh-cdx` to re-query the Wayback Machine's index
instead of reusing the cached list of captures — useful if new snapshots
become available.

### What gets kept vs. discarded

Between 2022 and 2025 the (by-then expired) domain was parked and used to
serve ad-network probe files (`ads.txt`, random `*.js` files,
`.well-known/*` files, etc.) rather than real content. `build.py` filters
these out automatically — it only keeps pages that returned a real,
substantial (200 OK, >500 byte) response, which the parking-era junk never
does. It also drops the site's old blog tag/category-listing pages and
duplicate legacy URL slugs to avoid archiving redundant near-duplicate
content; every actual blog post and site page is kept.

Some images referenced by the site were never captured by the Wayback
Machine and are simply missing from the archive — there's no way to recover
those.

## Deploying

`site/` is a plain static site (clean URLs, no build step, no server-side
code) and is intended to be deployed as-is to
[Cloudflare Pages](https://pages.cloudflare.com/):

- **Build output directory:** `site`
- **Build command:** (none — commit the built output, or run `python build.py`
  as the Pages build command)
- **Root directory:** `/`

## Original site credit

All written content, photography and branding belongs to the original
author of theverticaladventurer.com. This archive exists solely to preserve
that work now that the site itself is gone.
