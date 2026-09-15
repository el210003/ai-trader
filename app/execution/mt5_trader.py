"""MT5 live trading: symbol info, position sizing, order placement, positions.

Wraps the raw MetaTrader5 python API with the broker plumbing live execution
actually needs:

  - attaching symbols that are not yet in Market Watch
  - detecting an allowed order-filling mode (FOK / IOC / RETURN)
  - risk-percent position sizing from the stop-loss distance
  - volume / price normalization to broker constraints
  - magic-number tagging so the bot only ever touches ITS OWN trades
  - margin pre-check before sending

Every function takes the initialized `mt5` module as first argument (same
style as app/data/mt5_client.py) so callers share one connection and tests
can pass a stub. Numeric constants below are the stable MT5 API values;
they are resolved through getattr() so the module imports even without the
MetaTrader5 package installed (e.g. non-Windows dev machines).
"""
import json
import math
import time

# ---------------------------------------------------------------- constants
# order types
_ORDER_TYPE_BUY = 0
_ORDER_TYPE_SELL = 1
_ORDER_TYPE_BUY_LIMIT = 2
_ORDER_TYPE_SELL_LIMIT = 3
# filling policy
_ORDER_FILLING_FOK = 0
_ORDER_FILLING_IOC = 1
_ORDER_FILLING_RETURN = 2
_SYMBOL_FILLING_FOK = 1          # symbol_info().filling_mode bitmask flags
_SYMBOL_FILLING_IOC = 2
# time in force
_ORDER_TIME_GTC = 0
_ORDER_TIME_SPECIFIED = 2
# trade actions
_TRADE_ACTION_DEAL = 1           # market order / close
_TRADE_ACTION_REMOVE = 3         # delete pending order
_TRADE_ACTION_PENDING = 5        # place pending order
# retcodes
_RETCODE_DONE = 10009
_RETCODE_PLACED = 10008
_RETCODE_DONE_PARTIAL = 10010
_RETCODE_REQUOTE = 10004
_RETCODE_PRICE_CHANGED = 10020
_RETCODE_PRICE_OFF = 10021
_RETCODE_REJECT = 10006

_RETCODE_TEXT = {
    10004: "requote", 10006: "rejected by broker", 10007: "canceled by trader",
    10008: "order placed", 10009: "done", 10010: "done (partial)",
    10011: "request error", 10012: "timeout", 10013: "invalid request",
    10014: "invalid volume", 10015: "invalid price", 10016: "invalid stops",
    10017: "trading disabled", 10018: "market closed", 10019: "insufficient funds",
    10020: "price changed", 10021: "no quotes", 10022: "invalid expiration",
    10023: "order changed", 10024: "too many requests", 10025: "no changes",
    10026: "autotrading disabled by server", 10027: "autotrading disabled by terminal",
    10028: "order locked", 10029: "order frozen", 10030: "invalid fill type",
    10031: "no connection", 10032: "demo account only", 10033: "pending-order limit",
    10034: "volume limit",
}

# symbol_info().trade_mode values
_TRADE_MODE_DISABLED = 0
_TRADE_MODE_LONGONLY = 1
_TRADE_MODE_SHORTONLY = 2
_TRADE_MODE_CLOSEONLY = 3
_TRADE_MODE_FULL = 4

_OK_RETCODES = (_RETCODE_DONE, _RETCODE_PLACED, _RETCODE_DONE_PARTIAL)
_TRANSIENT_RETCODES = (_RETCODE_REQUOTE, _RETCODE_PRICE_CHANGED, _RETCODE_PRICE_OFF)


def retcode_text(code) -> str:
    try:
        return _RETCODE_TEXT.get(int(code), f"retcode {code}")
    except (TypeError, ValueError):
        return f"retcode {code}"


# ------------------------------------------------------------ symbol facts
def ensure_symbol(mt5, symbol: str) -> bool:
    """Make sure the symbol is selectable in the terminal (adds it to
    Market Watch if needed). Returns True when symbol_info is available."""
    info = mt5.symbol_info(symbol)
    if info is not None and info.visible:
        return True
    return bool(mt5.symbol_select(symbol, True)) and mt5.symbol_info(symbol) is not None


def symbol_trade_info(mt5, symbol: str) -> dict:
    """Broker constraints for one symbol, normalized to plain floats."""
    if not ensure_symbol(mt5, symbol):
        raise RuntimeError(f"MT5: symbol {symbol} not available (add it to Market Watch)")
    info = mt5.symbol_info(symbol)
    point = float(info.point or 0.0)
    step = float(info.volume_step or 0.01)
    return {
        "symbol": symbol,
        "digits": int(info.digits),
        "point": point,
        "spread_points": int(info.spread or 0),
        "trade_mode": int(info.trade_mode),
        "trade_mode_text": {
            _TRADE_MODE_DISABLED: "disabled", _TRADE_MODE_LONGONLY: "long-only",
            _TRADE_MODE_SHORTONLY: "short-only", _TRADE_MODE_CLOSEONLY: "close-only",
            _TRADE_MODE_FULL: "full",
        }.get(int(info.trade_mode), f"mode {info.trade_mode}"),
        "filling_mode": int(info.filling_mode or 0),
        "volume_min": float(info.volume_min or 0.01),
        "volume_max": float(info.volume_max or 100.0),
        "volume_step": step,
        "tick_value": float(info.trade_tick_value or 0.0),
        "tick_size": float(info.trade_tick_size or 0.0) or point,
        "contract_size": float(info.trade_contract_size or 0.0),
        "currency_profit": info.currency_profit,
    }


