# Estate sale system

A photo-to-listing pipeline. Someone photographs an item on their phone; a
human approves a price; the item appears in a catalogue and in ready-to-paste
marketplace copy. Nothing is priced or published without that human step.

## The flow

```
  Telegram                    Pipeline                       Human
  ────────                    ────────                       ─────
  /newitem      ──▶  allocate DK-YYYYMM-NNN
                     create inventory/<ID>/
  send photos   ──▶  store originals, dedupe by hash
  /done         ──▶  vision provider identifies the item
                     withhold every low-confidence field
                ◀──  ask ONLY what it could not determine
  answer                                                     
                     seed logistics defaults
                     generate the comps worksheet
                ◀──  "Saved. Item DK-202608-014"
                     status: Needs Review          ──────▶   review page
                                                             fill in real comps
                                                             check the price
                                                             APPROVE  ◀── the gate
                     price bands, pickup incentive
                     marketplace routing
                     listing copy per platform
                     catalogue site
                     spreadsheet row
                                                             paste the listings
  /estate       ◀──  sale status                             answer buyers
                     markdown engine (never below floor)
```

## Design commitments

**No fabricated evidence.** There is no code path that produces a comparable
without a URL a human supplied or an API returned. Rows without a source are
rejected on import. Placeholder rows are flagged, cap the confidence score, and
block approval outright.

**An AI estimate is never presented as market value.** Prices derive from the
median of the comparable set. With no comparables, the system declines to
recommend a price and says what is missing.

**The floor holds.** Every function that lowers a price clamps against
`floor_price`. The markdown engine, the pickup incentive, and the deadline
endgame all respect it, and the tests assert it.

**Approval is a gate, not a formality.** `prepare_review` cannot write. Only
`apply_decision` can approve, and it refuses on missing evidence, placeholder
evidence, unconfirmed ownership, or unknown condition. The demo asserts the
refusal fires.

**The submitter sees none of this.** Four commands, plain language, no IDs
recited, no confidence scores, no jargon.

## Module map

| Concern | Module |
|---|---|
| What a field is | `schema.py` |
| Where a file goes | `paths.py` |
| Database | `models.py`, `repository.py` |
| Photo → item | `vision.py`, `pipeline.py` |
| Telegram | `telegram_estate.py` |
| Evidence | `research.py` |
| Money | `pricing.py`, `settings.py` |
| Where to sell | `marketplaces.py` |
| What to write | `listing.py` |
| The gate | `approval.py` |
| Web | `api.py`, `site.py`, `images.py` |
| Spreadsheet | `exporter.py` |
| Proof it works | `demo.py` |

## Extending the research layer

`research.py` is the seam for automation. An eBay Marketplace-Insights adapter
or a search-API adapter needs only to produce `Comparable` objects with real
URLs and set `source` accordingly. The worksheet path stays as the fallback and
as the way a human overrides a bad automated match. Confidence scoring, price
bands, and the approval gate are all downstream and need no changes.

## Running it

```bash
python scripts/estate_demo.py --fresh    # everything, offline, no API key
python scripts/estate_export.py          # CSV + workbook
python scripts/estate_site.py            # rebuild the catalogue
python scripts/estate_markdown.py        # dry-run today's markdowns
pytest tests/unit/test_estate.py tests/integration/test_estate_pipeline.py -q
```
