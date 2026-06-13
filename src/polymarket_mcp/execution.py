"""Order execution. Paper mode simulates fills against the live book;
live mode routes through py-clob-client. Kill-switch is enforced here."""
from __future__ import annotations

import httpx

from . import config, notifications, order_book, risk, state


def place_order(*, mode: str | None = None, market_id: str, token_id: str,
                question: str, category: str, side: str, size_usd: float,
                fair_prob: float | None = None, confidence: float | None = None,
                thesis: str = "", limit_price: float | None = None,
                order_type: str = "market") -> dict:
    """Place a buy order for `side` on a market. `side` is the outcome to buy:
    "YES"/"NO" for binary markets, or the outcome label (e.g. "Lakers") for a
    multi-outcome market — each outcome is its own CLOB token, traded per
    `token_id`. Enforces the kill-switch first. In paper mode, fills against the
    live order book (walking the asks for slippage). In live mode, `order_type`
    selects "market" (FAK) or "limit" (GTC at `limit_price`)."""
    mode = mode or config.MODE
    if size_usd <= 0:
        return {"status": "rejected", "reason": "size_usd must be positive"}

    # Input validation: `side` is the outcome to buy. Multi-outcome markets use
    # the outcome label as the side (e.g. "Candidate A"), so we only reject a
    # blank/non-string side. Normalize lightly: strip whitespace, and upper-case
    # the binary YES/NO labels while preserving the original case of any other
    # outcome label.
    if not isinstance(side, str) or not side.strip():
        return {"status": "rejected",
                "reason": f"side must be a non-empty outcome label, got {side!r}"}
    side = side.strip()
    if side.upper() in {"YES", "NO"}:
        side = side.upper()

    order_type = (order_type or "market").strip().lower()
    if order_type not in {"market", "limit"}:
        return {"status": "rejected",
                "reason": f"order_type must be 'market' or 'limit', got {order_type!r}"}

    ok, reason = risk.check_kill_switch(mode)
    if not ok:
        return {"status": "rejected", "reason": reason}

    cash = state.get_portfolio(mode)["cash"]
    # Cash check differs by mode:
    #   LIVE: never place a live order exceeding tracked cash — hard-reject.
    #   PAPER: do NOT hard-reject on size_usd > cash. The actual fill is
    #     book-depth- AND cash-limited; an order whose real fill fits within cash
    #     should fill (partially). Only reject when there is no cash at all.
    if mode == "paper":
        if cash <= 0:
            return {"status": "rejected", "reason": f"insufficient cash ${cash:.2f}"}
    elif size_usd > cash:
        return {"status": "rejected", "reason": f"insufficient cash ${cash:.2f}"}

    # One open position per market (matches the DB's partial unique index).
    # Pre-check so a duplicate returns a clean rejection instead of an opaque
    # IntegrityError bubbling out of the open-position INSERT.
    if any(p["market_id"] == market_id for p in state.get_positions(mode, "open")):
        return {"status": "rejected",
                "reason": f"already hold an open position in market {market_id}"}

    # Closed-market guard: refuse to open into an already-resolved market.
    # LIVE-ONLY: the guard does a live status fetch, so gating it to live mode
    # keeps paper-mode (simulation) hermetic — paper candidates already come
    # from active markets and reconcile_positions settles resolved ones.
    if mode == "live" and _market_is_closed(market_id):
        return {"status": "rejected", "reason": "market is closed/resolved"}

    if mode == "paper":
        # Cap the spend at available cash so a paper fill NEVER debits more than
        # the portfolio holds. The book walk stops at min(size_usd, cash).
        return _paper_fill(market_id, token_id, question, category, side,
                           size_usd, fair_prob, confidence, thesis, limit_price,
                           cash=cash)
    return _live_fill(market_id, token_id, question, category, side, size_usd,
                      fair_prob, confidence, thesis, order_type, limit_price)


