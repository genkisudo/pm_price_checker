# pm_price_checker

Polymarket thin-market belief dashboard. Paste a Gamma market id, a market slug, or a polymarket.com URL and see what traders actually believe — especially on low-OI / low-volume markets where the displayed midpoint is misleading.

Single-file, standard-library Python 3. No pip installs.

## Run

```bash
python3 pm_dashboard.py            # http://127.0.0.1:8765
python3 pm_dashboard.py --no-browser --port=8765
```

Open `http://127.0.0.1:8765/?id=5733114` to deep-link. The JSON analysis is `GET /api/market/<id-or-slug-or-url>`.

The server binds to `127.0.0.1` only. Chart.js loads from jsDelivr, so the chart needs internet; everything else is served locally. All Polymarket calls are made server-side.

## What you can paste

- A numeric Gamma id, such as `5950438`
- A market slug, such as `where-will-it-rain-on-october-17-2026-atlanta-ga`
- A polymarket.com market URL (the last path segment is the slug)

Anything else is rejected. The app does not fetch the URL you paste. Upstream requests go only to `gamma-api.polymarket.com`, `clob.polymarket.com`, and `data-api.polymarket.com`, and redirects off those hosts are refused. An event URL that contains several markets returns those market ids instead of silently picking the first one.

## Data sources (all free, no key)

- Gamma `/markets/{id}` or `/markets/slug/{slug}`: metadata, outcomePrices, volume, UMA status. The list endpoint `/markets?slug=` returns open markets only; `closed=true` returns closed markets only. `/markets/slug/{slug}` works for both.
- CLOB `/book?token_id=` for both outcome tokens
- Data API v2 `/v2/prices-history?token_id=` (cursor pages; `bucket_seconds` 60, then 300, then 1800, then `interval=max`)
- Data API `/trades?market=` with `takerOnly=true` and `takerOnly=false` (paginated), `/oi`, `/holders`

## Belief rules

1. The two token books mirror each other (a Yes bid at p is a No ask at 1−p). They are merged into one book in outcome-0 terms and de-duplicated on the market's price tick. The same resting order is not counted twice.
2. The midpoint counts only when both sides have orders and the spread is ≤ 0.10. Otherwise the page uses the last trade, and it says so. Filling an empty side in as 0 or 1 does not make the midpoint valid, even when `1 − bestBid` happens to be ≤ 0.10.
3. A one-sided book is flagged ("Nobody bids Yes at any price, nobody sells No: strong signal for No") together with the money at stake. Resting orders set bounds: the best bid is a floor and the best ask is a ceiling. A floor below 50% or a ceiling above 50% does not get reported as a lean toward the other outcome.
4. Trade estimate: price weighted by USD × phase (pre-game ×1, in-play ×2, post-end ×3) × recency (6 h half-life). Blended with resting money near the top of the book (weight 0.5), then clamped into the book's bounds.
5. Zero trades: the verdict uses resting orders only, and confidence is always Low. If the book is gone as well, Gamma's price (including a 0/1 settlement) is shown for reference and is not reported as the traders' belief.
6. Confidence comes from the weighted signal in USD and the size of the lean (Low / Medium / High). Resolved markets show "Final" plus the pre-resolution belief.
7. Displayed vs real: Polymarket UI price, Gamma outcomePrices, naive midpoint, last pre-resolution midpoint, on-chain settlement (when the series has one), last trade, VWAP, and our estimate.

## OI / volume

- OI == volume: nobody has closed or sold.
- Volume > OI: positions were closed.
- After resolution, OI collapses as winners redeem, so the comparison stops being meaningful there. A market that is merely closed (not yet resolved) still uses the OI vs volume comparison.
- Gamma volume counts shares. The page also shows USD spent per outcome: size × price on buy fills, taker and maker, each side once. A matched sell is not subtracted from that spend (doing so cancels the buy and hides it). Taker sells are counted separately.

## Trade phases

- pre-game: before `gameStartTime`
- in-play: from `gameStartTime` to `endDate`, or to start + 2 h when `endDate` == start (typical for sports)
- post-end: after that

## API quirks

- `/book` returns 404 "No orderbook exists" once a market closes.
- `/oi` returns `[]` when no position was ever minted. `/holders` can return `null`. Gamma `volume` is `null` when there are no trades.
- `/trades` is taker-only by default. The counterparty (for example the Under buyer on a mint) only appears with `takerOnly=false`.
- Gamma `lastTradePrice` tracks only the outcome-0 token. A No trade at 0.999 can leave `lastTradePrice` sitting on an older Yes print.
- Data API v2 `/v2/prices-history` replaces CLOB `/prices-history` (shut down 30 Oct 2026). `token_id` is required. Exactly one window form per request: `start`/`end` (15 days max), `interval` (`max` is 12-hour buckets), or `as_of`. `bucket_seconds` is 60, 300, 1800, 10800, or 43200. Pages use `pagination.next_cursor`; `offset` is rejected. The old `{history:[{t,p}]}` shape is now `{data:[{timestamp,price,resolution_seconds}]}`.
- 1-minute history is kept at least 7 days, 5-minute 60, and 30-minute 90. Asking for a grain whose window has expired returns an empty page, not a coarser series. The page requests 60-second buckets for a recent or short market, and falls back to 300, then 1800, then `interval=max`. The note shows the grain actually served (`resolution_seconds`).
- A resolved series ends with a real tick and then one on-chain settlement point (`resolution_seconds` 0, one second later, last page only). That payout is shown as on-chain settlement and is not the last pre-resolution midpoint.
- A 429 or 503 on price history is retried once, after at most 2 seconds of `Retry-After`, and then warned.
- Gamma `gameStartTime` uses `2026-10-17 04:00:00+00`, not ISO.
- Price-history moves with no trade dots are orders being placed or cancelled, not executions.

## Tests

```bash
python3 -m unittest tests.test_pm_dashboard
```

The tests mock the Polymarket responses. They do not call the network.
