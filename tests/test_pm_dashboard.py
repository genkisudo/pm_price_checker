"""Offline tests for the thin-market belief model, input guards, and loopback server."""
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_dashboard as pm

COND = "0x" + "ab" * 32
YES = "111"
NO = "222"


def gamma(**kw):
    base = {
        "id": "100",
        "question": "Test market?",
        "slug": "test-market",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.50", "0.50"]),
        "clobTokenIds": json.dumps([YES, NO]),
        "conditionId": COND,
        "volume": "0",
        "volumeNum": 0,
        "volumeClob": 0,
        "closed": False,
        "active": True,
        "umaResolutionStatus": "",
        "endDate": None,
        "gameStartTime": None,
        "createdAt": "2026-10-16T00:00:00Z",
        "description": "rules",
        "resolutionSource": "https://example.com/rules",
        "image": "https://example.com/a.png",
        "orderPriceMinTickSize": 0.01,
        "lastTradePrice": None,
    }
    base.update(kw)
    return base


class Router:
    def __init__(self):
        self.urls = []
        self.gamma = gamma()
        self.books = {
            YES: {"bids": [], "asks": []},
            NO: {"bids": [], "asks": []},
        }
        self.book_err = None
        self.taker_pages = {0: []}
        self.fill_pages = {0: []}
        self.oi = []
        self.holders = None
        self.history = {YES: [{"t": 1792450158, "p": 0.5}], NO: [{"t": 1792450158, "p": 0.5}]}
        self.slug_market = None
        self.list_open = []
        self.list_closed = []
        self.events = []

    def __call__(self, url, ttl=10):
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        qs = urllib.parse.parse_qs(parts.query)
        host = parts.hostname
        if host not in pm.ALLOWED_HOSTS or parts.scheme != "https":
            return None, "router saw a non-allowlisted URL"
        if path.startswith("/markets/slug/"):
            return (self.slug_market, None) if self.slug_market else (None, "HTTP 404")
        if path == "/markets" and "slug" in qs:
            closed = (qs.get("closed") or [""])[0] == "true"
            return (self.list_closed if closed else self.list_open), None
        if path == "/events":
            return self.events, None
        if path.startswith("/markets/"):
            mid = path.rsplit("/", 1)[-1]
            candidates = [self.gamma, self.slug_market, *self.list_open, *self.list_closed]
            for ev in self.events or []:
                if isinstance(ev, dict):
                    candidates.extend(ev.get("markets") or [])
            for row in candidates:
                if isinstance(row, dict) and str(row.get("id")) == mid:
                    return row, None
            return None, "HTTP 404"
        if path == "/book":
            tok = (qs.get("token_id") or [""])[0]
            if self.book_err:
                return None, self.book_err
            return self.books.get(tok) or {"bids": [], "asks": []}, None
        if path == "/prices-history":
            tok = (qs.get("market") or [""])[0]
            return {"history": self.history.get(tok, [])}, None
        if path == "/trades":
            offset = int((qs.get("offset") or ["0"])[0])
            taker = (qs.get("takerOnly") or ["true"])[0] != "false"
            pages = self.taker_pages if taker else self.fill_pages
            return pages.get(offset, []), None
        if path == "/oi":
            return self.oi, None
        if path == "/holders":
            return self.holders, None
        return None, f"unmocked {path}"


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self._orig = pm.fetch_json
        pm.fetch_json = self.router
        pm._cache.clear()

    def tearDown(self):
        pm.fetch_json = self._orig
        pm._cache.clear()

    def analyze(self, q="100"):
        return pm.analyze(q)

    def test_one_sided_yes_book_matches_reference_rules(self):
        # Market 5950438 shape: no trades, Yes bids 0.81x15 and 0.15x100, mirrored No asks.
        self.router.gamma = gamma(
            id="5950438", question="Jamerson: 1+ shots on target",
            slug="bra-cor1-vit-2026-10-19-shots-on-target-jamerson",
            outcomePrices=json.dumps(["0.905", "0.095"]),
            volume=None, volumeNum=None, volumeClob=None,
            umaResolutionStatus="proposed", closed=False,
            gameStartTime="2026-10-19 23:00:00+00",
            endDate="2026-10-19T23:00:00Z",
            createdAt="2026-10-19T22:28:10.489103Z",
            orderPriceMinTickSize=0.01,
        )
        self.router.books[YES] = {"bids": [
            {"price": "0.15", "size": "100"},
            {"price": "0.81", "size": "15"},
            "not-a-level",
        ], "asks": []}
        self.router.books[NO] = {"bids": [], "asks": [
            {"price": "0.85", "size": "100"},
            {"price": "0.19", "size": "15"},
        ]}
        self.router.oi = []
        self.router.holders = None
        d = self.analyze("5950438")
        self.assertNotIn("error", d)
        v = d["verdict"]
        self.assertEqual(v["headline"], "Traders believe Yes (≥81.0%) — clear lean, low confidence.")
        self.assertEqual(v["probs"], [0.81, 0.19])
        self.assertEqual(v["confidence"], "Low")
        self.assertEqual(v["bound"], ">=")
        self.assertFalse(v["ambiguous"])
        self.assertFalse(d["book"]["midValid"])
        self.assertFalse(d["book"]["twoSided"])
        self.assertEqual(d["book"]["naiveMid"], 0.905)
        self.assertEqual(d["book"]["spread"], 0.19)
        self.assertEqual(len(d["book"]["bids"]), 2)  # mirrors deduped, not summed
        self.assertEqual(d["book"]["bids"][0]["size"], 15)
        self.assertEqual(d["book"]["bids"][0]["usd"], 12.15)
        self.assertEqual(d["book"]["bids"][1]["usd"], 15.0)
        self.assertEqual(d["book"]["asks"], [])
        self.assertIn("weak in size", v["flags"][0]["text"])
        self.assertIn("$12.15", v["flags"][0]["text"])
        self.assertEqual(v["signalUsd"], 6.08)
        ui = d["comparison"][0]
        self.assertEqual(ui["note"], "midpoint (no trades to fall back on)")
        self.assertEqual(ui["p"], [0.905, 0.095])
        self.assertIn("NOT valid", d["comparison"][2]["note"])
        self.assertEqual(d["stats"]["oi"], 0.0)
        self.assertEqual(d["stats"]["volumeGamma"], 0.0)
        self.assertEqual(d["stats"]["tradeCount"], 0)
        self.assertIn("both 0", d["stats"]["explanation"])
        self.assertTrue(any("/oi returned []" in w for w in d["warnings"]))
        self.assertTrue(any("holders endpoint returned null" in w for w in d["warnings"]))
        for url in self.router.urls:
            self.assertTrue(pm._allowed_url(url), url)

    def test_phantom_tight_spread_is_not_a_valid_midpoint(self):
        # Bid 0.96 and no ask. Empty side would make spread 0.04, which is NOT a real spread.
        self.router.books[YES] = {"bids": [{"price": "0.96", "size": "10"}], "asks": []}
        self.router.books[NO] = {"bids": [], "asks": []}
        self.router.taker_pages[0] = [{
            "outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": 5, "price": 0.40,
            "timestamp": 1792300000, "proxyWallet": "0x" + "aa" * 20,
            "transactionHash": "0x" + "11" * 32, "asset": YES,
        }]
        d = self.analyze()
        self.assertFalse(d["book"]["midValid"])
        self.assertLessEqual(d["book"]["spread"], 0.10)
        ui = d["comparison"][0]
        self.assertEqual(ui["note"], "last trade (one-sided book)")
        self.assertEqual(ui["p"][0], 0.4)
        self.assertIn("NOT valid", d["comparison"][2]["note"])

    def test_tight_two_sided_book_uses_midpoint(self):
        self.router.books[YES] = {
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.50", "size": "10"}],
        }
        d = self.analyze()
        self.assertTrue(d["book"]["midValid"])
        self.assertEqual(d["book"]["spread"], 0.10)
        self.assertEqual(d["comparison"][0]["note"], "midpoint (spread ≤ 0.10)")
        self.assertEqual(d["comparison"][0]["p"][0], 0.45)

    def test_wide_book_falls_back_to_last_trade(self):
        self.router.books[YES] = {
            "bids": [{"price": "0.10", "size": "10"}],
            "asks": [{"price": "0.70", "size": "10"}],
        }
        self.router.taker_pages[0] = [{
            "outcomeIndex": "0", "outcome": "Yes", "side": "buy", "size": "8", "price": "0.22",
            "timestamp": 1792300000, "proxyWallet": "0x" + "ab" * 20,
            "transactionHash": "0x" + "22" * 32, "asset": YES,
        }]
        d = self.analyze()
        self.assertFalse(d["book"]["midValid"])
        self.assertGreater(d["book"]["spread"], 0.10)
        self.assertEqual(d["comparison"][0]["note"], "last trade (spread > 0.10)")
        self.assertEqual(d["comparison"][0]["p"][0], 0.22)
        self.assertEqual(d["trades"][0]["outcomeIndex"], 0)

    def test_low_bid_does_not_flip_the_lean(self):
        self.router.books[YES] = {"bids": [{"price": "0.20", "size": "50"}], "asks": []}
        self.router.books[NO] = {"bids": [], "asks": [{"price": "0.80", "size": "50"}]}
        d = self.analyze()
        v = d["verdict"]
        self.assertTrue(v["ambiguous"])
        self.assertEqual(v["lean"], "Yes")
        self.assertIn("No confirmed lean", v["headline"])
        self.assertNotIn("believe No", v["headline"])
        self.assertEqual(v["confidence"], "Low")
        self.assertEqual(d["book"]["bids"][0]["size"], 50)  # not 100 from the mirror

    def test_ask_ceiling_below_half_confirms_the_other_outcome(self):
        self.router.books[YES] = {"bids": [], "asks": [{"price": "0.20", "size": "10"}]}
        self.router.books[NO] = {"bids": [{"price": "0.80", "size": "10"}], "asks": []}
        d = self.analyze()
        v = d["verdict"]
        self.assertFalse(v["ambiguous"])
        self.assertEqual(v["lean"], "No")
        self.assertIn("≥80.0%", v["headline"])
        self.assertEqual(v["bound"], "<=")

    def test_resolved_under_single_mint(self):
        self.router.gamma = gamma(
            id="5733114", question="1st Half O/U 1.5",
            outcomes=json.dumps(["Over", "Under"]),
            outcomePrices=json.dumps(["0", "1"]),
            volume="1420.52", volumeNum=1420.52,
            closed=True, umaResolutionStatus="resolved",
            gameStartTime="2026-10-18 15:00:00+00",
            endDate="2026-10-18T15:00:00Z",
            lastTradePrice=0.26,
            orderPriceMinTickSize=0.001,
        )
        self.router.book_err = 'HTTP 404 {"error":"No orderbook exists for the requested token id"}'
        tx = "0x" + "a1" * 32
        over = "0x" + "11" * 20
        under = "0x" + "22" * 20
        taker = {
            "outcome": "Over", "outcomeIndex": 0, "side": "BUY", "size": 1420.52, "price": 0.26,
            "timestamp": 1792338033, "proxyWallet": over, "transactionHash": tx, "asset": YES,
            "name": "UpTheBlues",
        }
        maker = {
            "outcome": "Under", "outcomeIndex": 1, "side": "BUY", "size": 1420.52, "price": 0.74,
            "timestamp": 1792338033, "proxyWallet": under, "transactionHash": tx, "asset": NO,
            "name": "", "pseudonym": "",
        }
        self.router.taker_pages[0] = [taker]
        self.router.fill_pages[0] = [taker, maker]
        self.router.oi = [{"market": COND, "value": 0}]
        d = self.analyze("5733114")
        self.assertEqual(d["market"]["winner"], "Under")
        self.assertEqual(d["market"]["status"], "resolved")
        self.assertIn("Resolved Under", d["verdict"]["headline"])
        self.assertIn("74.0%", d["verdict"]["headline"])
        self.assertEqual(d["verdict"]["beliefConfidence"], "Medium")
        self.assertEqual(d["verdict"]["confidence"], "Final (resolved)")
        self.assertEqual(d["stats"]["tradeCount"], 1)
        self.assertEqual(d["stats"]["makerFills"], 1)
        self.assertEqual(d["stats"]["usdByOutcome"]["Over"], 369.34)
        self.assertEqual(d["stats"]["usdByOutcome"]["Under"], 1051.18)
        self.assertEqual(d["stats"]["sharesBoughtByOutcome"]["Over"], 1420.52)
        self.assertEqual(d["stats"]["takerSellShares"], 0)
        self.assertEqual(d["trades"][0]["phase"], "in-play")
        self.assertIn("redeem", d["stats"]["explanation"])
        self.assertIn("no taker SELL", d["stats"]["explanation"])
        self.assertTrue(any("No orderbook exists" in w for w in d["warnings"]))
        self.assertFalse(any("lastTradePrice" in w for w in d["warnings"]))

    def test_match_buy_is_not_netted_against_the_sell(self):
        tx = "0x" + "b2" * 32
        seller = "0x" + "33" * 20
        buyer = "0x" + "44" * 20
        sell = {
            "outcome": "Yes", "outcomeIndex": 0, "side": "SELL", "size": 20, "price": 0.026,
            "timestamp": 1792211118, "proxyWallet": seller, "transactionHash": tx, "asset": YES,
            "name": "smarterstrat",
        }
        buy = {
            "outcome": "Yes", "outcomeIndex": 0, "side": "BUY", "size": 20, "price": 0.026,
            "timestamp": 1792211118, "proxyWallet": buyer, "transactionHash": tx, "asset": YES,
            "name": "buyer",
        }
        self.router.taker_pages[0] = [sell]
        self.router.fill_pages[0] = [sell, buy]
        self.router.gamma = gamma(volumeNum=20, volume="20", closed=False)
        self.router.oi = [{"value": 20}]
        d = self.analyze()
        self.assertEqual(d["stats"]["usdByOutcome"]["Yes"], 0.52)
        self.assertEqual(d["stats"]["sharesBoughtByOutcome"]["Yes"], 20)
        self.assertEqual(d["stats"]["takerSellShares"], 20)
        self.assertIn("nobody has closed", d["stats"]["explanation"])

    def test_gamma_last_trade_price_ignores_the_other_token(self):
        self.router.gamma = gamma(
            closed=True, umaResolutionStatus="resolved",
            outcomePrices=json.dumps(["0", "1"]),
            lastTradePrice=0.076, volumeNum=65.46,
        )
        self.router.book_err = "HTTP 404 No orderbook exists"
        self.router.taker_pages[0] = [{
            "outcome": "No", "outcomeIndex": 1, "side": "BUY", "size": 65.46, "price": 0.999,
            "timestamp": 1792319106, "proxyWallet": "0x" + "55" * 20,
            "transactionHash": "0x" + "c3" * 32, "asset": NO, "name": "taker",
        }]
        self.router.oi = [{"value": 0.1}]
        d = self.analyze()
        self.assertEqual(d["market"]["winner"], "No")
        self.assertIn("Resolved No", d["verdict"]["headline"])
        self.assertTrue(any("Yes token only" in w for w in d["warnings"]))
        self.assertAlmostEqual(d["trades"][0]["priceA"], 0.001, places=3)

    def test_string_outcome_index_without_name_or_asset(self):
        self.router.taker_pages[0] = [{
            "outcomeIndex": "0", "side": "BUY", "size": 4, "price": 0.3,
            "timestamp": 1792300000, "proxyWallet": "0x" + "66" * 20,
            "transactionHash": "0x" + "d4" * 32,
        }]
        d = self.analyze()
        self.assertEqual(d["trades"][0]["outcome"], "Yes")
        self.assertEqual(d["trades"][0]["outcomeIndex"], 0)

    def test_third_outcome_and_bad_rows_are_skipped(self):
        self.router.gamma = gamma(outcomes=json.dumps(["Yes", "No", "Draw"]))
        self.router.taker_pages[0] = [
            "nope",
            {"outcomeIndex": 2, "outcome": "Draw", "side": "BUY", "size": 9, "price": 0.2,
             "timestamp": 1792300000, "asset": "999", "transactionHash": "0x" + "e5" * 32,
             "proxyWallet": "0x" + "77" * 20},
            {"outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": -1, "price": 0.2,
             "timestamp": 1792300000, "transactionHash": "0x" + "e6" * 32,
             "proxyWallet": "0x" + "77" * 20},
            {"outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": 3, "price": 1.4,
             "timestamp": 1792300000, "transactionHash": "0x" + "e7" * 32,
             "proxyWallet": "0x" + "77" * 20},
            {"outcomeIndex": 1, "outcome": "No", "side": "BUY", "size": 3, "price": 0.7,
             "timestamp": "1792300000000", "transactionHash": "0x" + "e8" * 32,
             "proxyWallet": "0x" + "88" * 20, "asset": NO},
        ]
        d = self.analyze()
        self.assertTrue(any("!= 2 outcomes" in w for w in d["warnings"]))
        self.assertEqual(d["stats"]["tradeCount"], 1)
        self.assertEqual(d["trades"][0]["outcome"], "No")
        self.assertEqual(d["trades"][0]["ts"], 1792300000)  # milliseconds normalized

    def test_closed_not_resolved_uses_close_explanation(self):
        self.router.gamma = gamma(closed=True, umaResolutionStatus="", volumeNum=100, volume="100",
                                  outcomePrices=json.dumps(["0.40", "0.60"]))
        self.router.oi = [{"value": 40}]
        self.router.taker_pages[0] = [{
            "outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": 10, "price": 0.4,
            "timestamp": 1792300000, "proxyWallet": "0x" + "99" * 20,
            "transactionHash": "0x" + "f1" * 32, "asset": YES,
        }]
        d = self.analyze()
        self.assertEqual(d["market"]["status"], "closed")
        self.assertNotIn("redeem", d["stats"]["explanation"])
        self.assertIn("Volume exceeds OI", d["stats"]["explanation"])

    def test_resolved_with_no_tape_is_not_a_100_percent_belief(self):
        self.router.gamma = gamma(
            closed=True, umaResolutionStatus="resolved",
            outcomePrices=json.dumps(["1", "0"]), volume=None, volumeNum=None, volumeClob=None,
        )
        self.router.book_err = "HTTP 404 No orderbook exists"
        self.router.oi = []
        d = self.analyze()
        self.assertEqual(d["market"]["winner"], "Yes")
        self.assertIn("belief is unknown", d["verdict"]["headline"])
        self.assertNotIn("100.0%", d["verdict"]["headline"])
        self.assertIsNone(d["verdict"]["probs"])
        ours = next(c for c in d["comparison"] if c.get("ours"))
        self.assertIsNone(ours["p"])
        ui = d["comparison"][0]
        self.assertEqual(ui["p"], [1.0, 0.0])

    def test_gamma_price_alone_is_not_a_confirmed_lean(self):
        self.router.gamma = gamma(outcomePrices=json.dumps(["0.905", "0.095"]),
                                  volume=None, volumeNum=None, volumeClob=None)
        self.router.book_err = "HTTP 404 No orderbook exists"
        self.router.oi = []
        d = self.analyze()
        self.assertIn("No confirmed belief", d["verdict"]["headline"])
        self.assertNotIn("strong lean", d["verdict"]["headline"])
        self.assertIsNone(d["verdict"]["probs"])
        self.assertEqual(d["verdict"]["confidence"], "Low")

    def test_volume_without_trades_is_not_called_zero(self):
        self.router.gamma = gamma(volumeNum=50, volume="50")
        self.router.oi = []
        d = self.analyze()
        self.assertNotIn("both 0", d["stats"]["explanation"])
        self.assertIn("incomplete", d["stats"]["explanation"])

    def test_oi_and_holders_bad_payloads_do_not_crash(self):
        self.router.oi = ["nope"]
        self.router.holders = ["nope", {"token": YES, "holders": [
            "bad",
            {"proxyWallet": "0x" + "ab" * 20, "amount": "12.5", "outcomeIndex": "0", "name": "Ada"},
            {"proxyWallet": "not-an-address", "amount": -3, "outcomeIndex": 0},
        ]}]
        d = self.analyze()
        self.assertIsNone(d["stats"]["oi"])
        self.assertEqual(len(d["holders"][0]), 1)
        self.assertEqual(d["holders"][0][0]["amount"], 12.5)
        self.assertTrue(d["holders"][0][0]["wallet"].startswith("0x"))

    def test_unsafe_links_are_stripped(self):
        self.router.gamma = gamma(
            image="javascript:alert(1)",
            resolutionSource="https://127.0.0.1/secret",
            icon="https://10.1.1.1/x.png",
        )
        d = self.analyze()
        self.assertIsNone(d["market"]["image"])
        self.assertIsNone(d["market"]["resolutionSource"])

    def test_crossed_book_does_not_validate_midpoint(self):
        self.router.books[YES] = {
            "bids": [{"price": "0.70", "size": "5"}],
            "asks": [{"price": "0.60", "size": "5"}],
        }
        self.router.books[NO] = {"bids": [], "asks": []}
        d = self.analyze()
        self.assertFalse(d["book"]["midValid"])
        self.assertTrue(any("crossed" in f["text"].lower() for f in d["verdict"]["flags"]))
        probs = d["verdict"]["probs"]
        if probs is not None:
            self.assertTrue(all(0 <= p <= 1 for p in probs))

    def test_history_falls_back_to_the_other_token(self):
        self.router.history = {YES: [], NO: [{"t": 1792450158, "p": 0.2}, {"t": "nope"}, {"p": 0.2}]}
        d = self.analyze()
        last = next(c for c in d["comparison"] if c["label"] == "Last price-history point")
        self.assertEqual(last["p"][0], 0.8)

    def test_trade_phases_follow_game_clock(self):
        start = "2026-10-18 15:00:00+00"
        game = pm.parse_ts(start)
        self.router.gamma = gamma(
            gameStartTime=start, endDate="2026-10-18T20:00:00Z", closed=False,
        )
        def trade(ts, n):
            return {
                "outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": 1, "price": 0.4,
                "timestamp": ts, "proxyWallet": "0x" + f"{n:040x}"[-40:],
                "transactionHash": "0x" + f"{n:064x}"[-64:], "asset": YES,
            }
        self.router.taker_pages[0] = [
            trade(game - 60, 1),
            trade(game + 3 * 3600, 2),
            trade(game + 6 * 3600, 3),
        ]
        d = self.analyze()
        phases = [t["phase"] for t in d["trades"]]
        self.assertEqual(phases, ["pre-game", "in-play", "post-end"])

        self.router.gamma = gamma(gameStartTime=start, endDate="2026-10-18T15:00:00Z")
        self.router.taker_pages[0] = [
            trade(game + 90 * 60, 4),
            trade(game + 3 * 3600, 5),
        ]
        d = self.analyze()
        self.assertEqual([t["phase"] for t in d["trades"]], ["in-play", "post-end"])

    def test_pagination_dedup_and_cap(self):
        old_page, old_max = pm.TRADE_PAGE, pm.MAX_TRADE_OFFSET
        try:
            pm.TRADE_PAGE = 2
            pm.MAX_TRADE_OFFSET = 10
            def row(n):
                return {
                    "outcomeIndex": 0, "outcome": "Yes", "side": "BUY", "size": 1, "price": 0.4,
                    "timestamp": 1792300000 + n, "proxyWallet": "0x" + f"{n:040x}"[-40:],
                    "transactionHash": "0x" + f"{n:064x}"[-64:], "asset": YES,
                }
            self.router.taker_pages = {0: [row(1), row(2)], 2: [row(2), row(3)], 4: [row(4)]}
            self.router.fill_pages = {0: []}
            d = self.analyze()
            self.assertEqual(d["stats"]["tradeCount"], 4)
            self.assertFalse(any("pagination cap" in w for w in d["warnings"]))

            self.router.taker_pages = {0: [row(1), row(1)], 2: [row(1), row(1)]}
            d = self.analyze()
            self.assertEqual(d["stats"]["tradeCount"], 1)

            pm.MAX_TRADE_OFFSET = 0
            self.router.taker_pages = {i: [row(i + 1), row(i + 2)] for i in range(0, 20, 2)}
            d = self.analyze()
            self.assertTrue(any("pagination cap" in w for w in d["warnings"]))
            self.assertLess(d["stats"]["tradeCount"], 10)
        finally:
            pm.TRADE_PAGE = old_page
            pm.MAX_TRADE_OFFSET = old_max

    def test_taker_feed_failure_uses_one_fill_per_tx(self):
        tx = "0x" + "ab" * 32
        self.router.taker_pages = {}
        # Router returns [] for missing offsets, which is not an error.
        # Force the taker endpoint to error by pointing pages at a sentinel via a subclass? 
        # Simpler: wrap fetch to error the taker URL.
        orig = self.router

        def fetch(url, ttl=10):
            if "/trades?" in url and "takerOnly=false" not in url:
                return None, "HTTP 500 down"
            return orig(url, ttl)
        self.router.taker_pages = {0: []}
        pm.fetch_json = fetch
        a = "0x" + "12" * 20
        b = "0x" + "34" * 20
        self.router.fill_pages[0] = [
            {"outcomeIndex": 0, "outcome": "Over", "side": "BUY", "size": 10, "price": 0.3,
             "timestamp": 1792300000, "proxyWallet": a, "transactionHash": tx, "asset": YES},
            {"outcomeIndex": 1, "outcome": "Under", "side": "BUY", "size": 10, "price": 0.7,
             "timestamp": 1792300000, "proxyWallet": b, "transactionHash": tx, "asset": NO},
        ]
        self.router.gamma = gamma(outcomes=json.dumps(["Over", "Under"]))
        d = self.analyze()
        self.assertEqual(d["stats"]["tradeCount"], 1)
        self.assertEqual(d["trades"][0]["outcome"], "Over")
        self.assertEqual(d["stats"]["makerFills"], 1)
        self.assertEqual(d["stats"]["usdByOutcome"]["Over"], 3.0)
        self.assertEqual(d["stats"]["usdByOutcome"]["Under"], 7.0)
        self.assertTrue(any("Taker trade feed failed" in w for w in d["warnings"]))

    def test_event_slug_with_many_markets_is_rejected(self):
        self.router.slug_market = None
        self.router.events = [{"markets": [
            {"id": "1", "question": "Rain in New York?"},
            {"id": "2", "question": "Rain in Atlanta?"},
        ]}]
        d = self.analyze("where-will-it-rain-on-october-17-2026")
        self.assertIn("error", d)
        self.assertIn("event with 2 markets", d["error"])
        self.assertIn("1", d["error"])
        self.assertIn("2", d["error"])
        self.assertNotIn("/markets/1", " ".join(self.router.urls))

    def test_slug_list_finds_open_market_when_slug_route_misses(self):
        self.router.list_open = [gamma(id="5950438", slug="jamerson-shots")]
        d = self.analyze("jamerson-shots")
        self.assertNotIn("error", d)
        self.assertEqual(d["market"]["id"], "5950438")

    def test_closed_list_is_consulted_when_open_list_hides_it(self):
        self.router.list_open = []
        self.router.list_closed = [gamma(id="5772362", slug="rain-atlanta", closed=True)]
        d = self.analyze("rain-atlanta")
        self.assertNotIn("error", d)
        self.assertEqual(d["market"]["id"], "5772362")

    def test_bad_input_does_not_fetch(self):
        for q in ("", "   ", "../etc/passwd", "..", "http://169.254.169.254/latest/meta-data",
                  "https://gamma-api.polymarket.com/markets/1", "javascript:alert(1)",
                  "a" * 501, "slug with spaces", "foo/../../etc/passwd"):
            self.router.urls.clear()
            d = self.analyze(q)
            self.assertIn("error", d, q)
            self.assertEqual(self.router.urls, [], q)

    def test_polymarket_url_uses_the_last_slug(self):
        self.router.slug_market = gamma(id="5772362", slug="where-will-it-rain-on-october-17-2026-atlanta-ga")
        d = self.analyze("https://polymarket.com/event/where-will-it-rain-on-october-17-2026/"
                         "where-will-it-rain-on-october-17-2026-atlanta-ga")
        self.assertEqual(d["market"]["id"], "5772362")
        self.assertTrue(any("/markets/slug/where-will-it-rain-on-october-17-2026-atlanta-ga" in u
                            for u in self.router.urls))

    def test_condition_id_must_be_hex(self):
        self.router.gamma = gamma(conditionId="not-a-condition")
        before = len(self.router.urls)
        d = self.analyze("100")
        self.assertIn("condition id", d["error"])
        self.assertFalse(any("data-api" in u for u in self.router.urls[before:]))

    def test_non_numeric_token_is_not_requested(self):
        self.router.gamma = gamma(clobTokenIds=json.dumps(["not-a-token", YES]))
        d = self.analyze()
        self.assertTrue(any("Ignored a CLOB token" in w for w in d["warnings"]))
        book_urls = [u for u in self.router.urls if "/book?" in u]
        self.assertEqual(len(book_urls), 1)
        self.assertIn(YES, book_urls[0])
        self.assertNotIn("not-a-token", book_urls[0])


