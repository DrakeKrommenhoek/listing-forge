# Marketplace notes and fee verification

## Fee verification status

Fees were checked by web search on **2026-08-02**. Platforms change fees
frequently; treat anything older than a month as unverified and re-check before
relying on a net-proceeds figure for a significant sale.

The numbers below live in `estate/config/pricing.json` under `fees`, expressed
as a fraction of the sale price including payment processing where the platform
bundles it.

| Platform | Effective seller cost | Notes | Verified |
|---|---|---|---|
| eBay | ~13.6% + $0.30–0.40/order | Category-dependent, 12–15%. Clothing raised to ~15.3% in 2026. Below-standard sellers pay +6%. | 2026-08-02 |
| Poshmark | 20% ($15+), flat $2.95 under $15 | Highest of the clothing platforms, but includes the shipping label. | 2026-08-02 |
| Depop (US) | 3.3% + $0.45 payment processing | Lowest fees of the clothing platforms. Boosted listings cost extra. | 2026-08-02 |
| Grailed | 9% commission + 3.49% + $0.49 processing (~12.5% all-in) | Menswear, streetwear, designer only. | 2026-08-02 |
| Reverb | 5% selling fee (min $0.50, cap $500) + 3.19% + $0.49 processing | Configured as ~8.2% all-in. Preferred sellers pay 2.99% processing. | 2026-08-02 |
| Chairish | **30% (Professional) / 40% (Consignor)** | New accounts start on Consignor at 40% and auto-upgrade to Professional at 10 live listings. Only worth it for genuinely designer or antique pieces. | 2026-08-02 |
| Etsy | ~6.5% transaction + listing and processing fees | Vintage must be 20+ years old. | not re-checked |
| Discogs | ~9% | Requires exact pressing identification. | not re-checked |
| Facebook Marketplace / OfferUp / Craigslist / Nextdoor | 0% on local cash sales | Shipping-enabled sales on Facebook and OfferUp do carry a fee — not modelled. | 2026-08-02 |

**Sources**

- eBay: [Taxomate 2026 breakdown](https://taxomate.com/blog/ebay-seller-fees), [Underpriced 2026 guide](https://www.underpriced.app/blog/ebay-fees-complete-guide-2026)
- Poshmark / Depop / Grailed: [Voolist marketplace fee comparison 2026](https://www.voolist.com/blog/marketplace-fees-comparison-2026), [Voolist Grailed fees 2026](https://www.voolist.com/blog/grailed-fees-2026), [SellerFeeCalc Depop US](https://sellerfeecalc.com/depop-fees/us-seller-fees)
- Reverb: [Reverb selling fees](https://reverb.com/selling/selling-fees), [Reverb billing policy](https://reverb.com/legal/reverbs-billing-policy)
- Chairish: [Chairish fee calculator](https://sellerfeescalculator.com/chairish-fee-calculator)

## How the recommender decides

`marketplaces.py` scores every platform against the item and returns the
reasoning. Scoring inputs:

- **Hard blockers** — wrong category, price below/above the platform's
  practical band, pickup-only item on a shipping platform, weight beyond what
  ships economically. A blocked platform is listed under "Ruled out" with the
  reason, so you can see what was considered and why it lost.
- **Category fit** — `specialist=True` platforms (Reverb, Discogs, Poshmark,
  Depop, Grailed, Chairish, Etsy) get a bonus for their native category.
  Craigslist and Nextdoor are *not* specialists: they are generalists with a
  restricted category list.
- **Reach** — audience size, so a broad marketplace is not out-scored by a
  narrow one purely for being narrow.
- **Fee** — penalised proportionally.
- **Effort and time to sell** — penalised; a $30 item should not cost an hour
  of measuring and packing.
- **Value alignment** — cheap items are pushed away from slow, high-fee
  platforms; expensive shippable items get a bonus on low-fee national ones.
- **Move-out urgency** — favours fast local channels when a deadline is set.

## What the system deliberately does not do

- **It does not publish listings.** There is no auto-posting code path. Listing
  packages are generated to `inventory/<ITEM_ID>/copy/` for a human to paste.
  Adding an official API integration (eBay Sell, Etsy) would require your
  explicit authorisation and a new approval step.
- **It does not scrape marketplaces.** Comparable evidence is entered by a
  human from real search results, or later by an official API.