def pick_filling(mt5, info: dict) -> int:
    """Choose an order filling mode the broker actually accepts."""
    flags = int(info.get("filling_mode") or 0)
    if flags & _SYMBOL_FILLING_FOK:
        return _ORDER_FILLING_FOK
    if flags & _SYMBOL_FILLING_IOC:
        return _ORDER_FILLING_IOC
    return _ORDER_FILLING_RETURN


# ------------------------------------------------------------- normalization
def volume_decimals(step: float) -> int:
    d = 0
    s = step
    while d < 8 and abs(s - round(s, d)) > 1e-10:
        d += 1
    return d


def normalize_lot(lot: float, info: dict) -> float:
    """Floor to the broker's volume step and clamp into [min, max]."""
    step = info["volume_step"] or 0.01
    lot = math.floor((lot + 1e-12) / step) * step
    lot = min(lot, info["volume_max"])
    return round(lot, volume_decimals(step))


def normalize_price(price: float, info: dict) -> float:
    return round(float(price), int(info["digits"]))


# ------------------------------------------------------------------- sizing
def calc_lot(mt5, symbol: str, entry: float, sl: float, risk_percent: float,
             info: dict = None, balance: float = None) -> float:
    """Lot size so that hitting the stop loses `risk_percent`% of balance.

    loss_at_sl = |entry - sl| * (tick_value / tick_size) * lot   [account ccy]
    """
    info = info or symbol_trade_info(mt5, symbol)
    dist = abs(float(entry) - float(sl))
    if dist <= 0:
        raise ValueError("stop-loss distance is zero")
    if balance is None:
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError("MT5: account_info() returned None (logged in?)")
        balance = float(acc.balance)
    tick_size = info["tick_size"] or info["point"]
    tick_value = info["tick_value"]
    if tick_size <= 0 or tick_value <= 0:
        raise RuntimeError(
            f"MT5: {symbol} has no tick value info -- cannot size by risk; "
            "set execution.fixed_lot instead")
    value_per_unit = tick_value / tick_size          # account ccy per 1.0 price move per lot
    risk_amount = max(0.0, balance) * float(risk_percent) / 100.0
    if risk_amount <= 0:
        raise ValueError("risk_amount <= 0 (check execution.risk_percent / balance)")
    return risk_amount / (dist * value_per_unit)


def margin_available(mt5, info: dict, direction: str, lot: float, price: float) -> tuple:
    """(margin_required, margin_free) or (None, None) when not computable."""
    try:
        otype = _ORDER_TYPE_BUY if direction == "long" else _ORDER_TYPE_SELL
        need = mt5.order_calc_margin(otype, info["symbol"], float(lot), float(price))
        acc = mt5.account_info()
        if need is None or acc is None:
            return None, None
        return float(need), float(acc.margin_free)
    except Exception:
        return None, None


# ----------------------------------------------------------------- querying
def bot_positions(mt5, magic: int, symbol: str = None) -> list:
    """Open positions tagged with our magic number (never touches manual trades)."""
    rows = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    return [p for p in (rows or []) if int(getattr(p, "magic", 0)) == int(magic)]


def bot_pendings(mt5, magic: int, symbol: str = None) -> list:
    """Pending (limit) orders tagged with our magic number."""
    rows = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    return [o for o in (rows or []) if int(getattr(o, "magic", 0)) == int(magic)]


def position_dict(p) -> dict:
    return {
        "ticket": int(p.ticket), "symbol": p.symbol,
        "type": "long" if int(p.type) == _ORDER_TYPE_BUY else "short",
        "volume": float(p.volume), "price_open": float(p.price_open),
        "sl": float(p.sl or 0.0), "tp": float(p.tp or 0.0),
        "profit": float(p.profit), "magic": int(p.magic),
        "comment": getattr(p, "comment", ""),
        "time": int(p.time),
    }


def order_dict(o) -> dict:
    return {
        "ticket": int(o.ticket), "symbol": o.symbol,
        "type": {0: "buy", 1: "sell", 2: "buy-limit", 3: "sell-limit"}.get(int(o.type), str(o.type)),
        "volume": float(o.volume_current), "price_open": float(o.price_open),
        "sl": float(o.sl or 0.0), "tp": float(o.tp or 0.0),
        "magic": int(o.magic), "comment": getattr(o, "comment", ""),
        "time_setup": int(o.time_setup),
    }