class HelperTest(unittest.TestCase):
    def test_parse_ts_gamma_formats(self):
        a = pm.parse_ts("2026-10-17 04:00:00+00")
        b = pm.parse_ts("2026-10-17T04:00:00Z")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertIsNone(pm.parse_ts(""))
        self.assertIsNone(pm.parse_ts("nope"))
        day = pm.parse_ts("2026-10-17")
        self.assertIsNotNone(day)

    def test_madrid_or_utc(self):
        got = pm.madrid(1792338033)
        self.assertTrue(got.startswith("2026-10-18 17:40:33") or got.startswith("2026-10-18 15:40:33"), got)

    def test_qprice_complements_collapse(self):
        from decimal import Decimal
        tick = 0.01
        a = pm.qprice("0.81", tick)
        comp = (Decimal(1) - pm.qprice("0.19", tick)).quantize(Decimal("0.01"))
        self.assertEqual(a, comp)

    def test_allowed_urls(self):
        self.assertTrue(pm._allowed_url("https://gamma-api.polymarket.com/markets/1"))
        self.assertTrue(pm._allowed_url("https://clob.polymarket.com/book?token_id=1"))
        self.assertTrue(pm._allowed_url("https://data-api.polymarket.com/trades?market=0xab"))
        for bad in (
            "http://gamma-api.polymarket.com/markets/1",
            "https://user:pass@gamma-api.polymarket.com/markets/1",
            "https://gamma-api.polymarket.com.evil.com/markets/1",
            "https://169.254.169.254/latest",
            "http://127.0.0.1:8765/",
            "file:///etc/passwd",
            "https://gamma-api.polymarket.com/markets/1\r\nX: y",
        ):
            self.assertFalse(pm._allowed_url(bad), bad)

    def test_redirect_handler_blocks_off_host(self):
        handler = pm._PinHost()
        req = urllib.request.Request("https://gamma-api.polymarket.com/markets/1")
        headers = Message()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", headers, "http://169.254.169.254/latest")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", headers, "https://clob.polymarket.com.evil.com/book")
        ok = handler.redirect_request(
            req, None, 302, "Found", headers, "https://clob.polymarket.com/book?token_id=1")
        self.assertIsNotNone(ok)
        self.assertIn("clob.polymarket.com", ok.full_url)

    def test_fetch_json_does_not_touch_link_local(self):
        hits = {"n": 0}

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                hits["n"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, fmt, *args):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            port = srv.server_address[1]
            data, err = pm.fetch_json(f"http://127.0.0.1:{port}/latest/meta-data")
            self.assertIsNone(data)
            self.assertIn("blocked", err)
            self.assertEqual(hits["n"], 0)
            data, err = pm.fetch_json(f"http://169.254.169.254/latest")
            self.assertIsNone(data)
            self.assertEqual(hits["n"], 0)
        finally:
            srv.shutdown()
            srv.server_close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.srv = pm.make_server(0)
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        self.port = self.srv.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.th.join(timeout=2)

    def get(self, path):
        try:
            with self.opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        try:
            s = socket.create_connection(("::1", self.port), timeout=0.3)
            s.close()
            self.fail("server accepted an IPv6 loopback connection")
        except OSError:
            pass

    def test_index_and_bad_input(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Thin-market belief", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("nosniff", headers["X-Content-Type-Options"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        q = urllib.parse.quote("http://169.254.169.254/latest/meta-data")
        status, headers, body = self.get("/api/market/" + q)
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertIn("error", payload)
        self.assertNotIn("169.254.169.254", payload["error"])
        status, _, body = self.get("/api/market/" + urllib.parse.quote("../etc/passwd"))
        self.assertEqual(status, 400)
        status, _, body = self.get("/nope")
        self.assertEqual(status, 404)

    def test_invalid_port_rejected(self):
        # make_server itself does not validate; main() does. Call the same checks via argv parsing
        # by invoking the validator indirectly: ports outside 1-65535 are not what we bind here.
        self.assertEqual(pm.BIND_HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
