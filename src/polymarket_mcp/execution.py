"""Order execution. Paper mode simulates fills against the live book;
live mode routes through py-clob-client. Kill-switch is enforced here."""
from __future__ import annotations

import httpx

from . import config, order_book, risk, state


def place_order(*, mode: str | None = None, market_id: str, token_id: str,
                question: str, category: str, side: str, size_usd: float,
                fair_prob: float | None = None, confidence: float | None = None,
                thesis: str = "", limit_price: float | None = None,
                order_type: str = "market") -> dict:
    """Place a buy order for `side` ('YES'/'NO') on a market. Enforces the
    kill-switch first. In paper mode, fills against the live order book (walking
    the asks for slippage). In live mode, `order_type` selects "market" (FAK) or
    "limit" (GTC at `limit_price`)."""
    mode = mode or config.MODE
    if size_usd <= 0:
        return {"status": "rejected", "reason": "size_usd must be positive"}

    # Input validation: side must be a recognized outcome (case-insensitive).
    if not isinstance(side, str) or side.strip().upper() not in {"YES", "NO"}:
        return {"status": "rejected",
                "reason": f"side must be 'YES' or 'NO', got {side!r}"}
    side = side.strip().upper()

    order_type = (order_type or "market").strip().lower()
    if order_type not in {"market", "limit"}:
        return {"status": "rejected",
                "reason": f"order_type must be 'market' or 'limit', got {order_type!r}"}

    ok, reason = risk.check_kill_switch(mode)
    if not ok:
        return {"status": "rejected", "reason": reason}

    cash = state.get_portfolio(mode)["cash"]
    if size_usd > cash:
        return {"status": "rejected", "reason": f"insufficient cash ${cash:.2f}"}

    # Closed-market guard: refuse to open into an already-resolved market.
    # LIVE-ONLY: the guard does a live status fetch, so gating it to live mode
    # keeps paper-mode (simulation) hermetic — paper candidates already come
    # from active markets and reconcile_positions settles resolved ones.
    if mode == "live" and _market_is_closed(market_id):
        return {"status": "rejected", "reason": "market is closed/resolved"}

    if mode == "paper":
        return _paper_fill(market_id, token_id, question, category, side,
                           size_usd, fair_prob, confidence, thesis, limit_price)
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
                fair_prob, confidence, thesis, limit_price) -> dict:
    book = order_book.fetch_order_book(token_id)
    asks = book.get("asks")

    # Fallback path: a book with no detailed `asks` ladder (e.g. a mock that only
    # returns best_ask/best_bid). Fill the whole size at best_ask, exactly as the
    # original implementation did — preserving existing behavior/tests.
    if not asks:
        fill_price = book.get("best_ask")
        if fill_price is None:
            return {"status": "rejected", "reason": "no ask liquidity in paper book"}
        if limit_price is not None and fill_price > limit_price:
            return {"status": "rejected",
                    "reason": f"ask {fill_price} above limit {limit_price}"}
        shares = round(size_usd / fill_price, 2)
        return _record_paper_fill(market_id, token_id, question, category, side,
            size_usd, fill_price, shares, fair_prob, confidence, thesis,
            partial=False)

    # Slippage-aware path: walk the asks (ascending price) spending USD until the
    # full size is consumed, accumulating a size-weighted average fill price.
    spent = 0.0
    shares_acc = 0.0
    remaining = size_usd
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

    partial = remaining > 1e-9
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
    return result


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
        fill_price = round(making / taking, 4) if taking else round(limit_price, 4)
        spend = making or size_usd
    else:
        fill_price = round(limit_price, 4)
        spend = round(shares * limit_price, 2)
    pos_id = state.open_position("live", market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, entry_price=fill_price,
        size_usd=spend, shares=shares, fair_prob=fair_prob,
        confidence=confidence, thesis=thesis)
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


def get_live_positions() -> list[dict]:
    """Aggregate net open positions from on-chain trade history via the CLOB
    ``get_trades`` endpoint. Returns a list of dicts with market_id, outcome,
    shares, avg_price, cost, asset_id. Empty list if no key or on error.
    Ported/adapted from executor.get_live_positions."""
    if not config.POLYMARKET_PRIVATE_KEY:
        return []
    try:
        from collections import defaultdict
        client = _get_clob_client()
        trades = client.get_trades()
        if not isinstance(trades, list):
            return []
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
        return result
    except Exception:
        return []


def sync_live_positions(mode: str = "live") -> dict:
    """Compare on-chain positions to the locally-tracked open positions and
    REPORT discrepancies WITHOUT mutating any state. Returns a structured diff:

      - ``on_chain_only``: markets held on-chain but not tracked locally
      - ``local_only``: markets tracked locally but not held on-chain
      - ``matched``: markets present in both
      - ``in_sync``: True iff there are no discrepancies

    Best-effort: if the live position fetch fails it surfaces an ``error`` key.
    """
    on_chain = get_live_positions()
    local = state.get_positions(mode, status="open")

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
        outcome_side = pos["side"] if price >= 1.0 else (
            "NO" if pos["side"] == "YES" else "YES")
        state.update_evaluation_outcome(mode, pos["market_id"],
            outcome_side=outcome_side, resolution_price=price,
            pnl=r.get("realized_pnl"))
        details.append({"position_id": pos["id"], "question": pos["question"],
                        "result": "won" if price >= 1.0 else "lost",
                        "realized_pnl": r.get("realized_pnl")})
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
