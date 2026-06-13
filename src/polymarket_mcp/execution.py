"""Order execution. Paper mode simulates fills against the live book;
live mode routes through py-clob-client. Kill-switch is enforced here."""
from __future__ import annotations

from . import config, order_book, risk, state


def place_order(*, mode: str | None = None, market_id: str, token_id: str,
                question: str, category: str, side: str, size_usd: float,
                fair_prob: float | None = None, confidence: float | None = None,
                thesis: str = "", limit_price: float | None = None) -> dict:
    """Place a buy order for `side` ('YES'/'NO') on a market. Enforces the
    kill-switch first. In paper mode, fills against the live order book."""
    mode = mode or config.MODE
    if size_usd <= 0:
        return {"status": "rejected", "reason": "size_usd must be positive"}

    ok, reason = risk.check_kill_switch(mode)
    if not ok:
        return {"status": "rejected", "reason": reason}

    cash = state.get_portfolio(mode)["cash"]
    if size_usd > cash:
        return {"status": "rejected", "reason": f"insufficient cash ${cash:.2f}"}

    if mode == "paper":
        return _paper_fill(market_id, token_id, question, category, side,
                           size_usd, fair_prob, confidence, thesis, limit_price)
    return _live_fill(market_id, token_id, question, category, side, size_usd,
                      fair_prob, confidence, thesis)


def _paper_fill(market_id, token_id, question, category, side, size_usd,
                fair_prob, confidence, thesis, limit_price) -> dict:
    book = order_book.fetch_order_book(token_id)
    fill_price = book.get("best_ask")
    if fill_price is None:
        return {"status": "rejected", "reason": "no ask liquidity in paper book"}
    if limit_price is not None and fill_price > limit_price:
        return {"status": "rejected",
                "reason": f"ask {fill_price} above limit {limit_price}"}
    shares = round(size_usd / fill_price, 2)
    pos_id = state.open_position("paper", market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, entry_price=fill_price,
        size_usd=size_usd, shares=shares, fair_prob=fair_prob,
        confidence=confidence, thesis=thesis)
    return {"status": "filled", "mode": "paper", "position_id": pos_id,
            "side": side, "fill_price": fill_price, "shares": shares,
            "size_usd": size_usd}


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
               fair_prob, confidence, thesis) -> dict:
    if not config.POLYMARKET_PRIVATE_KEY:
        return {"status": "rejected", "reason": "POLYMARKET_PRIVATE_KEY not set"}
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


def _resolved_price(token_id: str, side: str) -> float | None:
    """Return 1.0 if this side won, 0.0 if it lost, None if unresolved.
    Uses the CLOB midpoint: >0.97 => resolved YES-token-wins, <0.03 => lost."""
    book = order_book.fetch_order_book(token_id)
    mid = None
    if book.get("best_bid") is not None and book.get("best_ask") is not None:
        mid = (book["best_bid"] + book["best_ask"]) / 2
    if mid is None:
        return None
    if mid >= 0.97:
        return 1.0
    if mid <= 0.03:
        return 0.0
    return None


def reconcile_positions(mode: str | None = None) -> dict:
    """Detect resolved markets among open positions and settle them, updating
    PnL. Returns a summary. Call this at the start of each cycle."""
    mode = mode or config.MODE
    settled = 0
    details = []
    for pos in state.get_positions(mode, status="open"):
        price = _resolved_price(pos["token_id"], pos["side"])
        if price is None:
            continue
        payout = pos["shares"] * price
        r = state.close_position(mode, pos["id"], exit_price=price, payout=payout)
        settled += 1
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
