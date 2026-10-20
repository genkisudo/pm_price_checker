#!/usr/bin/env python3
"""
Polymarket thin-market belief dashboard.

Paste a Gamma market id (or a polymarket.com URL / slug) and get a dashboard that
answers: what do traders actually believe the outcome is? Built for low-OI /
low-volume markets where Polymarket's displayed midpoint is misleading.

Standard library only. Run: python3 pm_dashboard.py -> http://127.0.0.1:8765
JSON analysis: GET /api/market/<id>

The server binds to loopback only. It never fetches a URL the user pasted.
Upstream calls go only to the public Polymarket Gamma, CLOB, and data APIs,
and redirects off those hosts are refused.
"""
import ipaddress
import json
import math
import re
import sys
import time
import threading
import traceback
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
    MADRID = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover
    MADRID = timezone.utc

PORT = 8765
BIND_HOST = "127.0.0.1"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
ALLOWED_HOSTS = frozenset(urllib.parse.urlsplit(u).hostname for u in (GAMMA, CLOB, DATA))
UA = {"User-Agent": "pm-dashboard/1.0 (+stdlib)", "Accept": "application/json"}

# ----- tunables for the belief model ----------------------------------------
TIGHT_SPREAD = 0.10        # Polymarket's own rule: midpoint only if spread <= 0.10
NEAR_TOP = 0.10            # levels within this distance of best price count as "top of book"
HALF_LIFE_H = 6.0          # recency half-life for trade weights
PHASE_MULT = {"pre-game": 1.0, "in-play": 2.0, "post-end": 3.0, "n/a": 1.0}
BOOK_WEIGHT = 0.5          # resting money counts half as much as executed money
INPLAY_DEFAULT_H = 2.0     # if endDate == gameStart (sports), assume play lasts ~2h
TRADE_PAGE = 500
MAX_TRADE_OFFSET = 3000
MAX_QUERY_LEN = 500

# ----- HTTP helpers -----------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 256


def _allowed_url(url):
    """True only for https URLs on the three Polymarket API hosts."""
    if not isinstance(url, str) or len(url) > 4000:
        return False
    if any(c in url for c in "\r\n\t\\"):
        return False
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        return False
    if parts.username or parts.password:
        return False
    return True


class _PinHost(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when they stay on the allowlisted API hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, "refusing redirect off Polymarket APIs", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PinHost)


def _cache_get(url, now):
    hit = _cache.get(url)
    if not hit:
        return None
    expiry, data, err = hit
    if now >= expiry:
        _cache.pop(url, None)
        return None
    return data, err


def _cache_put(url, data, err, ttl):
    _cache[url] = (time.time() + ttl, data, err)
    while len(_cache) > _CACHE_MAX:
        _cache.pop(next(iter(_cache)))


