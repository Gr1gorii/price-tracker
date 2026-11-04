# Black Friday 2026: pre-registered hypotheses

Written on 2026-11-04, before any Black Friday data exists. Thresholds and windows below
are fixed now and are implemented in `src/bfp/hypotheses.py`. Any change made after
2026-11-20 is reported in the write-up as a deviation.

Run: `uv run bfp hypotheses` writes `reports/hypotheses.md`. Until 20 Nov every verdict is
"pending".

## Windows and units

* **Baseline**: Rome dates 2026-10-05 to 2026-11-15.
* **BF window**: Rome dates 2026-11-20 to 2026-12-01 (Black Friday 27 Nov, Cyber Monday 30 Nov,
  last collection day 1 Dec).
* **Unit of time**: one scheduled collection slot (08:00 or 20:00 Rome). Reactions faster than
  12 h are not observable. Manual runs are ignored.
* Prices are compared with a 0.5 % tolerance ("equal" means within 0.5 %).
* H3 to H5 use discount episodes from `bfp check` that start inside the BF window and have
  sufficient history (at least 25 days in the 30-day window).

## What the data allows (checked on 2026-11-04)

* Cross-shop matching by identifier is not possible: no EAN appears in two shops
  (`bfp ean-matches`: 0), and only Notino exposes a page GTIN.
* Identical products in several shops exist only for **LEGO sets**, matched by the five-digit set
  number in the title: 161 sets, 142 at Euronics and Unieuro, 19 at Euronics, Unieuro and
  Toys Center. Beauty products have no reliable overlap (1 title match).
* Frozen on 2026-11-04 (`bfp hypotheses --freeze`, never overwritten):
  * `config/frozen/lego_sets.csv`: the 161 sets and their product URLs.
  * `config/frozen/tigota_bumped.csv`: 313 Tigotà products, 141 bumped and 172 control (see H3).
* Baseline facts so far (5 Oct to 4 Nov):
  * LEGO, Euronics vs Unieuro: prices equal on 70.5 % of set-days, median spread 0.3 %.
    Only 0.4 % of the equal set-days are below the recommended price shown by Unieuro: the
    chains sit together on LEGO's recommended price.
  * LEGO, cuts of 3 % or more by one chain: 35, none followed by the other chain within two slots.
  * Tigotà: on 31 Oct and 1 Nov the share of observations showing a discount fell from about
    43 % to 13 %, and 141 of 313 products became more than 5 % dearer than their 12 to 21 Oct median.
  * Share of observations showing a discount: Lookfantastic 60 %, Toys Center 45 %, Tigotà 41 %,
    Unieuro 7 %, Notino 1 %, Euronics 0 %.

## H1. LEGO: does the recommended price stop working as a common anchor?

Outside promotions both chains sit on the same price, LEGO's recommended price. That is a
coordination point rather than price competition. Two competing predictions for the BF
window, on the frozen sets, Euronics vs Unieuro, one median price per chain and Rome day:

* **H1a, independent campaigns**: the share of equal set-days falls by at least 15 percentage
  points against baseline, and the median spread rises above 1 %.
* **H1b, matching**: the share of equal set-days stays within 5 points of baseline, and at least
  half of the equal set-days are below the recommended price (both cut to the same level).

Any other outcome is reported as inconclusive.

## H2. LEGO: do the chains follow each other's cuts during BF?

A **unilateral cut** is a drop of at least 3 % from the previous slot by one chain while the
other chain's price is unchanged. It is **followed** if the other chain moves to the same price
within the next two slots. Reported separately and not counted as unilateral cuts:

* cuts that land on the other chain's current price (the cutting chain is the one matching);
* cuts by both chains in the same slot (to the same price or not);
* cuts with fewer than two slots of data after them.

Verdict on the BF window:

* **Supported** if at least 15 % of unilateral cuts are followed, with at least 20 cuts.
* **Rejected** if fewer than 5 % are followed.
* Fewer than 20 cuts: descriptive only.

## H3. Tigotà: the end of the October promotion inflates BF references

Under art. 17-bis the reference for a reduction is the lowest price of the previous 30 days.
For a reduction starting between 20 and 29 Nov, that window still contains the October
promotion prices (up to 30 Oct). Products whose price rose on 31 Oct and 1 Nov
(**bumped**: median price on 2 to 4 Nov more than 5 % above the 12 to 21 Oct median) are the
test group. The remaining Tigotà products are the control group. Both groups are frozen.

On Tigotà episodes starting 20 to 29 Nov:

* **Supported** if the median `discount_gap` (claimed minus honest discount) on bumped products
  is at least 5 percentage points, and at least twice the control median (or the control median
  is 0 or below).
* **Rejected (compliant)** if the median gap on bumped products is below 1 point.
* Fewer than 10 bumped episodes: descriptive only.

## H4. Labels that are always on show the smallest real discounts

If a product is labelled "discounted" most of the time, the label tells the buyer little. The
test is run within each shop at product level, which gives far more units than six shops:

* Products labelled as discounted on at least half of their baseline days have a lower median
  honest discount (`1 - price / true_low30`) in BF episodes than products labelled on fewer
  than 10 % of baseline days.
* A shop is eligible if both groups have at least 20 BF episodes there.
* **Supported** if this holds in at least 3 eligible shops. **Rejected** if it holds in at most 1.
  Fewer than 3 eligible shops: descriptive only.

## H5. Shops that publish their own 30-day low are flagged less often

Notino, Tigotà and Toys Center show a "30-day lowest price" on the page. Unieuro states that its
strikethrough price is the 30-day lowest. Lookfantastic and Euronics show neither. A published
low is only worth something because it can be checked, and this tracker checks it.

* **Supported** if the share of BF episodes flagged `reference_above_low30` is lower in the
  publishing group than in the non-publishing group by at least 5 percentage points.
* **Rejected** if it is not lower at all.
* Reported alongside: the rate of `displayed_low30_above_true` (the published low is higher
  than a price we observed) for each publishing shop.
* Secondary hypothesis. Euronics shows no previous price at all so far, so the non-publishing
  group may in practice be Lookfantastic alone. If fewer than 2 non-publishing shops have
  20 or more BF episodes, H5 is descriptive only.

## Reporting rules

* Headline numbers are shares and medians per shop and per category. Single examples never
  carry a headline: the largest gap among thousands of noisy measurements overstates the
  typical case.
* Examples are picked by a fixed rule: the three largest `discount_gap` values per shop
  among episodes with at least 28 history days, each with its URL and price history chart.
* Flags are conservative: with two samples a day, `true_low30` is an upper bound of the real
  minimum.
* Results that reject a hypothesis are published with the same prominence as results that
  support it.
