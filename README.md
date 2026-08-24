# listing-forge

> The sale this was built for completed via manual marketplace posting. This
> automation was never used for a real sale end to end. It is extracted here
> as a reference / potential starting point for a future project, not as a
> working product.

## What it did

A photo-to-listing pipeline for a household move-out / estate sale, built as
an additive module inside a personal assistant app ("Operation D.R.A.K.E."):

- **Telegram intake** (`telegram_estate.py`) — a submitter sends photos of one
  item plus a sentence of context; no slash commands required.
- **Vision identification** (`vision.py`) — mock, Anthropic, or OpenAI provider
  extracts category, condition, dimensions, and other sellable attributes from
  the photos.
- **Comparable research** (`research.py`, `research_provider.py`) — a manual
  queue only; the design deliberately never fabricates market evidence or
  wires in an automated/paid comp source.
- **Pricing engine** (`pricing.py`, `config/pricing.json`) — confidence-scored
  price bands (list / expected / floor) from the comparable median, plus a
  markdown engine that steps the price down on a schedule that accelerates as
  a hard move-out deadline approaches, with a non-negotiable floor.
- **Bundle pricing** (`bundling.py`) — multi-item basket discounts that never
  breach any selected item's floor.
- **Human approval gate** (`approval.py`) — nothing publishes until a reviewer
  approves it; the review web UI (`api.py`, token-gated) is the one place an
  AI-generated price becomes a real listing.
- **Marketplace listing copy** (`listing.py`, `marketplaces.py`) — per-platform
  copy (Craigslist, eBay, Facebook Marketplace, Poshmark, etc.) with fee-aware
  net-proceeds estimates.
- **Static catalogue site** (`site.py`, `scripts/estate_site.py`) — generates
  a public HTML catalogue with EXIF-stripped photos, bundle pricing, and a
  buyer inquiry form.
- **Public inquiry endpoint** (`serverless.py`, `inquiry_validation.py`,
  `inquiry_notifier.py`) — meant to deploy as its own serverless function
  (separate trust boundary, separate Telegram bot token) with honeypot fields,
  input truncation, and IP hashing.
- **Export** (`exporter.py`) — CSV/XLSX inventory export for a shared
  spreadsheet.

Six SQLite tables, all prefixed `estate_`: `estate_items`, `estate_photos`,
`estate_comps`, `estate_submissions`, `estate_events`, `estate_inquiries`.

## How it was meant to run

Inside the host app, `ESTATE_ENABLED=true` registered the Telegram handlers
and mounted the FastAPI router (`/estate/*`) — both additive and fail-safe: if
the module failed to import, the rest of the app kept working. A daily
systemd timer (`listing-forge-markdown.timer`, originally `drake-markdown.timer`)
was supposed to run `scripts/estate_markdown.py --apply` every morning at
7:30 to advance markdowns as the deadline closed in. `scripts/estate_site.py`
rebuilt the static catalogue after each approval batch. The public catalogue
and inquiry endpoint were meant to deploy separately to Vercel.

## What was never installed

The markdown timer and service unit were committed to the host repo but
**never installed on the production VPS** — confirmed via `systemctl status`
returning "could not be found" and zero journal history for the unit, ever.
Markdown pricing, had the sale used this system, would have been 100% manual.
More broadly: the whole pipeline (vision identification, pricing, markdown
cadence, Telegram intake, review/approval, static catalogue, inquiry
endpoint) was built and covered by unit/integration tests, but never
exercised against a real sale in production. The actual sale was run by
posting to marketplaces by hand.

## What would need to change to run this again, standalone

This was extracted from inside the host app, not built to run alone. Concretely:

- **Four shared modules were replaced with one shim**, `src/estate/_compat.py`:
  `get_settings()`/`Settings`, `get_session()`/`init_db()`/`get_engine()`
  (now backed by their own SQLite file instead of the host app's shared
  `agent.db`), `get_logger()`, and a local SQLAlchemy `Base` (the estate
  tables previously attached to the host app's shared declarative base). This
  is a mechanical stand-in — it has not been run.
- **The test suite was not re-verified.** Several integration tests reach
  directly into the host app's config/database modules to reset state between
  runs (`get_settings.cache_clear()`, reassigning internal engine/session
  globals, `Settings(_env_file=None, ...)`). Imports were mechanically
  rewritten to point at `estate._compat`, and `_compat.get_settings` was given
  an `lru_cache` for parity, but none of the 16 estate test files have been
  run against this copy — expect failures.
- **Vision providers need API key wiring.** `AnthropicVisionProvider` and
  `OpenAIVisionProvider` read `settings.anthropic_api_key` /
  `settings.openai_api_key`, which existed on the host app's app-wide
  `Settings` object but aren't defined on `_compat.Settings`. Add them (or
  read the env vars directly) before those providers will work; `mock` needs
  nothing.
- **The public inquiry endpoint's actual deployment isn't here.** Only its
  source (`inquiry_validation.py`, `inquiry_notifier.py`, `serverless.py`) is
  included — the Vercel project itself was generated output, not
  hand-maintained, and was never checked into the host repo either.
- **The systemd units are templates, not a working deploy.** They reference
  `/opt/listing-forge` and a `listingforge` user/venv that don't exist
  anywhere yet.
- **`config/pricing.json`'s deadline is real**, from the original sale
  (`2026-08-31`) — replace it before reusing the pricing engine for anything
  else.
- **No standalone entrypoint exists.** The host app's `main.py` wired FastAPI,
  Telegram, and the markdown cron together; that wiring wasn't extracted, so
  a small runner script would be needed to actually start this as a service.