def fetch_json(url, ttl=10):
    """GET JSON. Returns (data, error_string). Never raises. Never leaves the allowlist."""
    if not _allowed_url(url):
        return None, "blocked non-allowlisted URL"
    now = time.time()
    with _cache_lock:
        hit = _cache_get(url, now)
        if hit is not None:
            return hit
    data, err = None, None
    store_ttl = ttl
    try:
        req = urllib.request.Request(url, headers=UA)
        with _OPENER.open(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        msg = " ".join(f"{e.code} {e.reason or ''} {body}".split())
        err = f"HTTP {msg}".strip()
        # 404s are stable (closed books). Timeouts and 5xx should be retried quickly.
        if e.code not in (400, 404, 410, 422):
            store_ttl = min(ttl, 2)
    except Exception as e:  # network, JSON
        err = f"{type(e).__name__}: {e}"
        store_ttl = min(ttl, 2)
    with _cache_lock:
        _cache_put(url, data, err, store_ttl)
    return data, err


def fnum(x, default=None):
    try:
        if x is None or x == "":
            return default
        if isinstance(x, bool):
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def first_num(*vals):
    for v in vals:
        n = fnum(v)
        if n is not None:
            return n
    return None


def jlist(x):
    """Gamma returns some arrays as JSON strings."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def parse_ts(s):
    """Parse Gamma timestamps ('2026-10-17T04:00:00Z', '2026-10-17 04:00:00+00'). -> epoch seconds."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.search(r"([+-]\d{2})$", s)
    if m:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


def epoch_seconds(x):
    """Accept seconds or milliseconds. Reject nonsense."""
    v = fnum(x)
    if v is None:
        return None
    if v > 1e11:  # milliseconds
        v /= 1000.0
    if v < 0 or v > 1e11:
        return None
    return v


def madrid(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, MADRID).strftime("%Y-%m-%d %H:%M:%S %Z")


def dfloat(d):
    """Decimal -> float that JSON-serializes as the decimal text (not binary dust)."""
    return float(format(d, "f"))


def qprice(x, tick):
    """Quantize a price onto the market tick, or None if it is outside [0, 1]."""
    if x is None or isinstance(x, bool):
        return None
    try:
        d = Decimal(str(x).strip())
        t = Decimal(str(tick))
    except Exception:
        return None
    if not d.is_finite() or not t.is_finite() or t <= 0:
        return None
    n = (d / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    d = (n * t).quantize(t)
    if d < 0 or d > 1:
        return None
    return d


def token_str(t):
    if isinstance(t, bool):
        return None
    if isinstance(t, int) and t >= 0:
        s = str(t)
    elif isinstance(t, str) and re.fullmatch(r"\d{1,80}", t.strip()):
        s = t.strip()
    else:
        return None
    return s or None


def eth_addr(w):
    return w if isinstance(w, str) and re.fullmatch(r"0x[a-fA-F0-9]{40}", w) else ""


def txhash(h):
    return h if isinstance(h, str) and re.fullmatch(r"0x[a-fA-F0-9]{64}", h) else ""


def https_url(url):
    """Public https URL safe to hand to the browser. Drops javascript:, loopback, private IPs."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 500 or any(c in url for c in "\r\n\t\\"):
        return None
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return None
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                           or ip.is_multicast or ip.is_unspecified):
        return None
    return url


def clip_text(s, n=80):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s or ""))
    s = " ".join(s.split())
    return s[:n]


def short_err(e):
    text = " ".join(str(e).split())
    if "No orderbook exists" in text:
        return "HTTP 404 No orderbook exists"
    return text[:180]


def market_id_of(row):
    if not isinstance(row, dict):
        return None
    mid = row.get("id")
    if isinstance(mid, bool):
        return None
    if isinstance(mid, int) and mid >= 0:
        mid = str(mid)
    if isinstance(mid, str) and re.fullmatch(r"\d{1,20}", mid):
        return mid
    return None


_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,199}")


def _slug_ok(slug):
    return bool(slug) and bool(_SLUG_RE.fullmatch(slug)) and ".." not in slug


# ----- user input -------------------------------------------------------------
def parse_user_query(q):
    """Return ('id'|'slug', value) or ('error', message). Never treats input as a URL to fetch."""
    if not isinstance(q, str):
        q = "" if q is None else str(q)
    q = q.strip()
    if not q or len(q) > MAX_QUERY_LEN or re.search(r"[\x00-\x1f\x7f]", q):
        return "error", "Enter a numeric market id, a market slug, or a polymarket.com URL."
    decoded = urllib.parse.unquote(q).replace("\\", "/")
    if any(part in (".", "..") for part in decoded.split("/")):
        return "error", "Enter a numeric market id, a market slug, or a polymarket.com URL."
    if re.fullmatch(r"\d{1,20}", q):
        return "id", q
    if "://" in q or q.startswith("polymarket.com") or q.startswith("www.polymarket.com"):
        raw = q if "://" in q else "https://" + q
        parts = urllib.parse.urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in ("http", "https") or host not in ("polymarket.com", "www.polymarket.com"):
            return "error", "Only polymarket.com URLs are accepted. Paste a market id or slug, not an arbitrary URL."
        if parts.username or parts.password:
            return "error", "That URL is not a Polymarket market link."
        segs = [s for s in parts.path.split("/") if s]
        slug = urllib.parse.unquote(segs[-1]) if segs else ""
        if re.fullmatch(r"\d{1,20}", slug):
            return "id", slug
        if _slug_ok(slug):
            return "slug", slug
        qs = urllib.parse.parse_qs(parts.query)
        for key in ("market", "market_id", "id"):
            for val in qs.get(key) or []:
                if re.fullmatch(r"\d{1,20}", val):
                    return "id", val
        return "error", "Could not find a market slug in that Polymarket URL."
    slug = urllib.parse.unquote(q.split("?")[0].strip().strip("/"))
    if "/" in slug:
        slug = slug.split("/")[-1]
    if re.fullmatch(r"\d{1,20}", slug):
        return "id", slug
    if _slug_ok(slug):
        return "slug", slug
    return "error", "Enter a numeric market id, a market slug, or a polymarket.com URL."


def resolve_market_id(q):
    """Accept numeric id, slug, or polymarket URL. Returns (id, error, http_status)."""
    kind, value = parse_user_query(q)
    if kind == "error":
        return None, value, 400
    if kind == "id":
        return value, None, None
    slug = value
    enc = urllib.parse.quote(slug, safe="")
    data, _err = fetch_json(f"{GAMMA}/markets/slug/{enc}")
    mid = market_id_of(data) if isinstance(data, dict) else None
    if mid:
        return mid, None, None
    # The list endpoint hides the other state: no flag = open only, closed=true = closed only.
    for extra in ("", "&closed=true"):
        data, _err = fetch_json(f"{GAMMA}/markets?slug={enc}{extra}")
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("slug") == slug:
                    mid = market_id_of(row)
                    if mid:
                        return mid, None, None
    data, _err = fetch_json(f"{GAMMA}/events?slug={enc}")
    markets = []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        for row in data[0].get("markets") or []:
            mid = market_id_of(row)
            if mid:
                markets.append((mid, clip_text(row.get("question") or row.get("slug") or mid)))
    if len(markets) == 1:
        return markets[0][0], None, None
    if len(markets) > 1:
        preview = "; ".join(f"{mid} ({qtext})" for mid, qtext in markets[:8])
        more = "" if len(markets) <= 8 else f"; +{len(markets) - 8} more"
        return None, (f"'{slug}' is an event with {len(markets)} markets, not one market. "
                      f"Pass a market id or market slug. Examples: {preview}{more}"), 400
    return None, f"Could not resolve '{slug}' to a market id", 404


def get_trades(cond, taker_only=True):
    """Paginate /trades. Returns (rows, errors, truncated)."""
    out, errs, seen = [], [], set()
    offset = 0
    truncated = False
    limit = TRADE_PAGE
    flag = "true" if taker_only else "false"
    while offset <= MAX_TRADE_OFFSET:
        qs = urllib.parse.urlencode({
            "market": cond, "limit": str(limit), "offset": str(offset), "takerOnly": flag,
        })
        data, err = fetch_json(f"{DATA}/trades?{qs}")
        if err:
            errs.append(err)
            break
        if not isinstance(data, list) or not data:
            break
        added = 0
        for t in data:
            if not isinstance(t, dict):
                continue
            k = (t.get("transactionHash"), str(t.get("asset")), str(t.get("size")),
                 str(t.get("price")), str(t.get("side") or "").upper(),
                 str(t.get("proxyWallet") or "").lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(t)
            added += 1
        if len(data) < limit:
            break
        if added == 0:
            break  # offset ignored or repeating page
        if offset + limit > MAX_TRADE_OFFSET:
            truncated = True
            break
        offset += limit
    return out, errs, truncated


def get_history(token, start_ts, end_ts):
    """Try a fine-grained range query first, fall back to interval=max."""
    if not re.fullmatch(r"\d{1,80}", str(token)):
        return [], "invalid token id"
    tok = urllib.parse.quote(str(token), safe="")

    def history_of(data):
        if not isinstance(data, dict):
            return None
        h = data.get("history")
        return h if isinstance(h, list) else None

    if start_ts and end_ts and end_ts > start_ts:
        span_min = (end_ts - start_ts) / 60
        fid = max(1, int(math.ceil(span_min / 3000)))
        data, err = fetch_json(
            f"{CLOB}/prices-history?market={tok}&startTs={int(start_ts)}&endTs={int(end_ts)}&fidelity={fid}",
            ttl=30)
        h = history_of(data)
        if h:
            return h, None
    data, err = fetch_json(f"{CLOB}/prices-history?market={tok}&interval=max&fidelity=1", ttl=30)
    h = history_of(data)
    return (h or []), err


# ----- analysis ---------------------------------------------------------------
def unify_book(books, outcomes, tick):
    """Merge two token books into ONE book in outcome-0 price terms.

    A bid on outcome1 at q == an ask on outcome0 at 1-q (Polymarket mirrors them),
    so we convert and dedupe by (side, price) keeping the larger size.
    Prices are quantized to the market tick with Decimal so 0.81 and 1-0.19
    collapse to the same level instead of double-counting.
    """
    bids, asks = {}, {}

    def add(d, price, size, src):
        if price is None or size <= 0:
            return
        cur = d.get(price)
        if cur is None or size > cur["size"]:
            d[price] = {"price": price, "size": size, "src": src}

    for idx, b in enumerate(books):
        if not isinstance(b, dict):
            continue
        for side_name, levels in (("bids", b.get("bids")), ("asks", b.get("asks"))):
            if not isinstance(levels, list):
                continue
            for lvl in levels:
                if not isinstance(lvl, dict):
                    continue
                price = qprice(lvl.get("price"), tick)
                size = fnum(lvl.get("size"), 0) or 0
                if price is None:
                    continue
                if side_name == "bids":
                    if idx == 0:
                        add(bids, price, size, f"{outcomes[0]} bid")
                    else:
                        add(asks, (Decimal(1) - price).quantize(Decimal(str(tick))), size, f"{outcomes[1]} bid")
                else:
                    if idx == 0:
                        add(asks, price, size, f"{outcomes[0]} ask")
                    else:
                        add(bids, (Decimal(1) - price).quantize(Decimal(str(tick))), size, f"{outcomes[1]} ask")

    def emit(rows, desc):
        out = []
        for price, row in rows:
            px = dfloat(price)
            usd = dfloat((price * Decimal(str(row["size"]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            out.append({"price": px, "size": round(row["size"], 4), "usd": usd, "src": row["src"]})
        out.sort(key=lambda r: -r["price"] if desc else r["price"])
        return out

    bl = emit(bids.items(), True)
    al = emit(asks.items(), False)
    # Money the buyer of that side commits. Asks are bids for the other outcome,
    # so the collateral is (1 - price) * size, not price * size.
    for row, price in ((row, Decimal(str(row["price"]))) for row in al):
        row["usd"] = dfloat(((Decimal(1) - price) * Decimal(str(row["size"]))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))
    return bl, al


def lean_words(p):
    d = abs(p - 0.5)
    if d >= 0.40:
        return "strong"
    if d >= 0.20:
        return "clear"
    if d >= 0.05:
        return "slight"
    return "toss-up"


def outcome_index(raw, outcome, asset, tokens, outcomes):
    """Coerce outcomeIndex. Gamma/data-api sometimes send strings. Do not guess a side."""
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, str) and raw.strip() in ("0", "1"):
        return int(raw.strip())
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw in (0, 1):
        return int(raw)
    if asset is not None and tokens:
        a = str(asset)
        if a == tokens[0]:
            return 0
        if len(tokens) > 1 and a == tokens[1]:
            return 1
    if outcome == outcomes[0]:
        return 0
    if len(outcomes) > 1 and outcome == outcomes[1]:
        return 1
    return None


def normalize_fill(raw, outcomes, tokens, phase_fn):
    if not isinstance(raw, dict):
        return None
    size = fnum(raw.get("size"))
    price = fnum(raw.get("price"))
    ts = epoch_seconds(raw.get("timestamp"))
    if size is None or price is None or ts is None:
        return None
    if size <= 0 or round(size, 4) <= 0 or not (0.0 <= price <= 1.0):
        return None
    oi = outcome_index(raw.get("outcomeIndex"), raw.get("outcome"), raw.get("asset"), tokens, outcomes)
    if oi not in (0, 1):
        return None
    side = str(raw.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    wallet = raw.get("proxyWallet") or ""
    if not isinstance(wallet, str):
        wallet = ""
    name = raw.get("name") or raw.get("pseudonym") or ""
    if not isinstance(name, str):
        name = ""
    tx = raw.get("transactionHash") or ""
    if not isinstance(tx, str):
        tx = ""
    price_a = price if oi == 0 else 1 - price
    addr = eth_addr(wallet)
    return {
        "ts": ts, "time": madrid(ts), "outcome": outcomes[oi], "outcomeIndex": oi, "side": side,
        "size": round(size, 4), "price": round(price, 6), "usd": round(size * price, 2),
        "priceA": round(price_a, 6),
        "bullishFor": outcomes[oi] if side == "BUY" else outcomes[1 - oi],
        "wallet": addr,
        "walletRaw": wallet.lower(),
        "walletShort": (wallet[:6] + "…" + wallet[-4:]) if len(wallet) > 12 else wallet,
        "name": name[:200], "phase": phase_fn(ts),
        "tx": txhash(tx), "txRaw": tx,
    }


def fill_identity(t):
    base = (t["txRaw"], t["walletRaw"], t["outcomeIndex"], t["size"], t["side"], t["price"])
    if not t["txRaw"]:
        return base + (t["ts"],)
    return base


def public_fill(t, role):
    out = {k: v for k, v in t.items() if k not in ("walletRaw", "txRaw")}
    out["role"] = role
    return out


def history_points(raw):
    pts = []
    for x in raw or []:
        if not isinstance(x, dict):
            continue
        ts = epoch_seconds(x.get("t"))
        p = fnum(x.get("p"))
        if ts is None or p is None or not (0.0 <= p <= 1.0):
            continue
        pts.append({"t": int(ts), "p": p})
    return pts


def analyze(market_id):
    mid, err, status_code = resolve_market_id(market_id)
    if not mid:
        return {"error": err, "status": status_code or 400}
    g, err = fetch_json(f"{GAMMA}/markets/{mid}")
    if not isinstance(g, dict) or not g.get("conditionId"):
        return {"error": f"Gamma market {mid} not found ({err or 'empty response'})", "status": 404}
    cond = str(g.get("conditionId") or "")
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", cond):
        return {"error": "Gamma returned an unexpected condition id", "status": 502}

    warnings = []
    outcomes = [str(x) for x in jlist(g.get("outcomes"))] or ["Yes", "No"]
    if len(outcomes) != 2:
        warnings.append("Market has != 2 outcomes; dashboard assumes binary and uses the first two.")
    outcomes = (outcomes + ["Outcome 2"])[:2]
    A, B = outcomes
    raw_tokens = jlist(g.get("clobTokenIds"))
    tokens, skipped = [], 0
    for t in raw_tokens:
        s = token_str(t)
        if s:
            tokens.append(s)
        else:
            skipped += 1
    tokens = tokens[:2]
    if skipped:
        warnings.append("Ignored a CLOB token id that was not a decimal integer.")
    if not tokens:
        warnings.append("Market has no CLOB token ids; there is no order book to read.")
    gprices = [fnum(x) for x in jlist(g.get("outcomePrices"))][:2]
    now = time.time()
    created = parse_ts(g.get("createdAt")) or parse_ts(g.get("startDate"))
    end_ts = parse_ts(g.get("endDate"))
    game_ts = parse_ts(g.get("gameStartTime"))
    closed = bool(g.get("closed"))
    uma = str(g.get("umaResolutionStatus") or "").lower()
    tick = fnum(g.get("orderPriceMinTickSize"))
    if tick is None or tick <= 0 or tick > 0.1:
        tick = 0.0001

    # ---- parallel fetch ----
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_books = [ex.submit(fetch_json, f"{CLOB}/book?token_id={t}", 5) for t in tokens]
        f_trades = ex.submit(get_trades, cond, True)
        f_fills = ex.submit(get_trades, cond, False)
        enc_cond = urllib.parse.quote(cond, safe="")
        f_oi = ex.submit(fetch_json, f"{DATA}/oi?market={enc_cond}", 15)
        f_hold = ex.submit(fetch_json, f"{DATA}/holders?market={enc_cond}&limit=20", 30)
        hist_start = (created or now - 30 * 86400) - 3600
        f_hist = [ex.submit(get_history, t, hist_start, now) for t in tokens]
        books_raw = [f.result() for f in f_books]
        trades_raw, trade_errs, trade_trunc = f_trades.result()
        fills_raw, fill_errs, fill_trunc = f_fills.result()
        oi_raw, oi_err = f_oi.result()
        hold_raw, hold_err = f_hold.result()
        hists = [f.result() for f in f_hist]

    books, book_errs = [], []
    for data, e in books_raw:
        books.append(data if isinstance(data, dict) else None)
        if e:
            book_errs.append(e)
    book_available = any(b is not None for b in books)
    if tokens and not book_available:
        detail = "; ".join(sorted({short_err(e) for e in book_errs if e}))
        extra = f" ({detail})" if detail else ""
        warnings.append("Order book unavailable" + extra + ". CLOB removes the book once a market closes.")
    warnings += [f"trades: {short_err(e)}" for e in trade_errs]
    warnings += [f"maker fills: {short_err(e)}" for e in fill_errs]
    if trade_trunc or fill_trunc:
        warnings.append("Trade history hit the pagination cap and may be truncated.")

    # ---- status ----
    winner = None
    if closed and len(gprices) == 2 and None not in gprices:
        if max(gprices) >= 0.99:
            winner = outcomes[0] if gprices[0] >= gprices[1] else outcomes[1]
    if uma == "disputed":
        status, status_label = "disputed", "UMA disputed"
    elif uma == "proposed":
        status, status_label = "proposed", "UMA proposed (in review / challenge window)"
    elif uma == "resolved" or (closed and winner and uma in ("resolved", "")):
        status = "resolved"
        status_label = f"Resolved: {winner}" if winner else "Resolved"
    elif closed:
        status, status_label = "closed", "Closed, awaiting resolution"
    else:
        status, status_label = "open", "Open"
    if status in ("open", "proposed") and game_ts and now >= game_ts:
        if end_ts and end_ts > game_ts and now < end_ts:
            status_label += " · in play"

    # ---- phases ----
    play_end = None
    if game_ts:
        play_end = end_ts if (end_ts and end_ts > game_ts + 600) else game_ts + INPLAY_DEFAULT_H * 3600
    elif end_ts:
        play_end = end_ts

    def phase(ts):
        if game_ts and ts < game_ts:
            return "pre-game"
        if play_end and ts >= play_end:
            return "post-end"
        if game_ts:
            return "in-play"
        return "n/a"

    # ---- trades ----
    trades = []
    for t in trades_raw:
        row = normalize_fill(t, outcomes, tokens, phase)
        if row:
            trades.append(row)
    trades.sort(key=lambda x: x["ts"])
    fills = []
    for f in fills_raw or []:
        row = normalize_fill(f, outcomes, tokens, phase)
        if row:
            fills.append(row)
    if not trades and fills:
        by, order = {}, []
        for row in fills:
            k = row["txRaw"] or f"{row['walletRaw']}|{row['ts']}|{row['outcomeIndex']}|{row['size']}|{row['side']}"
            if k not in by:
                by[k] = []
                order.append(k)
            by[k].append(row)
        for k in order:
            rows = by[k]
            chosen = next((r for r in rows if r["outcomeIndex"] == 0), rows[0])
            trades.append(dict(chosen))
        trades.sort(key=lambda x: x["ts"])
        if trade_errs:
            warnings.append("Taker trade feed failed; belief uses one fill per transaction from takerOnly=false.")
        else:
            warnings.append("Taker feed returned no rows; belief uses one fill per transaction from takerOnly=false.")
    for t in trades:
        t["role"] = "taker"
    taker_keys = {fill_identity(t) for t in trades}
    makers = []
    for row in fills:
        if fill_identity(row) in taker_keys:
            continue
        makers.append(row)
    makers.sort(key=lambda x: x["ts"])
    trades_out = [public_fill(t, "taker") for t in trades]
    makers_out = [public_fill(t, "maker") for t in makers]
    all_fills = sorted(trades_out + makers_out, key=lambda x: (x["ts"], x["role"] != "taker"))

    # USD spent is the premium paid on BUY fills, each side once.
    # A match is a BUY and a SELL of the same token: counting both nets to ~0 and hides the spend.
    # A mint is a BUY of each outcome: both premiums are real money out the door.
    usd_rows = all_fills if not fill_errs else trades_out
    usd_by = {A: 0.0, B: 0.0}
    sh_by = {A: 0.0, B: 0.0}
    for t in usd_rows:
        if t["side"] == "BUY":
            usd_by[t["outcome"]] += t["usd"]
            sh_by[t["outcome"]] += t["size"]
    sells_sh = sum(t["size"] for t in trades_out if t["side"] == "SELL")

    # ---- book ----
    bids, asks = unify_book(books, outcomes, tick) if book_available else ([], [])
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    has_level = bool(bids or asks)
    if book_available and has_level:
        eff_bid = Decimal(str(best_bid)) if best_bid is not None else Decimal(0)
        eff_ask = Decimal(str(best_ask)) if best_ask is not None else Decimal(1)
        naive_mid = dfloat(((eff_bid + eff_ask) / 2).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        spread = dfloat((eff_ask - eff_bid).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    else:
        naive_mid = None
        spread = None
    two_sided = best_bid is not None and best_ask is not None
    crossed = two_sided and spread is not None and spread < -1e-9
    mid_valid = two_sided and spread is not None and 0 <= spread <= TIGHT_SPREAD + 1e-9
    top_bid_usd = sum(l["usd"] for l in bids if best_bid is not None and l["price"] >= best_bid - NEAR_TOP - 1e-9)
    top_ask_usd = sum(l["usd"] for l in asks if best_ask is not None and l["price"] <= best_ask + NEAR_TOP + 1e-9)
    flags = []

    def small_note(u):
        return f" (but only ${u:,.2f} at stake, so weak in size)." if u < 100 else f" (${u:,.2f} at stake near top)."
    if book_available:
        if not bids and not asks:
            flags.append({"kind": "empty", "text": "Order book is completely empty: no resting orders on either side."})
        elif not bids:
            flags.append({"kind": "one-sided", "favours": B,
                          "text": f"Nobody bids {A} at any price, nobody sells {B}: strong signal for {B}" + small_note(top_ask_usd)})
        elif not asks:
            flags.append({"kind": "one-sided", "favours": A,
                          "text": f"Nobody sells {A} at any price, nobody bids {B}: strong signal for {A}" + small_note(top_bid_usd)})
        elif spread is not None and spread > TIGHT_SPREAD:
            flags.append({"kind": "wide", "text": f"Spread is {spread:.3f} (> {TIGHT_SPREAD:.2f}): midpoint is not a valid price here."})
        if crossed:
            flags.append({"kind": "wide", "text": f"Book is crossed (best bid {best_bid:.3f} above best ask {best_ask:.3f}); midpoint is not used."})
        if asks and asks[0]["price"] >= 0.99 and bids:
            flags.append({"kind": "junk", "text": f"Only {A} offer is at ≥0.99: effectively nobody sells {A}."})
        if bids and bids[0]["price"] <= 0.01 and asks:
            flags.append({"kind": "junk", "text": f"Only {A} bid is at ≤0.01: effectively nobody bids {A}."})

    last_trade = trades_out[-1] if trades_out else None
    last_A = last_trade["priceA"] if last_trade else None
    spent = sum(t["usd"] for t in trades_out)
    vwap_all = (sum(t["priceA"] * t["usd"] for t in trades_out) / spent) if trades_out and spent > 0 else None

    # Polymarket UI rule: midpoint only when BOTH sides exist and the spread is <= 0.10.
    # A one-sided book must not qualify just because the empty side was filled in as 0 or 1
    # (that phantom spread is 1 - best_bid, which is <= 0.10 whenever the bid is >= 0.90).
    if closed and len(gprices) == 2 and None not in gprices:
        ui_price, ui_rule = gprices[0], "closed market: Gamma outcomePrices"
    elif mid_valid:
        ui_price, ui_rule = naive_mid, "midpoint (spread ≤ 0.10)"
    elif last_A is not None:
        if not book_available:
            ui_rule = "last trade (no live book)"
        elif not two_sided:
            ui_rule = "last trade (one-sided book)"
        else:
            ui_rule = "last trade (spread > 0.10)"
        ui_price = last_A
    elif naive_mid is not None:
        ui_price, ui_rule = naive_mid, "midpoint (no trades to fall back on)"
    else:
        ui_price, ui_rule = (gprices[0] if gprices else None), "Gamma outcomePrices"

    # ---- belief model (all in outcome-0 probability) ----
    reasons = []
    trade_est, trade_W = None, 0.0
    if trades_out:
        ref = trades_out[-1]["ts"]
        num = den = 0.0
        for src, dst in zip(trades, trades_out):
            decay = 0.5 ** (max(0.0, ref - src["ts"]) / 3600 / HALF_LIFE_H)
            w = max(src["usd"], 0.01) * PHASE_MULT.get(src["phase"], 1) * decay
            dst["weight"] = round(w, 3)
            num += w * src["priceA"]
            den += w
        # keep weights on the tape rows that alias the same dicts (takers are trades_out)
        trade_est, trade_W = (num / den, den) if den else (None, 0.0)
        n_after = sum(1 for t in trades_out if t["phase"] in ("in-play", "post-end"))
        if trade_est is not None:
            reasons.append(f"{len(trades_out)} trade(s), ${sum(t['usd'] for t in trades_out):,.2f} spent; "
                           f"recency/phase-weighted trade price → {A} {trade_est:.3f} "
                           f"({n_after} trade(s) after game start/end weighted ×2–3).")
    else:
        reasons.append("Zero trades ever: verdict is based only on resting orders, so confidence is low.")

    book_est, book_W, lo, hi = None, 0.0, 0.0, 1.0
    if book_available and has_level:
        if mid_valid:
            bs, as_ = bids[0]["size"], asks[0]["size"]
            book_est = (best_bid * as_ + best_ask * bs) / (bs + as_) if bs + as_ > 0 else naive_mid
            book_W = (top_bid_usd + top_ask_usd) * BOOK_WEIGHT
            lo, hi = best_bid, best_ask
            reasons.append(f"Two-sided book, spread {spread:.3f}: midpoint is valid; depth-weighted micro-price {book_est:.3f}.")
        elif two_sided and crossed:
            book_est = None
            lo, hi = 0.0, 1.0
            reasons.append("Book is crossed (best bid above best ask); midpoint is not used and the book does not bound the estimate.")
        elif two_sided:
            bs, as_ = top_bid_usd, top_ask_usd
            book_est = (best_bid * as_ + best_ask * bs) / (bs + as_) if bs + as_ > 0 else naive_mid
            book_W = (bs + as_) * BOOK_WEIGHT * 0.5
            lo, hi = best_bid, best_ask
            reasons.append(f"Wide book {best_bid:.3f}/{best_ask:.3f}: belief bounded in that range; "
                           f"money-weighted estimate {book_est:.3f}. Naive midpoint {naive_mid:.3f} is not reliable.")
        elif bids:
            book_est, lo = best_bid, best_bid
            book_W = top_bid_usd * BOOK_WEIGHT
            reasons.append(f"Only bids for {A} (best {best_bid:.3f}, ${top_bid_usd:,.2f} near top) and nobody selling: "
                           f"belief in {A} is at least {best_bid:.3f}. Naive midpoint {naive_mid:.3f} just averages with an empty 1.00 ask.")
        else:
            book_est, hi = best_ask, best_ask
            book_W = top_ask_usd * BOOK_WEIGHT
            reasons.append(f"Only offers of {A} (best {best_ask:.3f}) i.e. bids for {B}, nobody bids {A}: "
                           f"belief in {A} is at most {best_ask:.3f}. Naive midpoint {naive_mid:.3f} averages with an empty 0 bid.")
    elif not book_available:
        reasons.append("No live book (market closed): estimate uses executed trades only.")

    # A number we will actually call a belief. Gamma's price is not one: on a resolved
    # market it is the settlement (0 or 1), and with no tape it was never a trade.
    has_estimate = trade_est is not None or book_est is not None
    if trade_est is not None and book_est is not None:
        est = (trade_est * trade_W + book_est * book_W) / (trade_W + book_W) if trade_W + book_W > 0 else trade_est
    elif trade_est is not None:
        est = trade_est
    elif book_est is not None:
        est = book_est
    else:
        est = gprices[0] if gprices and gprices[0] is not None else None
        if status == "resolved":
            reasons.append("No trades and no orders. Gamma outcomePrices are the settlement, not a pre-resolution belief, so they are not used as the estimate.")
        else:
            reasons.append("No trades and no orders. Gamma outcomePrices are shown for reference only and are not a confirmed belief.")
    if has_estimate:
        if lo > hi:
            lo, hi = hi, lo
        est_unclamped = est
        est = min(max(est, lo), hi)
        if abs(est - est_unclamped) > 1e-6:
            reasons.append(f"Trade estimate {est_unclamped:.3f} clamped into book bounds [{lo:.3f}, {hi:.3f}].")
    bound = None
    if book_available and not two_sided and has_level and trade_est is None and not crossed:
        bound = ">=" if bids else "<="
    # A floor below 50% (or a ceiling above 50%) still contains a coin-flip.
    # Reporting the other outcome as the lean (e.g. "No 80%" from a 20% bid) is not supported.
    ambiguous = (trade_est is None and bound is not None and lo < 0.5 - 1e-9 and hi > 0.5 + 1e-9)
    if ambiguous:
        fav = next((f.get("favours") for f in flags if f.get("kind") == "one-sided"), None)
        side = fav or (A if bound == ">=" else B)
        reasons.append(
            f"That bound still includes 50%, so it does not confirm a lean toward {side}.")

    fav = next((f.get("favours") for f in flags if f.get("kind") == "one-sided"), None)
    if has_estimate:
        lean = A if est >= 0.5 else B
        if ambiguous and fav:
            lean = fav
        p_lean = est if lean == A else 1 - est
    else:
        lean, p_lean = None, None
    signal_usd = sum(t["usd"] for t in trades_out if t["phase"] in ("in-play", "post-end")) + \
        sum(t["usd"] for t in trades_out) * 0.3 + (top_bid_usd + top_ask_usd) * BOOK_WEIGHT
    one_sided_agrees = bool(lean) and any(f.get("favours") == lean for f in flags)
    if not trades_out:
        conf = "Low"
    elif signal_usd >= 500 and p_lean is not None and p_lean >= 0.8:
        conf = "High"
    elif signal_usd >= 100 and p_lean is not None and p_lean >= 0.6:
        conf = "Medium"
    else:
        conf = "Low"
    if conf == "Low" and trades_out and one_sided_agrees and signal_usd >= 50:
        conf = "Medium"
    if conf == "Medium" and one_sided_agrees and signal_usd >= 300 and p_lean is not None and p_lean >= 0.85:
        conf = "High"
    disagree = (trade_est is not None and book_est is not None
                and (trade_est - 0.5) * (book_est - 0.5) < 0 and abs(trade_est - book_est) > 0.3)
    if disagree:
        reasons.append("Trades and resting orders point different ways: treat with caution.")
        conf = "Low" if conf != "High" else "Medium"
    if fav and fav != lean and not ambiguous:
        reasons.append(f"One-sided book favors {fav}, while the price estimate favors {lean}. Treat the lean with caution.")
        conf = "Low"

    strength = "no signal" if not has_estimate else ("unconfirmed" if ambiguous else lean_words(est))
    if status == "resolved" and not has_estimate:
        who = winner or "unknown"
        headline = (f"Resolved {who}. No trades were recorded and the book is gone, "
                    f"so the pre-resolution belief is unknown.")
        reasons.insert(0, f"Market resolved to {who}. Settlement prices are not a trader belief when the tape is empty.")
    elif status == "resolved":
        headline = f"Resolved {winner}. Before resolution traders leaned {lean} ({p_lean * 100:.1f}%)."
        agrees = (winner == lean)
        reasons.insert(0, f"Market resolved to {winner}; trader belief {'matched' if agrees else 'did NOT match'} the outcome.")
    elif not has_estimate:
        gp = gprices[0] if gprices else None
        shown = f"{A} {gp * 100:.1f}%" if gp is not None else "no price"
        headline = (f"No confirmed belief — no trades and no resting orders. "
                    f"Gamma shows {shown}. {conf.lower()} confidence.")
    elif ambiguous:
        if bound == ">=":
            headline = (f"No confirmed lean. One-sided book favors {lean}, but {A} is only "
                        f"≥{est * 100:.1f}% — the bound includes 50%. {conf.lower()} confidence.")
        else:
            headline = (f"No confirmed lean. One-sided book favors {lean}, but {A} is only "
                        f"≤{est * 100:.1f}% — that does not put {B} above 50%. {conf.lower()} confidence.")
    else:
        ineq = ""
        if bound == ">=" and lean == A:
            ineq = "≥"
        elif bound == "<=" and lean == B:
            ineq = "≥"
        headline = (f"Traders believe {lean} ({ineq}{p_lean * 100:.1f}%) — "
                    f"{lean_words(est)} lean, {conf.lower()} confidence.")

    # ---- OI / volume ----
    oi_val = None
    if isinstance(oi_raw, list):
        if oi_raw and isinstance(oi_raw[0], dict):
            oi_val = fnum(oi_raw[0].get("value"))
    elif isinstance(oi_raw, dict):
        oi_val = fnum(oi_raw.get("value"))
    if oi_err:
        warnings.append(f"/oi: {short_err(oi_err)}")
    elif oi_raw == [] and not trades_out and not makers_out:
        oi_val = 0.0
        warnings.append("/oi returned [] (no positions ever minted): shown as 0.")
    elif oi_raw == [] and (trades_out or makers_out):
        warnings.append("/oi returned [] even though the tape has fills.")
    vol = first_num(g.get("volumeNum"), g.get("volume"), g.get("volumeClob"))
    vol_shares_tape = sum(t["size"] for t in trades_out)
    if vol is None and not trades_out:
        vol = 0.0
    no_flow = not trades_out and not makers_out
    if trade_errs and not trades_out and not makers_out:
        oi_expl = "Trade history could not be loaded, so OI and volume cannot be interpreted from the tape."
    elif no_flow:
        oi_zero = oi_val is not None and abs(oi_val) <= 1e-9
        vol_zero = vol is None or abs(vol) <= 1e-9
        if oi_zero and vol_zero and book_available:
            oi_expl = "No trades: OI and volume are both 0. Any displayed price comes purely from resting orders."
        elif oi_zero and vol_zero:
            oi_expl = "No trades: OI and volume are both 0. The order book is gone, so Gamma's price is not a live book."
        else:
            oi_expl = "No trades were returned, but OI or Gamma volume is non-zero, so the tape may be incomplete."
    elif status == "resolved":
        # After resolution OI collapses as winners redeem. That is not evidence of closes.
        oi_expl = ("Market is resolved: OI shrinks as winners redeem and positions are merged, so OI vs volume no longer "
                   "tells you about closing trades. Use the trade tape: "
                   + (f"{sells_sh:,.2f} shares were sold by takers." if sells_sh else "no taker SELL trades appear on the tape."))
    elif oi_val is not None and vol and abs(vol - oi_val) <= max(0.01 * vol, 0.5):
        oi_expl = "OI == volume: every share ever traded is still held; nobody has closed or sold a position."
    elif oi_val is not None and vol and vol > oi_val:
        oi_expl = (f"Volume exceeds OI by {vol - oi_val:,.2f}: roughly that many shares changed hands as closes "
                   f"(sells to other buyers, or merges). Taker SELLs on tape: {sells_sh:,.2f} shares.")
    else:
        oi_expl = "OI and volume are not directly comparable here (missing data or OI > volume due to different counting)."

    # ---- holders ----
    holders = [[], []]
    if hold_err:
        warnings.append(f"holders: {short_err(hold_err)}")
    elif hold_raw is None:
        warnings.append("holders endpoint returned null (no holders or not indexed yet).")
    if isinstance(hold_raw, list):
        for grp in hold_raw:
            if not isinstance(grp, dict):
                continue
            tok = str(grp.get("token")) if grp.get("token") is not None else ""
            idx = tokens.index(tok) if tok in tokens else None
            raw_holders = grp.get("holders")
            if not isinstance(raw_holders, list):
                continue
            for h in raw_holders:
                if not isinstance(h, dict):
                    continue
                i = idx if idx is not None else outcome_index(
                    h.get("outcomeIndex"), h.get("outcome"), h.get("asset"), tokens, outcomes)
                if i not in (0, 1):
                    continue
                w = h.get("proxyWallet") or ""
                if not isinstance(w, str):
                    w = ""
                name = h.get("name") or h.get("pseudonym") or ""
                if not isinstance(name, str):
                    name = ""
                amt = fnum(h.get("amount"))
                if amt is None or amt < 0:
                    continue
                holders[i].append({
                    "wallet": eth_addr(w),
                    "walletShort": (w[:6] + "…" + w[-4:]) if len(w) > 12 else w,
                    "name": name[:200], "amount": amt,
                })
    for hl in holders:
        hl.sort(key=lambda x: -x["amount"])

    # ---- history ----
    history = []
    for i, (h, e) in enumerate(hists):
        pts = history_points(h)
        history.append(pts)
        if not pts:
            warnings.append(f"prices-history empty for {outcomes[i]}" + (f" ({short_err(e)})" if e else ""))
    last_hist = history[0][-1]["p"] if history and history[0] else None
    if last_hist is None and len(history) > 1 and history[1]:
        last_hist = 1 - history[1][-1]["p"]
    gamma_ltp = fnum(g.get("lastTradePrice"))
    if gamma_ltp is not None and last_A is not None and abs(gamma_ltp - last_A) > 0.02:
        warnings.append(f"Gamma lastTradePrice={gamma_ltp} differs from the tape's last trade ({A}-equivalent {last_A:.3f}); "
                        f"Gamma tracks the {A} token only, so trades in {B} are ignored there.")

    def pair(p):
        return None if p is None else [round(p, 4), round(1 - p, 4)]

    ours_note = "trades (recency+phase weighted) + resting money, bounded by book"
    if not has_estimate:
        ours_note = "no trades and no book — Gamma's price is not used as a belief"
    elif ambiguous:
        ours_note = "one-sided book bound only; the interval includes 50%, so this is not a confirmed lean"
    comparison = [
        {"label": "Polymarket UI price", "note": ui_rule, "p": pair(ui_price)},
        {"label": "Gamma outcomePrices", "note": "what most APIs/systems ingest",
         "p": [gprices[0], gprices[1]] if len(gprices) == 2 and None not in gprices else None},
        {"label": "Naive midpoint", "note": "(bid+ask)/2, empty side = 0 / 1" + ("" if mid_valid else " — NOT valid"),
         "p": pair(naive_mid)},
        {"label": "Last price-history point", "note": "CLOB prices-history (midpoint series)", "p": pair(last_hist)},
        {"label": "Last trade",
         "note": (f"{last_trade['outcome']} {last_trade['side']} @ {last_trade['price']} · {last_trade['time']}"
                  if last_trade else "no trades"),
         "p": pair(last_A)},
        {"label": "VWAP (all trades)", "note": "USD-weighted", "p": pair(vwap_all)},
        {"label": "Our estimate", "note": ours_note, "p": pair(est) if has_estimate else None, "ours": True},
    ]

    slug = g.get("slug") if isinstance(g.get("slug"), str) else ""
    market_url = f"https://polymarket.com/market/{slug}" if _slug_ok(slug) else None
    # de-duplicate warnings, keep order
    deduped, seen_w = [], set()
    for w in warnings:
        if w and w not in seen_w:
            seen_w.add(w)
            deduped.append(w)

    return {
        "market": {
            "id": str(g.get("id")), "question": g.get("question"), "slug": slug or None,
            "url": market_url,
            "outcomes": outcomes, "tokens": tokens, "conditionId": cond,
            "description": g.get("description") if isinstance(g.get("description"), str) else None,
            "resolutionSource": https_url(g.get("resolutionSource")),
            "image": https_url(g.get("icon") or g.get("image")),
            "closed": closed, "active": g.get("active"), "umaResolutionStatus": g.get("umaResolutionStatus"),
            "status": status, "statusLabel": status_label, "winner": winner,
            "endDate": g.get("endDate"), "endTs": end_ts, "endMadrid": madrid(end_ts),
            "gameStartTime": g.get("gameStartTime"), "gameTs": game_ts, "gameMadrid": madrid(game_ts),
            "playEndTs": play_end, "createdTs": created, "createdMadrid": madrid(created),
            "gamma": {k: g.get(k) for k in ("volume", "liquidity", "bestBid", "bestAsk", "spread", "lastTradePrice", "outcomePrices")},
        },
        "verdict": {
            "lean": lean,
            "probs": None if not has_estimate else [round(est, 4), round(1 - est, 4)],
            "pLean": None if p_lean is None else round(p_lean, 4),
            "strength": strength, "confidence": "Final (resolved)" if status == "resolved" else conf,
            "beliefConfidence": conf, "bound": bound, "ambiguous": ambiguous, "headline": headline,
            "reasons": reasons, "flags": flags,
            "tradeEstimate": None if trade_est is None else round(trade_est, 6),
            "bookEstimate": None if book_est is None else round(book_est, 6),
            "signalUsd": round(signal_usd, 2),
            "resolvedTo": winner,
        },
        "comparison": comparison,
        "book": {"available": book_available, "bids": bids, "asks": asks, "bestBid": best_bid, "bestAsk": best_ask,
                 "spread": spread, "naiveMid": naive_mid, "midValid": mid_valid, "twoSided": two_sided,
                 "topBidUsd": round(top_bid_usd, 2), "topAskUsd": round(top_ask_usd, 2),
                 "errors": [short_err(e) for e in book_errs]},
        "stats": {"oi": oi_val, "volumeGamma": vol, "tradeCount": len(trades_out), "tapeShares": round(vol_shares_tape, 4),
                  "usdByOutcome": {k: round(v, 2) for k, v in usd_by.items()},
                  "sharesBoughtByOutcome": {k: round(v, 4) for k, v in sh_by.items()},
                  "takerSellShares": round(sells_sh, 4), "makerFills": len(makers_out), "explanation": oi_expl,
                  "liquidity": first_num(g.get("liquidityNum"), g.get("liquidity"))},
        "trades": trades_out,
        "fills": all_fills,
        "history": history,
        "holders": holders,
        "warnings": deduped,
        "generatedAt": madrid(now),
    }


# ----- HTML -------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thin-market belief dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d212a;--line:#2a2f3a;--text:#e7e9ee;--muted:#9aa3b2;
--a:#3b82f6;--b:#f59e0b;--good:#22c55e;--bad:#ef4444;--warn:#eab308;--radius:12px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header.top{display:flex;gap:12px;align-items:center;padding:14px 22px;border-bottom:1px solid var(--line);background:#0c0e12;position:sticky;top:0;z-index:5}
header.top h1{font-size:15px;margin:0 12px 0 0;font-weight:650;letter-spacing:.2px}
input{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:9px 12px;border-radius:9px;width:340px;font-size:14px}
button{background:var(--a);border:0;color:#fff;padding:9px 16px;border-radius:9px;font-weight:600;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--muted)}
main{max-width:1280px;margin:0 auto;padding:20px 22px 60px}
.grid{display:grid;gap:16px}.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}
@media(max-width:900px){.g2,.g3{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--muted);margin:0 0 12px;font-weight:600}
.q{font-size:22px;font-weight:700;margin:0 0 8px}
.meta{display:flex;flex-wrap:wrap;gap:8px 18px;color:var(--muted);font-size:13px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid var(--line)}
.pill.open{color:var(--good);border-color:#1f5f3a}.pill.proposed{color:var(--warn);border-color:#6b5a12}.pill.resolved{color:#a78bfa;border-color:#4c3d85}.pill.disputed{color:var(--bad)}.pill.closed{color:#cbd5e1;border-color:#475569}
.verdict{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}
@media(max-width:900px){.verdict{grid-template-columns:1fr}}
.headline{font-size:19px;font-weight:700;margin:2px 0 12px}
.bar{display:flex;height:34px;border-radius:9px;overflow:hidden;font-weight:700;font-size:13px}
.bar div{display:flex;align-items:center;padding:0 10px;white-space:nowrap;min-width:0}
.conf{font-size:12px;font-weight:700;padding:3px 9px;border-radius:7px}
.conf.High,.conf.Final{background:#143d27;color:#4ade80}.conf.Medium{background:#3d3412;color:#facc15}.conf.Low{background:#3d1717;color:#f87171}
ul.reasons{margin:10px 0 0;padding-left:18px;color:#cfd4dd}ul.reasons li{margin:4px 0}
.flag{background:#2a1f0c;border:1px solid #6b4e12;color:#fcd34d;padding:8px 12px;border-radius:9px;margin:6px 0;font-weight:600}
.flag.one-sided{background:#0f2a1c;border-color:#1f6b42;color:#86efac}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.5px;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #20242d;font-variant-numeric:tabular-nums}
tr.ours td{background:#13233d;font-weight:700}
.num{text-align:right}.muted{color:var(--muted)}.small{font-size:12px}
.big{font-size:26px;font-weight:750}
.A{color:var(--a)}.B{color:var(--b)}
.depth{position:relative}.depth span.bg{position:absolute;inset:3px 0;opacity:.18;border-radius:4px}
.tag{font-size:11px;padding:2px 7px;border-radius:6px;background:#232834;color:#c7cdd8}
.tag.in-play{background:#3b2a0e;color:#fbbf24}.tag.post-end{background:#3a1530;color:#f472b6}.tag.pre-game{background:#122b3d;color:#7dd3fc}
.warn{color:#fbbf24;font-size:12.5px}
.scroll{max-height:420px;overflow:auto}
a{color:#7fb0ff}
#empty{color:var(--muted);text-align:center;padding:80px 0}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:s 1s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}
.chartnote{color:var(--muted);font-size:12px;margin-top:8px}
</style></head><body>
<header class="top"><h1>Thin-market belief</h1>
<input id="mid" placeholder="Gamma market id, slug or polymarket URL" autofocus>
<button id="go">Analyze</button><button class="ghost" id="raw">JSON</button>
<span class="muted small" id="gen"></span></header>
<main id="app"><div id="empty">Paste a Polymarket market id (e.g. 5733114) and press Analyze.</div></main>
<script>
const $=s=>document.querySelector(s);
const TZ='Europe/Madrid';
const fmtT=ts=>ts==null?'—':new Intl.DateTimeFormat('en-GB',{timeZone:TZ,year:'numeric',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}).format(new Date(ts*1000));
const fmtShort=ms=>new Intl.DateTimeFormat('en-GB',{timeZone:TZ,month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(ms));
const pct=p=>p==null?'—':(p*100).toFixed(1)+'%';
const usd=v=>v==null?'—':'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const n2=v=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const isAddr=w=>/^0x[a-fA-F0-9]{40}$/.test(w||'');
const isTx=w=>/^0x[a-fA-F0-9]{64}$/.test(w||'');
function httpsUrl(u){
  if(typeof u!=='string' || u.length>500) return null;
  try{
    const x=new URL(u);
    if(x.protocol!=='https:' || x.username || x.password) return null;
    return u;
  }catch(e){return null}
}
let chart=null,current=null;

async function load(id){
  if(!id) return; current=id;
  history.replaceState(null,'','?id='+encodeURIComponent(id));
  $('#go').innerHTML='<span class="spinner"></span>';
  try{
    const r=await fetch('/api/market/'+encodeURIComponent(id)); const d=await r.json();
    if(d.error){$('#app').innerHTML='<div id="empty">'+esc(d.error)+'</div>';return}
    render(d);
  }catch(e){$('#app').innerHTML='<div id="empty">Error: '+esc(e)+'</div>'}
  finally{$('#go').textContent='Analyze'}
}

function render(d){
  const m=d.market,v=d.verdict,[A,B]=m.outcomes,s=d.stats,bk=d.book;
  $('#gen').textContent='updated '+d.generatedAt;
  const conf=(v.confidence||'').split(' ')[0];
  const flags=(v.flags||[]).map(f=>`<div class="flag ${esc(f.kind)}">${esc(f.text)}</div>`).join('');
  const boundTxt=v.bound&&v.probs?`<div class="small muted">Book-only bound: ${esc(A)} ${v.bound==='>='?'≥':'≤'} ${pct(v.probs[0])}</div>`:'';
  const cmp=(d.comparison||[]).map(c=>`<tr class="${c.ours?'ours':''}"><td>${esc(c.label)}<div class="small muted">${esc(c.note)}</div></td>
     <td class="num A">${c.p?pct(c.p[0]):'—'}</td><td class="num B">${c.p?pct(c.p[1]):'—'}</td></tr>`).join('');
  const img=httpsUrl(m.image), src=httpsUrl(m.resolutionSource), murl=httpsUrl(m.url);
  const bar=!v.probs
    ? `<div class="muted small">No trade or book signal to plot.</div>`
    : v.ambiguous
    ? `<div class="muted small">No confirmed probability — the resting book does not pin either side past 50%.</div>`
    : `<div class="bar"><div style="width:${Math.max((v.probs[0]||0)*100,0.5)}%;background:var(--a)">${esc(A)} ${pct(v.probs[0])}</div>
        <div style="width:${Math.max((v.probs[1]||0)*100,0.5)}%;background:var(--b);justify-content:flex-end">${esc(B)} ${pct(v.probs[1])}</div></div>`;
  const html=`
  <div class="card" style="margin-bottom:16px">
    <div style="display:flex;gap:14px;align-items:flex-start">
      ${img?`<img src="${esc(img)}" alt="" style="width:52px;height:52px;border-radius:10px;object-fit:cover">`:''}
      <div style="flex:1"><div class="q">${esc(m.question)}</div>
      <div class="meta"><span class="pill ${esc(m.status)}">${esc(m.statusLabel)}</span>
        <span>Game start: <b>${fmtT(m.gameTs)}</b></span><span>End date: <b>${fmtT(m.endTs)}</b></span>
        <span>Created: ${fmtT(m.createdTs)}</span>
        ${src?`<span>Source: <a href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(src.replace(/^https?:\/\//,'').slice(0,60))}</a></span>`:''}
        ${murl?`<span><a href="${esc(murl)}" target="_blank" rel="noopener noreferrer">Polymarket ↗</a></span>`:''}
        <span class="muted">id ${esc(m.id)} · UMA: ${esc(m.umaResolutionStatus||'none')}</span></div></div></div>
    ${m.description?`<details style="margin-top:10px"><summary class="muted small">Rules</summary><div class="small" style="white-space:pre-wrap;color:#cfd4dd;margin-top:6px">${esc(m.description)}</div></details>`:''}
  </div>

  <div class="card verdict" style="margin-bottom:16px">
    <div><h2>Trader belief</h2>
      <div class="headline">${esc(v.headline)}</div>
      ${bar}
      <div style="margin-top:10px;display:flex;gap:10px;align-items:center"><span class="conf ${esc(conf)}">${esc(v.confidence)}</span>
        ${m.status==='resolved'?`<span class="small muted">pre-resolution belief confidence: ${esc(v.beliefConfidence)}</span>`:''}
        <span class="small muted">${esc(v.strength)} lean · signal ≈ ${usd(v.signalUsd)} weighted</span></div>
      ${boundTxt}${flags}
      <ul class="reasons">${(v.reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('')}</ul>
    </div>
    <div><h2>Displayed price vs real belief</h2>
      <table><tr><th>Source</th><th class="num">${esc(A)}</th><th class="num">${esc(B)}</th></tr>${cmp}</table></div>
  </div>

  <div class="grid g3" style="margin-bottom:16px">
    <div class="card"><h2>Open interest</h2><div class="big">${s.oi==null?'—':n2(s.oi)}</div><div class="small muted">shares (≈ USD of full sets) · Data API /oi</div></div>
    <div class="card"><h2>Volume (Gamma)</h2><div class="big">${s.volumeGamma==null?'—':n2(s.volumeGamma)}</div><div class="small muted">${s.tradeCount} taker trade(s) + ${s.makerFills||0} maker fills · ${n2(s.tapeShares)} taker shares</div></div>
    <div class="card"><h2>Actual USD spent (size × price)</h2><div class="small muted" style="margin:-6px 0 8px">buy fills only, taker + maker, each side once</div>
      <div style="display:flex;gap:22px"><div><div class="big A">${usd(s.usdByOutcome[A])}</div><div class="small muted">on ${esc(A)} · ${n2(s.sharesBoughtByOutcome[A])} sh</div></div>
      <div><div class="big B">${usd(s.usdByOutcome[B])}</div><div class="small muted">on ${esc(B)} · ${n2(s.sharesBoughtByOutcome[B])} sh</div></div></div></div>
  </div>
  <div class="card" style="margin-bottom:16px"><b>OI vs volume:</b> ${esc(s.explanation)}
    <div class="small muted" style="margin-top:4px">Gamma volume counts shares (each $1 full set), not dollars; the tape shows takers only — the maker on the other side bought the opposite outcome or sold the same one.</div></div>

  <div class="card" style="margin-bottom:16px"><h2>Price history (${esc(A)} / ${esc(B)})</h2>
    <div style="height:360px"><canvas id="ch"></canvas></div>
    <div class="chartnote">Lines are CLOB <b>midpoints</b> (empty side counted as 0 / 1), not trades. Any move without a dot is just orders being placed or cancelled. Dots = executed trades, sized by USD; filled = BUY, hollow = SELL; blue = ${esc(A)}, orange = ${esc(B)}. Dashed lines: game start, end date, first trade.</div></div>

  <div class="grid g2" style="margin-bottom:16px">
    <div class="card"><h2>Unified order book (in ${esc(A)} price)</h2>${bookHtml(d)}</div>
    <div class="card"><h2>Top holders</h2>${holdersHtml(d)}</div>
  </div>
  <div class="card" style="margin-bottom:16px"><h2>Trade tape (Europe/Madrid)</h2>${tapeHtml(d)}</div>
  ${(d.warnings||[]).length?`<div class="card"><h2>API notes</h2>${d.warnings.map(w=>`<div class="warn">• ${esc(w)}</div>`).join('')}</div>`:''}`;
  $('#app').innerHTML=html;
  drawChart(d);
}

function bookHtml(d){
  const bk=d.book,[A,B]=d.market.outcomes;
  if(!bk.available) return '<div class="muted">No live order book (market closed or not on CLOB).</div>';
  const maxU=Math.max(1,...bk.bids.map(l=>l.usd),...bk.asks.map(l=>l.usd));
  const row=(l,side)=>`<tr><td class="depth"><span class="bg" style="width:${l.usd/maxU*100}%;background:${side==='ask'?'var(--b)':'var(--a)'}"></span>
     ${side==='ask'?`<span class="B">Sell ${esc(A)} @ ${l.price.toFixed(3)}</span> <span class="muted small">≡ buy ${esc(B)} @ ${(1-l.price).toFixed(3)}</span>`:`<span class="A">Buy ${esc(A)} @ ${l.price.toFixed(3)}</span> <span class="muted small">≡ sell ${esc(B)} @ ${(1-l.price).toFixed(3)}</span>`}</td>
     <td class="num">${n2(l.size)}</td><td class="num">${usd(l.usd)}</td></tr>`;
  const asks=[...bk.asks].reverse().map(l=>row(l,'ask')).join('')||`<tr><td colspan=3 class="muted">Nobody sells ${esc(A)} / bids ${esc(B)}</td></tr>`;
  const bids=bk.bids.map(l=>row(l,'bid')).join('')||`<tr><td colspan=3 class="muted">Nobody bids ${esc(A)} / sells ${esc(B)}</td></tr>`;
  const mid=`<tr><td colspan=3 style="text-align:center;background:#11141a" class="small">spread ${bk.spread==null?'—':bk.spread.toFixed(3)} · naive mid ${bk.naiveMid==null?'—':bk.naiveMid.toFixed(3)} ${bk.midValid?'<span style="color:var(--good)">(valid)</span>':'<span style="color:var(--bad)">(not valid)</span>'}</td></tr>`;
  return `<div class="scroll"><table><tr><th>Order</th><th class="num">Shares</th><th class="num">USD at stake</th></tr>${asks}${mid}${bids}</table></div>
   <div class="small muted" style="margin-top:6px">Both token books mirror each other; merged and de-duplicated into one. USD at stake = money the order owner commits.</div>`;
}
function holdersHtml(d){
  const [A,B]=d.market.outcomes;
  const col=(i,name)=>{const h=d.holders[i]||[];return `<div><div class="${i?'B':'A'}" style="font-weight:700;margin-bottom:4px">${esc(name)}</div>
   ${h.length?`<table>${h.slice(0,10).map(x=>{
     const who=esc(x.name||x.walletShort||'holder');
     const cell=isAddr(x.wallet)?`<a href="https://polymarket.com/profile/${esc(x.wallet)}" target="_blank" rel="noopener noreferrer">${who}</a>`:who;
     return `<tr><td>${cell}</td><td class="num">${n2(x.amount)}</td></tr>`;
   }).join('')}</table>`:'<div class="muted small">none</div>'}</div>`};
  return `<div class="grid g2">${col(0,A)}${col(1,B)}</div>`;
}
function tapeHtml(d){
  const t=[...(d.fills||d.trades||[])].reverse();
  if(!t.length) return '<div class="muted">No trades have ever executed in this market.</div>';
  const note='<div class="small muted" style="margin-bottom:6px">Taker rows drive the belief model and chart dots; maker rows are the counterparties of the same fills (shown for USD-per-outcome accounting, not double-counted).</div>';
  return note+`<div class="scroll"><table><tr><th>Time</th><th>Phase</th><th>Outcome</th><th>Side</th><th class="num">Shares</th><th class="num">Price</th><th class="num">USD</th><th>Bullish for</th><th>Role</th><th>Wallet</th></tr>
   ${t.map(x=>{
     const who=esc(x.name||x.walletShort||'');
     const cell=isTx(x.tx)?`<a href="https://polygonscan.com/tx/${esc(x.tx)}" target="_blank" rel="noopener noreferrer" title="${esc(x.wallet)}">${who}</a>`:who;
     return `<tr><td>${fmtT(x.ts)}</td><td><span class="tag ${esc(x.phase)}">${esc(x.phase)}</span></td><td class="${x.outcomeIndex?'B':'A'}">${esc(x.outcome)}</td><td>${esc(x.side)}</td>
   <td class="num">${n2(x.size)}</td><td class="num">${Number(x.price).toFixed(3)}</td><td class="num">${usd(x.usd)}</td><td>${esc(x.bullishFor)}</td><td class="${x.role==='maker'?'muted':''}">${esc(x.role)}</td>
   <td>${cell}</td></tr>`;
   }).join('')}</table></div>`;
}

function drawChart(d){
  try{
    if(chart){chart.destroy();chart=null}
    const canvas=document.getElementById('ch');
    if(!canvas) return;
    if(typeof Chart==='undefined'){canvas.outerHTML='<div class="muted">Chart.js failed to load (offline?).</div>';return}
    const m=d.market,[A,B]=m.outcomes;
    const hist=d.history||[[],[]];
    const trades=d.trades||[];
    const line=(i,c)=>({type:'line',label:(i?B:A)+' midpoint',data:(hist[i]||[]).filter(p=>p&&p.t!=null&&p.p!=null).map(p=>({x:p.t*1000,y:p.p})),borderColor:c,backgroundColor:c,borderWidth:1.6,pointRadius:0,stepped:true,order:2});
    const maxU=Math.max(1,...trades.map(t=>t.usd||0));
    const dots=(i,c)=>({type:'scatter',label:(i?B:A)+' trades',order:1,
      data:trades.filter(t=>t.outcomeIndex===i).map(t=>({x:t.ts*1000,y:t.price,t})),
      pointRadius:ctx=>{const t=ctx.raw&&ctx.raw.t;return t?4+14*Math.sqrt((t.usd||0)/maxU):4},
      pointBackgroundColor:ctx=>ctx.raw&&ctx.raw.t&&ctx.raw.t.side==='SELL'?'transparent':c+'cc',
      pointBorderColor:'#fff',pointBorderWidth:ctx=>ctx.raw&&ctx.raw.t&&ctx.raw.t.side==='SELL'?2:1,borderColor:c});
    const marks=[];
    if(m.gameTs) marks.push({x:m.gameTs*1000,label:'game start',c:'#7dd3fc'});
    if(m.endTs && Math.abs((m.endTs-(m.gameTs||0)))>60) marks.push({x:m.endTs*1000,label:'end date',c:'#f472b6'});
    else if(m.endTs && m.gameTs){
      if(marks.length) marks[0].label='game start = end date';
      else marks.push({x:m.endTs*1000,label:'game start = end date',c:'#7dd3fc'});
    }else if(m.endTs) marks.push({x:m.endTs*1000,label:'end date',c:'#f472b6'});
    if(trades.length) marks.push({x:trades[0].ts*1000,label:'first trade',c:'#a3e635'});
    const vlines={id:'vlines',afterDatasetsDraw(c){const {ctx,chartArea:a,scales:{x}}=c;ctx.save();
      marks.forEach((mk,i)=>{const px=x.getPixelForValue(mk.x);if(px<a.left||px>a.right)return;ctx.strokeStyle=mk.c;ctx.setLineDash([5,4]);ctx.lineWidth=1.2;
        ctx.beginPath();ctx.moveTo(px,a.top);ctx.lineTo(px,a.bottom);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=mk.c;ctx.font='11px system-ui';ctx.fillText(mk.label,px+4,a.top+12+i*13)});ctx.restore()}};
    const xs=[...hist.flat().map(p=>p&&p.t*1000),...trades.map(t=>t.ts*1000),...marks.map(mk=>mk.x)].filter(v=>Number.isFinite(v));
    if(!xs.length){canvas.outerHTML='<div class="muted">No price history or trades to chart.</div>';return}
    chart=new Chart(canvas,{data:{datasets:[line(0,'#3b82f6'),line(1,'#f59e0b'),dots(0,'#3b82f6'),dots(1,'#f59e0b')]},
      plugins:[vlines],options:{maintainAspectRatio:false,animation:false,parsing:false,interaction:{mode:'nearest',intersect:false},
      scales:{x:{type:'linear',min:Math.min(...xs),max:Math.max(...xs),ticks:{color:'#9aa3b2',maxTicksLimit:8,callback:v=>fmtShort(v)},grid:{color:'#222733'}},
              y:{min:0,max:1,ticks:{color:'#9aa3b2',callback:v=>(v*100)+'%'},grid:{color:'#222733'}}},
      plugins:{legend:{labels:{color:'#cfd4dd',usePointStyle:true}},tooltip:{callbacks:{label:ctx=>{const t=ctx.raw&&ctx.raw.t;return t?`${t.outcome} ${t.side} ${n2(t.size)} @ ${Number(t.price).toFixed(3)} = ${usd(t.usd)} (${t.phase}) ${t.name||t.walletShort}`:`${ctx.dataset.label}: ${pct(ctx.raw.y)}`},
        title:items=>items.length?fmtShort(items[0].raw.x)+' Madrid':''}}}}});
  }catch(e){
    const canvas=document.getElementById('ch');
    if(canvas) canvas.outerHTML='<div class="muted">Chart failed to render.</div>';
  }
}
$('#go').onclick=()=>load($('#mid').value.trim());
$('#mid').addEventListener('keydown',e=>{if(e.key==='Enter')load($('#mid').value.trim())});
$('#raw').onclick=()=>{if(current)window.open('/api/market/'+encodeURIComponent(current),'_blank','noopener')};
const q=new URLSearchParams(location.search).get('id'); if(q){$('#mid').value=q;load(q)}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "pm-dashboard"
    sys_version = ""

    def _send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                         "style-src 'unsafe-inline'; img-src https: data:; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        m = re.fullmatch(r"/api/market/(.+)", path)
        if m:
            try:
                raw = urllib.parse.unquote(m.group(1))
                res = analyze(raw)
                code = int(res.pop("status", 200 if "error" not in res else 404))
            except Exception as e:  # never crash the server
                traceback.print_exc()
                res, code = {"error": f"{type(e).__name__}: {e}"}, 500
            return self._send(code, json.dumps(res, indent=1, default=str), "application/json")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, json.dumps({"error": "not found"}), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now(MADRID).strftime("%H:%M:%S"), fmt % args))


def make_server(port):
    """HTTP server bound to loopback only. port 0 asks the OS for a free port."""
    return ThreadingHTTPServer((BIND_HOST, port), Handler)


def main():
    port = PORT
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            try:
                port = int(a.split("=", 1)[1])
            except ValueError:
                print("Invalid --port (expected an integer 1-65535)", file=sys.stderr)
                sys.exit(2)
            if not 1 <= port <= 65535:
                print("Invalid --port (expected an integer 1-65535)", file=sys.stderr)
                sys.exit(2)
    try:
        srv = make_server(port)
    except OSError as e:
        print(f"Could not bind {BIND_HOST}:{port}: {e}", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"Polymarket belief dashboard on {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Timer(0.6, _open).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