def _market_is_closed(market_id: str) -> bool:
    """Best-effort check of the CLOB market `closed` flag. Returns True only when
    the API clearly reports the market as closed/resolved. On any error (flaky
    status call, non-200, parse failure) returns False so trading is NOT blocked
    by a transient status hiccup."""
    try:
        resp = httpx.get(f"{config.POLYMARKET_HOST}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return False
        data = resp.json()
    except Exception:
        return False
    return bool(data.get("closed", False))


def _paper_fill(market_id, token_id, question, category, side, size_usd,
                fair_prob, confidence, thesis, limit_price, cash=None) -> dict:
    book = order_book.fetch_order_book(token_id)
    asks = book.get("asks")

    # Effective spend cap: never spend more than min(requested size, cash).
    # `cash=None` (legacy callers/tests) means "no cash cap" -> use size_usd.
    effective_cap = size_usd if cash is None else min(size_usd, cash)
    # Whether the cash cap (not just book depth) is the binding constraint.
    cash_capped = cash is not None and cash < size_usd

    # Fallback path: a book with no detailed `asks` ladder (e.g. a mock that only
    # returns best_ask/best_bid). Fill up to the effective cap at best_ask,
    # preserving the original full-size behavior when uncapped.
    if not asks:
        fill_price = book.get("best_ask")
        if fill_price is None:
            return {"status": "rejected", "reason": "no ask liquidity in paper book"}
        if limit_price is not None and fill_price > limit_price:
            return {"status": "rejected",
                    "reason": f"ask {fill_price} above limit {limit_price}"}
        spend = round(effective_cap, 2)
        shares = round(spend / fill_price, 2)
        # A cash cap below the requested size makes this a partial fill.
        return _record_paper_fill(market_id, token_id, question, category, side,
            spend, fill_price, shares, fair_prob, confidence, thesis,
            partial=cash_capped)

    # Slippage-aware path: walk the asks (ascending price) spending USD until the
    # effective cap is consumed, accumulating a size-weighted average fill price.
    spent = 0.0
    shares_acc = 0.0
    remaining = effective_cap
    for level in asks:
        try:
            price = float(level["price"])
            avail_shares = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or avail_shares <= 0:
            continue
        # A limit cap stops the walk: never fill above the limit price.
        if limit_price is not None and price > limit_price:
            break
        level_value = price * avail_shares
        if level_value >= remaining:
            # This level can absorb the rest of the order.
            take_shares = remaining / price
            shares_acc += take_shares
            spent += remaining
            remaining = 0.0
            break
        # Consume the whole level and continue to the next.
        shares_acc += avail_shares
        spent += level_value
        remaining -= level_value

    if spent <= 0 or shares_acc <= 0:
        # Nothing fillable: either no usable levels, or every level was above the
        # limit price. Mirror the limit-rejection message when a limit is set.
        if limit_price is not None:
            best = book.get("best_ask")
            return {"status": "rejected",
                    "reason": f"ask {best} above limit {limit_price}"}
        return {"status": "rejected", "reason": "no ask liquidity in paper book"}

    # Partial if book depth (or limit) left some of the cap unspent, OR the cash
    # cap kept us from spending the full requested size.
    partial = remaining > 1e-9 or cash_capped
    fill_price = round(spent / shares_acc, 4)
    shares = round(shares_acc, 2)
    spent = round(spent, 2)
    return _record_paper_fill(market_id, token_id, question, category, side,
        spent, fill_price, shares, fair_prob, confidence, thesis, partial=partial)


def _record_paper_fill(market_id, token_id, question, category, side, size_usd,
                       fill_price, shares, fair_prob, confidence, thesis,
                       partial: bool) -> dict:
    pos_id = state.open_position("paper", market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, entry_price=fill_price,
        size_usd=size_usd, shares=shares, fair_prob=fair_prob,
        confidence=confidence, thesis=thesis)
    result = {"status": "filled", "mode": "paper", "position_id": pos_id,
              "side": side, "fill_price": fill_price, "shares": shares,
              "size_usd": size_usd}
    if partial:
        result["partial"] = True
    _notify_fill("paper", question, side, fill_price, shares, size_usd)
    return result


def _notify_fill(mode, question, side, fill_price, shares, size_usd) -> None:
    """Best-effort fill notification. No-ops when no channel is configured (so
    it adds zero network in tests) and never raises — a notify failure must not
    affect order execution."""
    try:
        notifications.notify(
            "fill",
            f"{side} {question} @ {fill_price} "
            f"({shares} sh, ${size_usd}, {mode})",
            mode=mode, side=side, question=question, fill_price=fill_price,
            shares=shares, size_usd=size_usd)
    except Exception:
        pass


_clob_client = None


def _get_clob_client():
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    from py_clob_client.client import ClobClient
    client = ClobClient(host=config.POLYMARKET_HOST,
                        key=config.POLYMARKET_PRIVATE_KEY, chain_id=137,
                        signature_type=config.POLYMARKET_SIGNATURE_TYPE,
                        funder=config.POLYMARKET_PROXY_ADDRESS or None)
    client.set_api_creds(client.create_or_derive_api_creds())
    _clob_client = client
    return client


def _live_fill(market_id, token_id, question, category, side, size_usd,
               fair_prob, confidence, thesis, order_type="market",
               limit_price=None) -> dict:
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"status": "rejected", "reason": "POLYMARKET_PRIVATE_KEY not set"}
    if order_type == "limit":
        return _live_limit_fill(market_id, token_id, question, category, side,
                                size_usd, fair_prob, confidence, thesis,
                                limit_price)
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    client = _get_clob_client()
    amount = max(round(size_usd, 2), 1.0)
    args = MarketOrderArgs(token_id=token_id, amount=amount, side="BUY")
    try:
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FAK)
    except Exception as e:
        msg = str(e)
        if "no orders found to match" in msg or "FAK" in msg:
            return {"status": "rejected", "reason": "no live liquidity"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    if resp.get("status") not in ("matched", "delayed"):
        return {"status": "error", "reason": f"order status {resp.get('status')}"}
    taking = float(resp.get("takingAmount") or 0)   # shares received
    making = float(resp.get("makingAmount") or 0)   # usdc spent
    # No-fill guard: a 'matched'/'delayed' status can still carry zero shares
    # (e.g. sports markets that delay before matching). Do NOT record a zombie
    # position with 0 shares — mirrors executor.py's `if taking and taking != "0"`.
    if not taking:
        return {"status": "no_fill",
                "reason": "order matched/delayed but no fill amount yet",
                "order_id": resp.get("orderID", "")}
    shares = round(taking, 2)
    fill_price = round(making / taking, 4)
    pos_id = state.open_position("live", market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, entry_price=fill_price,
        size_usd=making or amount, shares=shares, fair_prob=fair_prob,
        confidence=confidence, thesis=thesis)
    _notify_fill("live", question, side, fill_price, shares, making or amount)
    return {"status": "filled", "mode": "live", "position_id": pos_id,
            "order_id": resp.get("orderID", ""), "side": side,
            "fill_price": fill_price, "shares": shares}


def _live_limit_fill(market_id, token_id, question, category, side, size_usd,
                     fair_prob, confidence, thesis, limit_price) -> dict:
    """Place a GTC limit BUY at `limit_price` via py-clob-client. Unlike a FAK
    market order, a GTC limit order may rest on the book unfilled, so we record
    the position from the resting size (size_usd / limit_price) rather than from
    immediate fill amounts. Guards the import: if the installed client lacks the
    limit-order API, returns a clear error rather than raising."""
    if limit_price is None or limit_price <= 0:
        return {"status": "rejected",
                "reason": "limit order requires a positive limit_price"}
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
    except ImportError as e:
        return {"status": "error",
                "reason": f"py-clob-client limit API unavailable: {e}"}
    client = _get_clob_client()
    # size is in shares for a limit order; spend size_usd at limit_price.
    shares = round(size_usd / limit_price, 2)
    if shares <= 0:
        return {"status": "rejected", "reason": "limit order size rounds to 0 shares"}
    try:
        args = OrderArgs(token_id=token_id, price=round(limit_price, 4),
                         size=shares, side="BUY")
        signed = client.create_order(args)
        resp = client.post_order(signed, OrderType.GTC)
    except AttributeError as e:
        # The installed client exposes neither create_order nor GTC posting.
        return {"status": "error",
                "reason": f"py-clob-client lacks limit order support: {e}"}
    except Exception as e:
        msg = str(e)
        if "no orders found to match" in msg or "FAK" in msg:
            return {"status": "rejected", "reason": "no live liquidity"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    status = resp.get("status", "")
    if status not in ("matched", "delayed", "live"):
        return {"status": "error", "reason": f"order status {status}"}
    # A GTC order can fill immediately (matched), rest (live), or delay. Record
    # the position at the limit price for the intended size; if it filled
    # immediately, prefer the reported fill amounts.
    taking = float(resp.get("takingAmount") or 0)   # shares received
    making = float(resp.get("makingAmount") or 0)   # usdc spent
    if taking:
        shares = round(taking, 2)
        fill_price = round(making / taking, 4)
        spend = making or size_usd
    else:
        fill_price = round(limit_price, 4)
        spend = round(shares * limit_price, 2)
    pos_id = state.open_position("live", market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, entry_price=fill_price,
        size_usd=spend, shares=shares, fair_prob=fair_prob,
        confidence=confidence, thesis=thesis)
    _notify_fill("live", question, side, fill_price, shares, spend)
    return {"status": "filled", "mode": "live", "position_id": pos_id,
            "order_id": resp.get("orderID", ""), "side": side,
            "fill_price": fill_price, "shares": shares, "order_type": "limit",
            "resting": not taking}


def get_live_balance() -> dict:
    """Fetch the live USDC (collateral) balance from the CLOB. Returns
    ``{"balance_usd": float}`` on success or ``{"error": str}`` when no key is
    configured or the call fails. Ported/adapted from executor.get_live_balance,
    returning a structured dict instead of a bare float|None."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"error": "POLYMARKET_PRIVATE_KEY not set", "balance_usd": None}
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        client = _get_clob_client()
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance_usd = int(bal.get("balance", "0")) / 1e6
        return {"balance_usd": round(balance_usd, 2)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "balance_usd": None}


def _fetch_live_positions() -> tuple[list[dict], str | None]:
    """Fetch + aggregate on-chain positions, distinguishing a genuine empty
    result from a fetch failure. Returns ``(positions, error)`` where ``error``
    is ``None`` on success (including a genuine zero-position account) or a short
    string describing why the fetch failed. Requires a private key.

    This is the failure-aware core that ``sync_live_positions`` uses; the public
    ``get_live_positions`` wraps it to preserve its list-only contract."""
    try:
        from collections import defaultdict
        client = _get_clob_client()
        trades = client.get_trades()
        if not isinstance(trades, list):
            return [], f"unexpected get_trades response type: {type(trades).__name__}"
        positions = defaultdict(
            lambda: {"bought": 0.0, "sold": 0.0, "cost": 0.0, "asset_id": "",
                     "market_id": "", "outcome": ""})
        for t in trades:
            market = t.get("market", "")
            outcome = t.get("outcome", "")
            side_str = t.get("side", "")
            try:
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
            except (TypeError, ValueError):
                continue
            asset_id = t.get("asset_id", "")
            key = f"{market}_{outcome}"
            if side_str == "BUY":
                positions[key]["bought"] += size
                positions[key]["cost"] += size * price
                if asset_id:
                    positions[key]["asset_id"] = asset_id
            elif side_str == "SELL":
                positions[key]["sold"] += size
            positions[key]["market_id"] = market
            positions[key]["outcome"] = outcome
        result = []
        for pos in positions.values():
            net = pos["bought"] - pos["sold"]
            if net > 0.01:
                avg_price = pos["cost"] / pos["bought"] if pos["bought"] > 0 else 0
                result.append({
                    "market_id": pos["market_id"],
                    "outcome": pos["outcome"],
                    "shares": round(net, 2),
                    "avg_price": round(avg_price, 4),
                    "cost": round(pos["cost"], 2),
                    "asset_id": pos["asset_id"],
                })
        return result, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def get_live_positions() -> list[dict]:
    """Aggregate net open positions from on-chain trade history via the CLOB
    ``get_trades`` endpoint. Returns a list of dicts with market_id, outcome,
    shares, avg_price, cost, asset_id. Empty list if no key or on error.
    Ported/adapted from executor.get_live_positions.

    NOTE: this swallows fetch errors as an empty list for back-compat; callers
    that must distinguish failure from a genuinely empty account (e.g.
    ``sync_live_positions``) should use ``_fetch_live_positions`` instead."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return []
    positions, _error = _fetch_live_positions()
    return positions


def sync_live_positions(mode: str = "live") -> dict:
    """Compare on-chain positions to the locally-tracked open positions and
    REPORT discrepancies WITHOUT mutating any state. Returns a structured diff:

      - ``on_chain_only``: markets held on-chain but not tracked locally
      - ``local_only``: markets tracked locally but not held on-chain
      - ``matched``: markets present in both
      - ``in_sync``: True iff the on-chain fetch SUCCEEDED and there are no
        discrepancies

    Crucially, ``in_sync`` is never optimistically True when the chain state is
    unknown:
      - No ``POLYMARKET_PRIVATE_KEY`` -> returns an ``error`` with
        ``in_sync: False`` (we cannot read the chain, so we cannot claim sync).
      - On-chain fetch failure -> surfaces ``fetch_error`` with ``in_sync: False``
        rather than treating the failure as a (misleading) empty chain.
    A genuinely empty on-chain account with no local positions IS reported as
    ``in_sync: True``.
    """
    local = state.get_positions(mode, status="open")

    # Cannot read the chain without a key: do NOT claim in_sync.
    if not config.POLYMARKET_PRIVATE_KEY:
        return {
            "mode": mode,
            "error": "POLYMARKET_PRIVATE_KEY not set",
            "in_sync": False,
            "on_chain_count": 0,
            "local_count": len(local),
            "on_chain_only": [],
            "local_only": [],
            "matched": [],
        }

    on_chain, fetch_error = _fetch_live_positions()
    # A failed fetch is NOT an empty chain: report it and refuse to claim sync.
    if fetch_error is not None:
        return {
            "mode": mode,
            "fetch_error": fetch_error,
            "in_sync": False,
            "on_chain_count": 0,
            "local_count": len(local),
            "on_chain_only": [],
            "local_only": [],
            "matched": [],
        }

    on_chain_by_market: dict[str, dict] = {}
    for p in on_chain:
        on_chain_by_market[str(p.get("market_id", ""))] = p
    local_by_market: dict[str, list[dict]] = {}
    for p in local:
        local_by_market.setdefault(str(p.get("market_id", "")), []).append(p)

    on_chain_keys = set(on_chain_by_market)
    local_keys = set(local_by_market)

    on_chain_only = []
    for mid in sorted(on_chain_keys - local_keys):
        p = on_chain_by_market[mid]
        on_chain_only.append({
            "market_id": mid, "outcome": p.get("outcome", ""),
            "shares": p.get("shares"), "avg_price": p.get("avg_price"),
        })
    local_only = []
    for mid in sorted(local_keys - on_chain_keys):
        for p in local_by_market[mid]:
            local_only.append({
                "market_id": mid, "position_id": p.get("id"),
                "side": p.get("side"), "shares": p.get("shares"),
            })
    matched = sorted(on_chain_keys & local_keys)

    return {
        "mode": mode,
        "in_sync": not on_chain_only and not local_only,
        "on_chain_count": len(on_chain),
        "local_count": len(local),
        "on_chain_only": on_chain_only,
        "local_only": local_only,
        "matched": matched,
    }


def get_open_orders() -> dict:
    """Fetch resting/open (unfilled or partially-filled) orders from the CLOB.

    Returns ``{"orders": [ {order_id, market, token_id, side, price, size,
    size_matched, outcome, order_type, status}, ... ]}`` on success, or
    ``{"error": str}`` when no private key is configured or the call fails.
    Never raises. Requires a private key (orders are per-API-key, Level-2 auth).

    The CLOB ``get_orders`` returns raw order dicts whose field names vary by
    build; we read each field defensively (e.g. ``id``/``orderID`` for the id,
    ``asset_id``/``token_id`` for the token) so a schema drift degrades to a
    missing value rather than an exception."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"error": "POLYMARKET_PRIVATE_KEY not set"}
    try:
        client = _get_clob_client()
        raw = client.get_orders()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    # A genuinely empty book (no resting orders) is success, not an error.
    if not raw:
        return {"orders": []}
    if not isinstance(raw, list):
        return {"error": f"unexpected get_orders response type: {type(raw).__name__}"}
    orders = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        orders.append({
            "order_id": o.get("id") or o.get("orderID") or o.get("order_id") or "",
            "market": o.get("market", ""),
            "token_id": o.get("asset_id") or o.get("token_id") or "",
            "side": o.get("side", ""),
            "price": o.get("price"),
            "size": o.get("original_size") or o.get("size"),
            "size_matched": o.get("size_matched"),
            "outcome": o.get("outcome", ""),
            "order_type": o.get("order_type") or o.get("type") or "",
            "status": o.get("status", ""),
        })
    return {"orders": orders}


def cancel_order(order_id: str) -> dict:
    """Cancel a single resting order on the CLOB by id. Returns
    ``{"status": "cancelled", "order_id": ...}`` on success or
    ``{"status": "error", "reason": ...}`` otherwise. Never raises. Requires a
    private key (Level-2 auth). Guards the import + missing key + any client
    exception so a flaky cancel can't crash the caller.

    Note the CLOB ``cancel`` response may report individual orders that could
    not be cancelled (e.g. already filled) under ``not_canceled``; we surface
    that detail but still treat a non-erroring call as ``cancelled``."""
    if not isinstance(order_id, str) or not order_id.strip():
        return {"status": "error", "reason": "order_id must be a non-empty string"}
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"status": "error", "reason": "POLYMARKET_PRIVATE_KEY not set"}
    try:
        client = _get_clob_client()
        resp = client.cancel(order_id)
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    result = {"status": "cancelled", "order_id": order_id}
    # Surface any detail the CLOB returns (e.g. canceled/not_canceled maps).
    if isinstance(resp, dict):
        not_canceled = resp.get("not_canceled")
        if not_canceled:
            result["not_canceled"] = not_canceled
    return result


def import_live_positions(mode: str = "live") -> dict:
    """ADOPT on-chain positions that are not yet tracked locally into local
    state. Where ``sync_live_positions`` only REPORTS drift, this RECONCILES it:
    for each on-chain position whose market is not already an open local
    position, it calls ``state.adopt_live_position`` (which inserts an open row
    WITHOUT debiting cash or logging a buy trade — see its docstring). This is
    how live position tracking survives a restart.

    Returns ``{"adopted": n, "skipped": n, "details": [...]}`` on success, or an
    ``error``/``fetch_error`` form with ``adopted: 0`` when the chain cannot be
    read. Requires a private key. Never raises on the live fetch (it goes
    through the failure-aware ``_fetch_live_positions``)."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"error": "POLYMARKET_PRIVATE_KEY not set", "adopted": 0, "skipped": 0}

    on_chain, fetch_error = _fetch_live_positions()
    if fetch_error is not None:
        return {"fetch_error": fetch_error, "adopted": 0, "skipped": 0}

    # Markets already tracked as open locally -> skip (idempotent / no clobber).
    local_markets = {str(p.get("market_id", ""))
                     for p in state.get_positions(mode, status="open")}

    adopted = 0
    skipped = 0
    details = []
    for p in on_chain:
        market_id = str(p.get("market_id", ""))
        if market_id in local_markets:
            skipped += 1
            continue
        shares = p.get("shares") or 0.0
        avg_price = p.get("avg_price") or 0.0
        cost = p.get("cost")
        size_usd = cost if cost is not None else round(shares * avg_price, 2)
        outcome = p.get("outcome", "")
        state.adopt_live_position(
            mode, market_id=market_id, token_id=p.get("asset_id", ""),
            question=outcome or market_id, category="", side=outcome,
            entry_price=avg_price, size_usd=size_usd, shares=shares,
            thesis="adopted from on-chain position")
        adopted += 1
        details.append({"market_id": market_id, "outcome": outcome,
                        "shares": shares, "avg_price": avg_price})
    return {"adopted": adopted, "skipped": skipped, "details": details}


def preflight_live() -> dict:
    """Pre-flight checklist for live trading. Never raises. Returns
    ``{"ok": bool, "checks": {...}}`` where each check is a boolean (or a small
    detail). ``ok`` is True only when every critical check passes."""
    checks: dict = {}

    # 1. Private key configured?
    checks["private_key_set"] = bool(config.POLYMARKET_PRIVATE_KEY)

    # 2. CLOB client creds derivable? (only attempt if a key is present)
    creds_ok = False
    creds_detail = "skipped: no private key"
    if checks["private_key_set"]:
        try:
            _get_clob_client()
            creds_ok = True
            creds_detail = "ok"
        except Exception as e:
            creds_detail = f"{type(e).__name__}: {e}"
    checks["clob_creds_derivable"] = creds_ok
    checks["clob_creds_detail"] = creds_detail

    # 3. Balance > 0?
    balance_positive = False
    balance_usd = None
    if checks["private_key_set"]:
        try:
            bal = get_live_balance()
            balance_usd = bal.get("balance_usd")
            balance_positive = bool(balance_usd and balance_usd > 0)
        except Exception:
            balance_positive = False
    checks["balance_positive"] = balance_positive
    checks["balance_usd"] = balance_usd

    # 4. Kill-switch limits configured (both > 0)?
    checks["kill_switch_configured"] = bool(
        config.KILL_SWITCH_DAILY_USD > 0 and config.KILL_SWITCH_TOTAL_USD > 0)
    checks["kill_switch_daily_usd"] = config.KILL_SWITCH_DAILY_USD
    checks["kill_switch_total_usd"] = config.KILL_SWITCH_TOTAL_USD

    ok = (checks["private_key_set"] and checks["clob_creds_derivable"]
          and checks["balance_positive"] and checks["kill_switch_configured"])
    return {"ok": ok, "checks": checks}


def _resolved_price(market_id: str, token_id: str) -> float | None:
    """Return the resolved payout for the held token: 1.0 if it won, 0.0 if it
    lost, or None if the market has NOT resolved yet.

    Resolution is gated on the market's ``closed`` flag from the CLOB API. A
    cheap (or expensive) price on an OPEN market is NOT a resolution — it just
    means the market thinks the outcome is unlikely (or likely). Settling on
    price alone would book every cheap position as an instant loss. Only once
    the market is closed do we read the held token's final price (which settles
    to ~0 or ~1)."""
    try:
        resp = httpx.get(f"{config.POLYMARKET_HOST}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    if not data.get("closed", False):
        return None
    for t in data.get("tokens", []):
        if str(t.get("token_id")) == str(token_id):
            try:
                final = float(t.get("price", 0.5))
            except (TypeError, ValueError):
                return None
            return 1.0 if final >= 0.5 else 0.0
    return None


def reconcile_positions(mode: str | None = None) -> dict:
    """Detect resolved markets among open positions and settle them, updating
    PnL. Returns a summary. Call this at the start of each cycle."""
    mode = mode or config.MODE
    settled = 0
    details = []
    for pos in state.get_positions(mode, status="open"):
        price = _resolved_price(pos["market_id"], pos["token_id"])
        if price is None:
            continue
        payout = pos["shares"] * price
        r = state.close_position(mode, pos["id"], exit_price=price, payout=payout)
        settled += 1
        # Best-effort: backfill the matching logged evaluation's outcome so the
        # track record reflects how this market resolved. A missing intelligence
        # row (returns False) must not break reconciliation.
        if price >= 1.0:
            outcome_side = pos["side"]              # the held outcome won
        elif pos["side"] in ("YES", "NO"):
            outcome_side = "NO" if pos["side"] == "YES" else "YES"
        else:
            outcome_side = ""                       # multi-outcome loss: winner unknown
        state.update_evaluation_outcome(mode, pos["market_id"],
            outcome_side=outcome_side, resolution_price=price,
            pnl=r.get("realized_pnl"))
        details.append({"position_id": pos["id"], "question": pos["question"],
                        "result": "won" if price >= 1.0 else "lost",
                        "realized_pnl": r.get("realized_pnl")})
    # Best-effort breaker alert: if settling losses just tripped the kill-switch,
    # send a single "breaker" notify. No-ops when unconfigured; never raises.
    if settled:
        ok, reason = risk.check_kill_switch(mode)
        if not ok:
            try:
                notifications.notify("breaker", f"Kill switch tripped: {reason}",
                                     mode=mode, reason=reason)
            except Exception:
                pass
    return {"settled": settled, "details": details}


def _live_sell(token_id: str, shares: float) -> dict:
    """Sell `shares` of a CLOB token via a FAK SELL market order.
    Ported from executor.py `sell_position`. Returns a dict with `status` and,
    on success, `shares_sold` + `usdc_received`. Does NOT touch local state."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"status": "error", "reason": "POLYMARKET_PRIVATE_KEY not set",
                "shares_sold": 0.0}
    import math

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    client = _get_clob_client()

    # Query the actual token balance to avoid "not enough balance" errors;
    # never try to sell more than we actually hold.
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id))
        actual_balance = float(bal.get("balance", "0")) / 1e6
        if 0 < actual_balance < shares:
            shares = actual_balance
    except Exception:
        pass  # fall through with the requested share count

    sell_shares = math.floor(shares * 100) / 100  # floor to 2dp, never exceed balance
    if sell_shares < 0.1:
        return {"status": "error", "reason": "sell size below minimum (0.1 shares)",
                "shares_sold": 0.0}

    args = MarketOrderArgs(token_id=token_id, amount=sell_shares, side="SELL")
    try:
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FAK)
    except Exception as e:
        msg = str(e)
        if "no orders found to match" in msg or "FAK" in msg:
            return {"status": "no_liquidity", "reason": "no buyer liquidity to sell into",
                    "shares_sold": 0.0}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}",
                "shares_sold": 0.0}
    if resp.get("status") not in ("matched", "delayed"):
        return {"status": "error", "reason": f"sell order status {resp.get('status')}",
                "shares_sold": 0.0}
    taking = float(resp.get("takingAmount") or 0)   # usdc received for a SELL
    if not taking:
        return {"status": "no_liquidity", "reason": "sell matched but no proceeds yet",
                "shares_sold": 0.0}
    return {"status": "sold", "shares_sold": sell_shares, "usdc_received": taking,
            "order_id": resp.get("orderID", "")}


def close_position(mode: str | None = None, *, position_id: int) -> dict:
    """Sell an open position at the current best bid (paper) or via the CLOB (live)."""
    mode = mode or config.MODE
    positions = {p["id"]: p for p in state.get_positions(mode, status="open")}
    pos = positions.get(position_id)
    if pos is None:
        return {"status": "rejected", "reason": f"position {position_id} not open"}

    if mode == "paper":
        book = order_book.fetch_order_book(pos["token_id"])
        bid = book.get("best_bid")
        if bid is None:
            return {"status": "rejected", "reason": "no bid liquidity to sell into"}
        payout = pos["shares"] * bid
        r = state.close_position(mode, position_id, exit_price=bid, payout=payout)
        if "error" in r:
            return {"status": "rejected", **r}
        return {"status": "closed", "exit_price": bid, **r}

    # Live mode: actually sell on the CLOB, then settle locally with real proceeds.
    sale = _live_sell(pos["token_id"], pos["shares"])
    if sale["status"] != "sold":
        return {"status": sale["status"], "reason": sale.get("reason", "")}
    shares_sold = sale["shares_sold"]
    proceeds = sale["usdc_received"]
    exit_price = round(proceeds / shares_sold, 4) if shares_sold else 0.0
    r = state.close_position(mode, position_id, exit_price=exit_price, payout=proceeds)
    if "error" in r:
        return {"status": "rejected", **r}
    return {"status": "closed", "exit_price": exit_price, **r}
