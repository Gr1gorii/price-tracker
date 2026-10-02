# Are Black Friday discounts real? — Italy 2026

[![tests](https://github.com/Gr1gorii/price-tracker/actions/workflows/tests.yml/badge.svg)](https://github.com/Gr1gorii/price-tracker/actions/workflows/tests.yml)

Since the EU Omnibus directive (Italy: art. 17-bis Codice del Consumo), an announced price
reduction must be computed from the **lowest price the seller applied in the previous 30 days**.
This project tracks ~2,000 products in Italian online shops (electronics, appliances, beauty,
toys) twice a day from October 2026 and tests whether Black Friday (27 Nov 2026) discounts
respect that rule.

* **Collector** — polite, scheduled scraping: robots.txt-aware, 1 request / 5 s per shop,
  identified User-Agent, schema.org JSON-LD / microdata / CSS parsing, Parquet + DuckDB.
* **Compliance checker** — discount episodes, true 30-day low, claimed vs honest discount,
  flags for inflated reference prices and pre-discount price bumps.
* **Stack** — Python 3.12, uv, httpx, selectolax, extruct, DuckDB, pandas, matplotlib, pytest,
  GitHub Actions (twice-daily cron, DST-safe).

Status: data collection (phase 1). Analysis and write-up after Black Friday.
Charts in `reports/demo/` are generated from **synthetic** data to show the method.

Only prices and product identifiers are stored — no personal data, no reviews.

## Setup

```bash
uv sync                      # Python 3.12 venv + deps
uv run pytest                # tests (synthetic + saved real fixtures)
uv run bfp validate          # check config/
```

Optional, only for shops with `render: true`:

```bash
uv sync --extra render && uv run playwright install chromium
```

## Configure (you)

* `config/shops.yaml` — one entry per shop; `enabled: true` **only after you checked
  robots.txt and the terms**. `runner: local` for shops that block datacenter IPs.
* `config/products.csv` — `shop,url,category,ean` (category ∈ electronics, appliances,
  beauty, toys; EAN optional but helps matching across shops). Keep ≤ ~600 URLs per shop:
  1 request / 5 s ⇒ 600 URLs ≈ 50 min per run.
* Optional `low30_api` per shop: the shop's own public endpoint that its page calls for the
  "30-day lowest price" (Toys Center). Requested only when the page shows a discount, through the
  same 5 s limiter and robots.txt check; a block there counts as a block signal.
* Contact e-mail for the User-Agent: `BFP_CONTACT_EMAIL` — locally in `.env`
  (`BFP_CONTACT_EMAIL=you@example.com`, not committed), on GitHub as a repository secret.
* `config/settings.yaml` — slots, thresholds, checker parameters.

Finding products:

```bash
uv run bfp sitemap <shop> --keywords tv,smartphone --limit 300 --enrich 20
#  → candidates/<shop>.csv: pick rows, fill category, copy to config/products.csv
uv run bfp probe <shop> --n 5            # what the parser sees; add --render to compare with JS
uv run bfp ean-matches                   # EANs present in ≥ 2 shops
```

## Run

```bash
uv run bfp collect --shops a,b --limit 10    # manual run (labelled as a manual slot)
uv run bfp health --runs 4                   # per-shop success table + missing slots
uv run bfp check --from 2026-11-20 --to 2026-11-30   # CSV + 3 PNGs in reports/
uv run bfp sql "select shop, status, count(*) from observations group by all"
uv run bfp demo                              # checker + charts on SYNTHETIC data → reports/demo/
uv run bfp unblock <shop>                    # after re-checking a shop that blocked us
```

### Schedule (GitHub Actions)

`.github/workflows/collect.yml` fires at 06, 07, 08:40, 18, 19, 20:40 UTC. `bfp gate`
converts to Europe/Rome (handles CEST→CET on 25 Oct 2026) and collects only inside the
08:00 / 20:00 windows (+3.5 h for GitHub delays), once per slot. Data is committed back
to the repo by the workflow.

Repo setup: push this folder to GitHub, then in *Settings → Actions → General* allow
"Read and write permissions". Secrets: `BFP_CONTACT_EMAIL` (required), `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` (optional). A public repo has free Actions minutes; a private
one on the Free plan (~2000 min/month) will not fit 2 runs × ~40 min × 31 days.

### Local runner (shops that block datacenter IPs)

```bash
./scripts/install_local.sh          # launchd agent, hourly at :05, gate inside
BFP_GIT_SYNC=1 ./scripts/run_local.sh   # one manual tick; with git commit/push
```

Logs: `~/Library/Logs/bfp/collector.log`. The Mac must be awake (launchd runs missed
firings on wake; the slot window is 3.5 h).

## Rules the collector enforces

* ≥ 5 s between requests to one domain (robots.txt and redirect hops count); robots.txt
  `Crawl-delay` is honoured when larger. Shops run concurrently.
* User-Agent `BFPriceTracker/<ver> (+mailto:<email>; …)` plus a `From:` header.
* robots.txt is checked at runtime with **urllib.robotparser and** a strict matcher for
  `*` / `$` rules (robotparser ignores them); a URL is fetched only if both allow it.
  robots.txt 5xx/unreachable ⇒ nothing is fetched; 401 ⇒ disallow all; 403 ⇒ treated as a block.
* Redirect targets are robots-checked before being followed.
* Block signals (403/429, Cloudflare/Akamai/DataDome/PerimeterX challenge pages): after
  3 in a run the shop is stopped and written to `data/state/blocked.json`; later runs skip
  it until `bfp unblock`. No logins, no CAPTCHA solving, no proxies.
* Nothing personal is stored: only whitelisted product fields. Failed-parse HTML is
  sanitised (reviews, ratings, Q&A, forms, scripts except JSON-LD, e-mails removed),
  gzipped, never committed (uploaded as a 30-day Actions artifact).

## Data

Parquet, one file per slot × runner × shop: `data/observations/date=YYYY-MM-DD/<HH>__<runner>__<shop>.parquet`;
DuckDB view `observations` over all of them (`bfp sql`).

| column | |
|---|---|
| run_id, slot | `2026-10-10T08__actions`, scheduled Rome slot (`…T1530m` = manual run) |
| ts | request time, UTC |
| shop, url, final_url, category, ean | from products.csv (+ URL after redirects) |
| page_gtin, sku, title | as found on the page |
| price, currency | `parse_method`: jsonld → microdata → css |
| displayed_prev_price | strikethrough / "prezzo precedente" (css selector or JSON-LD StrikethroughPrice) |
| displayed_rrp_price | "prezzo consigliato / di listino" (css selector or JSON-LD ListPrice/MSRP) — **not** a previous price |
| displayed_low30_price | "prezzo più basso degli ultimi 30 giorni" (selector or Italian text fallback) |
| displayed_discount_pct | shown −N % (selector only) |
| availability | schema.org name (InStock, OutOfStock, …) |
| display_method | where displayed fields came from: css / jsonld / text |
| status | ok, parse_failed, needs_js, http_error, not_found, blocked, robots_disallowed, network_error, render_error, skipped_blocked |
| http_status, rendered, elapsed_ms, error, html_snapshot, collector_version | diagnostics |

Health manifests per run: `data/health/<slot>__<runner>.json`; report: `reports/health.md`.

## Compliance method (`bfp check`)

For every ok observation that shows a discount (price < displayed previous price, or a
displayed −N %):

* **episode** — contiguous discount-showing observations of a URL with the same displayed
  previous price (±1 %). Missed slots don't break it; an ok observation without discount
  does; a changed reference price starts a new episode. Progressive discounts with the same
  reference stay one episode (art. 17-bis c. 5).
* **window** = `[episode_start − 30 d, episode_start)` — the law counts from the reduction,
  not from each day of the promotion. (The literal per-observation window is reported too as
  `true_low30_rolling`; it makes every promotion look fake from its second day.)
* **history_days** — distinct Rome days with an ok observation in the window; < 25 ⇒
  `insufficient_history`.
* **true_low30** — min *in-stock* price in the window (`in_stock_only`), with `true_low30_ts`.
  With 2 samples a day this is an upper bound of the real minimum ⇒ flags are conservative.
* `honest_discount = 1 − price / true_low30`; `claimed_discount` = shown % or
  `1 − price / displayed_prev_price`; `discount_gap = claimed − honest`.
* reference = displayed previous price, else implied by a shown −N %: `price / (1 − N %)`
  (`reference_source` = prev | pct_implied). RRP ("prezzo consigliato") is reported but never
  treated as the announced reference.
* flags: `reference_above_low30` (reference > true_low30 × 1.01), `price_bump` (a price
  ≥ 10 % above the preceding window minimum), `displayed_low30_above_true` (the shown
  "30-day lowest" is > 1 % above a price we observed), `self_reported_low30_below_price`
  (a discount is announced while the shop's own "30-day lowest" is below the current price —
  needs no history). Flags are also computed with
  insufficient history (a lower price we saw is proof either way), but such rows keep that
  verdict and are excluded from shares.

Outputs: `reports/compliance.csv` (per observation), `reports/episodes.csv` (per episode,
headline = max claimed), `a_price_history.png`, `b_claimed_vs_honest.png`,
`c_flagged_by_category.png`.