def account_snapshot(mt5) -> dict:
    acc = mt5.account_info()
    if acc is None:
        return {}
    return {
        "login": int(acc.login), "server": acc.server, "currency": acc.currency,
        "balance": round(float(acc.balance), 2), "equity": round(float(acc.equity), 2),
        "margin_free": round(float(acc.margin_free), 2),
        "leverage": int(acc.leverage), "trade_allowed": bool(acc.trade_allowed),
    }


def server_time(mt5, symbol: str = None) -> int:
    """Approximate broker-server epoch seconds (from the latest tick)."""
    tick = mt5.symbol_info_tick(symbol) if symbol else None
    if tick is None:
        return int(time.time())
    return int(tick.time or time.time())


# ------------------------------------------------------------- order sending
def _request_base(symbol: str, lot: float, otype: int, sl: float, tp: float,
                  magic: int, comment: str) -> dict:
    return {
        "symbol": symbol,
        "volume": float(lot),
        "type": int(otype),
        "sl": float(sl),
        "tp": float(tp),
        "magic": int(magic),
        "comment": (comment or "ai-trader")[:31],   # MT5 comment limit
        "type_time": _ORDER_TIME_GTC,
    }


def _result_dict(result) -> dict:
    if result is None:
        return {"ok": False, "retcode": None, "comment": "order_send returned None"}
    return {
        "ok": int(getattr(result, "retcode", 0)) in _OK_RETCODES,
        "retcode": int(getattr(result, "retcode", 0)),
        "comment": getattr(result, "comment", ""),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
        "volume": float(getattr(result, "volume", 0.0) or 0.0),
        "price": float(getattr(result, "price", 0.0) or 0.0),
    }


def send_market(mt5, symbol: str, direction: str, lot: float, sl: float, tp: float,
                magic: int, comment: str = "ai-trader", deviation: int = 20,
                max_retries: int = 2, info: dict = None) -> dict:
    """Market order with SL/TP attached. Retries requotes with a fresh price."""
    info = info or symbol_trade_info(mt5, symbol)
    otype = _ORDER_TYPE_BUY if direction == "long" else _ORDER_TYPE_SELL
    filling = pick_filling(mt5, info)
    last = None
    for attempt in range(max_retries + 1):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.ask or not tick.bid:
            return {"ok": False, "retcode": None, "comment": "no tick price available"}
        price = tick.ask if direction == "long" else tick.bid
        req = _request_base(symbol, lot, otype, sl, tp, magic, comment)
        req.update({
            "action": _TRADE_ACTION_DEAL,
            "price": normalize_price(price, info),
            "deviation": int(deviation),
            "type_filling": filling,
        })
        last = _result_dict(mt5.order_send(req))
        if last["ok"] or last["retcode"] not in _TRANSIENT_RETCODES:
            return last
        time.sleep(0.3)   # brief pause before re-quote retry
    return last


def send_limit(mt5, symbol: str, direction: str, lot: float, price: float,
               sl: float, tp: float, magic: int, comment: str = "ai-trader",
               info: dict = None) -> dict:
    """Pending limit order at `price` with SL/TP attached (GTC — the engine
    cancels stale pendings itself, which is portable across brokers)."""
    info = info or symbol_trade_info(mt5, symbol)
    otype = _ORDER_TYPE_BUY_LIMIT if direction == "long" else _ORDER_TYPE_SELL_LIMIT
    req = _request_base(symbol, lot, otype, sl, tp, magic, comment)
    req.update({
        "action": _TRADE_ACTION_PENDING,
        "price": normalize_price(price, info),
        "type_filling": _ORDER_FILLING_RETURN,   # pendings require RETURN on most brokers
    })
    return _result_dict(mt5.order_send(req))


def close_position(mt5, position, magic: int, comment: str = "ai-trader-close",
                   deviation: int = 20, info: dict = None) -> dict:
    """Fully close one open position by ticket (opposite market deal)."""
    info = info or symbol_trade_info(mt5, position.symbol)
    closing_buy = int(position.type) == _ORDER_TYPE_SELL   # close short -> buy
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return {"ok": False, "retcode": None, "comment": "no tick price available"}
    req = _request_base(position.symbol, float(position.volume),
                        _ORDER_TYPE_BUY if closing_buy else _ORDER_TYPE_SELL,
                        0.0, 0.0, magic, comment)
    req.update({
        "action": _TRADE_ACTION_DEAL,
        "position": int(position.ticket),
        "price": normalize_price(tick.ask if closing_buy else tick.bid, info),
        "deviation": int(deviation),
        "type_filling": pick_filling(mt5, info),
    })
    return _result_dict(mt5.order_send(req))


def cancel_order(mt5, ticket: int) -> dict:
    """Delete a pending order by ticket."""
    return _result_dict(mt5.order_send({"action": _TRADE_ACTION_REMOVE,
                                        "order": int(ticket)}))


def snapshot_payload(result: dict) -> str:
    """Compact JSON of an order_send result for the trades table."""
    try:
        return json.dumps(result or {}, default=str)
    except Exception:
        return "{}"
