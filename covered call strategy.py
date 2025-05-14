import threading
import time
import math
import logging
import json
from types import SimpleNamespace
from pathlib import Path

from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from ib_insync import IB, MarketOrder, LimitOrder, Contract, Option, Future
from threading import Lock


# --- Generic JSON loader ---
def safe_json_load(path: Path, default):
    """
    Safely load JSON from a file, returning default if any error occurs.
    """
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


# File to persist baseline MES price across restarts
BASE_PRICE_FILE = Path(__file__).with_suffix('.base_mes_price.json')
# File to track daily and weekly roll counts
ROLL_COUNTS_FILE = Path(__file__).with_suffix('.roll_counts.json')

def load_base_mes_price():
    """
    Load the persisted baseline MES price, or return None if unavailable.
    """
    val = safe_json_load(BASE_PRICE_FILE, None)
    try:
        return float(val)
    except Exception:
        return None

def save_base_mes_price(price):
    try:
        BASE_PRICE_FILE.write_text(json.dumps(price))
        print(f"📦 Saved baseline MES price: {price:.2f}")
    except Exception as e:
        print(f"⚠️ Failed to save base MES price: {e}")

# --- Roll counts persistence ---
def load_roll_counts():
    """
    Load persisted roll counts, or initialize empty daily/weekly structure.
    """
    return safe_json_load(ROLL_COUNTS_FILE, {'daily': {}, 'weekly': {}})

def save_roll_counts(counts):
    try:
        ROLL_COUNTS_FILE.write_text(json.dumps(counts))
    except Exception as e:
        print(f"⚠️ Failed to save roll counts: {e}")
# Silence all ib_insync logging and IB errorEvent callbacks
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger('ib_insync').setLevel(logging.CRITICAL)

# ───────── CONFIG ─────────

IB_HOST          = '127.0.0.1'
IB_PORT          = 7001      # live trading gateway
CLIENT_ID        = 3

UNDERLYING       = 'MES'
STRIKE_STEP      = 1         # how many strikes to roll up/down each time
PROFIT_TARGET    = 30      # percent gain on premium to roll DOWN
LOSS_LIMIT       = -50     # percent loss on premium to roll UP
CHECK_INTERVAL   = 1         # seconds between PnL checks
MAX_SPREAD_TICKS = 5         # only trade if (ask–bid) ≤ this many ticks

MES_MOVE_UP_THRESHOLD   = 25   # points MES must RISE before a roll‑UP
MES_MOVE_DOWN_THRESHOLD = 15   # points MES must FALL before a roll‑DOWN

CANCELLATION_DELAY = 5  # seconds to wait for an option fill before cancelling

# ─── Real‑time summary state ───
summary_lock   = Lock()
summary_data   = {}   # will hold keys: price, strike, bid, ask, spread, fut_bid, fut_ask, cost, pnl, pnl_pct, down_left, up_left, pct_to_profit, pct_to_loss
summary_paused = False  # True while an order is working
skip_summary_count = 0  # cycles to skip after a fill

# ─── Summary print thresholds ───
LAST_PRINTED_PNL = None    # last printed P/L percent
LAST_PRINTED_SPREAD = None # last printed call spread
PNL_PRINT_THRESHOLD = 0.5  # percent change threshold to reprint summary
SPREAD_PRINT_THRESHOLD = 0.25  # dollar change threshold for spread
# ─────────────────────────────────

def get_expiry_and_future_expiry():
    ny = ZoneInfo('America/New_York')
    now = datetime.now(ny)
    if now.hour < 16:
        exp_date = now.date()
    else:
        exp_date = now.date() + timedelta(days=1)
        if exp_date.weekday() == 5:  # Saturday
            exp_date += timedelta(days=2)
        elif exp_date.weekday() == 6:  # Sunday
            exp_date += timedelta(days=1)
    expiry = exp_date.strftime('%Y%m%d')
    fut_expiry = exp_date.strftime('%Y%m')
    return expiry, fut_expiry

# ───────── HELPERS ─────────

# ─── Fetch MES futures midpoint at fill time ───
def fetch_mes_mid(ib):
    """
    Fetch the current MES futures midpoint (bid+ask)/2,
    with fallback to reqTickers if snapshot data is invalid.
    """
    expiry, fut_month = get_expiry_and_future_expiry()
    details = ib.reqContractDetails(Contract(
        symbol=UNDERLYING, secType='FUT', exchange='CME', currency='USD'
    ))
    if not details:
        raise Exception("No MES future contract found")
    fut_contract = details[0].contract
    ib.qualifyContracts(fut_contract)
    # Primary: snapshot via reqMktData
    ticker = ib.reqMktData(fut_contract, '', False, False)
    ib.sleep(0.2)
    bid, ask = ticker.bid, ticker.ask
    # Fallback if invalid bid/ask
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        tickers = ib.reqTickers(fut_contract)
        if tickers:
            tb = tickers[0]
            ib.sleep(0.1)
            bid = tb.bid or bid or 0.0
            ask = tb.ask or ask or bid or 0.0
    # Compute midpoint
    if bid is not None and ask is not None and ask > bid:
        mid = (bid + ask) / 2
        # quantize midpoint to 0.25 increments
        return round(mid * 4) / 4
    # As a last resort, use last or close price
    last = ticker.last or ticker.close or float('nan')
    # quantize midpoint to 0.25 increments
    last = round(last * 4) / 4
    return float(last)

def connect_ib():
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
    ib.errorEvent.clear()
    ib.errorEvent += lambda *args, **kwargs: None
    return ib

def get_mid_price(tick):
    """Compute midpoint from bid/ask."""
    if tick.bid is None or tick.ask is None:
        return None
    return (tick.bid + tick.ask) / 2

def find_atm_strike(ib, underlying_price, chain):
    """Find the strike in chain closest to the underlying price."""
    strikes = sorted(chain.strikes)
    return min(strikes, key=lambda s: abs(s - underlying_price))

def choose_option_contract(ib, strike_offset=0):
    """
    Retrieves and qualifies the at-the-money MES short call via option chain metadata.
    """
    # After 8 AM, switch to next trading day only if today is Monday–Thursday
    ny = ZoneInfo('America/New_York')
    now = datetime.now(ny)
    if now.hour >= 8 and now.weekday() in (0, 1, 2, 3):
        # next calendar day, skip weekends
        next_day = now.date() + timedelta(days=1)
        if next_day.weekday() == 5:  # Saturday
            next_day += timedelta(days=2)
        elif next_day.weekday() == 6:  # Sunday
            next_day += timedelta(days=1)
        expiry = next_day.strftime('%Y%m%d')
    else:
        # use today’s expiry (or next business day if past cutoff)
        expiry, _ = get_expiry_and_future_expiry()
    # Get the reliable MES midpoint for ATM calculation
    price = fetch_mes_mid(ib)
    # 2) Fetch all MES call options for today's expiry
    opt_filter = Contract(
        symbol=UNDERLYING,
        secType='FOP',
        exchange='CME',
        currency='USD',
        lastTradeDateOrContractMonth=expiry,
        right='C'
    )
    details = ib.reqContractDetails(opt_filter)
    if not details:
        raise Exception(f"No MES call options found for expiry {expiry}")
    # Determine the target strike nearest to the future mid-price and apply offset
    strikes = sorted({d.contract.strike for d in details})
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - price))
    target_idx = atm_idx + strike_offset
    target_idx = max(0, min(target_idx, len(strikes) - 1))
    best_strike = strikes[target_idx]
    # Remove or comment out the old candidates logic block
    # if candidates:
    #     best_strike = candidates[0]
    # else:
    #     best_strike = strikes[-1]
    # 4) Find the contract matching that strike
    for d in details:
        c = d.contract
        if getattr(c, 'strike', None) == best_strike:
            ib.qualifyContracts(c)
            return c
    raise Exception(f"Failed to find MES call at strike {best_strike}")


# ───────── Stepped Limit Order Helper ─────────
def place_stepped_limit(ib, contract, action, qty):
    """
    Place a stepped DAY limit order: start at midpoint, step toward bid (SELL) or ask (BUY)
    """
    # Ensure we're only placing orders on options
    if getattr(contract, 'secType', None) != 'FOP':
        print(f"⚠️ Skipping non-option contract: {getattr(contract, 'secType', 'UNKNOWN')}")
        return None
    # ─── Ensure only one outstanding limit order ───
    ib.reqOpenOrders()
    for order_data in ib.openOrders():
        o = order_data.order
        c = order_data.contract
        if getattr(c, 'conId', None) == getattr(contract, 'conId', None) \
           and o.action == action and getattr(o, 'orderType', '') == 'LMT':
            print(f"⚠️ Cancelling existing {action} LMT order for {contract.localSymbol} at {o.lmtPrice}")
            try:
                ib.cancelOrder(o)
            except:
                pass
            ib.sleep(1)  # allow cancellation to propagate
    # ───────────────────────────────────────────────
    # Prevent double-short: skip SELL if a short call already exists
    if action == 'SELL':
        ib.reqPositions()
        existing_shorts = [p for p in ib.positions()
                           if p.contract.secType == 'FOP'
                           and p.contract.symbol == UNDERLYING
                           and p.position < 0]
        if existing_shorts:
            print(f"⚠️ Skipping new short call; existing: {existing_shorts[0].contract.localSymbol}")
            return None
    # ─── Two-step IOC: threshold then NBBO fallback ───
    with summary_lock:
        summary_paused = True
    md = ib.reqMktData(contract, '', False, True)
    ib.sleep(0.5)  # allow snapshot to populate
    raw_bid = md.bid
    raw_ask = md.ask
    # Fallback if no valid NBBO: quick reqTickers()
    if raw_bid is None or math.isnan(raw_bid) or raw_bid <= 0 \
       or raw_ask is None or math.isnan(raw_ask) or raw_ask <= 0:
        ticker_fb = ib.reqTickers(contract)[0]
        ib.sleep(0.1)
        raw_bid = ticker_fb.bid or 0.0
        raw_ask = ticker_fb.ask or raw_bid
    bid, ask = raw_bid, raw_ask
    # Calculate threshold price: 0.25 above bid for SELL, 0.25 below ask for BUY
    threshold = 0.25
    if action == 'SELL':
        first_price = min(bid + threshold, ask)
    else:
        first_price = max(ask - threshold, bid)
    # 1) Place threshold GTC (explicitly cancel after delay if not filled)
    order1 = LimitOrder(action, qty, first_price, tif='GTC')
    print(f"📝 Placed {action} IOC threshold order at ${first_price:.2f}")
    trade1 = ib.placeOrder(contract, order1)
    # ─── Wait for threshold fill before fallback ───
    print(f"⏳ Waiting {CANCELLATION_DELAY}s for threshold to fill before fallback")
    ib.sleep(CANCELLATION_DELAY)
    try:
        ib.cancelOrder(order1)
        print(f"⚠️ Cancelled threshold order at ${first_price:.2f}")
    except:
        pass
    # Check fill
    filled1 = False
    if hasattr(trade1, 'fills') and trade1.fills:
        filled1 = True
    elif getattr(trade1, 'orderStatus', None) and trade1.orderStatus.status == 'Filled':
        filled1 = True
    if filled1:
        with summary_lock:
            summary_paused = False
        return trade1

    # 2) Fallback: use Market‑to‑Limit for guaranteed execution with price cap
    print(f"⚠️ Threshold did not fill; placing {action} Market‑to‑Limit fallback order")
    order2 = MarketOrder(action, qty)
    # Flag MTL conversion (IBKR converts first fill price to limit)
    order2.orderType = 'MKT'
    order2.convertToLimit = True
    trade2 = ib.placeOrder(contract, order2)
    # Allow brief time for execution
    ib.sleep(1)
    with summary_lock:
        summary_paused = False
    return trade2

def calc_pnl_percent(entry_price, current_price, multiplier=1):
    """
    PnL percent for a short call:
      profit when current < entry → positive
    """
    pnl = (entry_price - current_price) * multiplier
    cost = entry_price * multiplier
    return pnl / cost * 100

def roll_position(ib, old_trade, new_contract):
    """Closes old short call and opens a new one at market."""
    # 1) Close old
    close_order = MarketOrder('BUY', old_trade.order.totalQuantity)
    ib.placeOrder(old_trade.contract, close_order)
    ib.sleep(1)
    # 2) Open new
    return place_short_call(ib, new_contract)

def ensure_single_short_call(ib):
    """
    Ensures that there is at most one short call open.
    Closes any extras if more than one is found.
    """
    ib.reqPositions()
    positions = ib.positions()
    short_calls = [p for p in positions if p.contract.secType == 'FOP'
                   and p.contract.symbol == UNDERLYING and p.position < 0]
    # Close any extra short calls beyond the first
    if len(short_calls) > 1:
        for extra in short_calls[1:]:
            print(f"⚠️ Closing extra short call: {extra.contract.localSymbol}")
            ib.placeOrder(extra.contract, MarketOrder('BUY', abs(extra.position)))
            ib.sleep(1)

# ───────── MAIN LOOP ─────────

# ─── Real-time summary printer thread ───
def summary_thread():
    """Print latest summary_data every 2 s, but only full summary when MES price changes."""
    last_rt_price = None
    waiting_printed = False
    global skip_summary_count
    while True:
        time.sleep(2)
        ts = datetime.now(ZoneInfo('America/New_York')).strftime('%H:%M:%S')
        with summary_lock:
            if skip_summary_count > 0:
                skip_summary_count -= 1
                continue
            if summary_paused or not summary_data:
                continue
            expected_keys = {'mes_rt_price','price','strike','bid','ask','spread',
                             'fut_bid','fut_ask','cost','pnl','pnl_pct','cash',
                             'pct_to_profit','pct_to_loss','down_left','up_left'}
            if not expected_keys.issubset(summary_data.keys()):
                continue
            d = summary_data
            mes_rt = d.get('mes_rt_price', d.get('price', 0.0))
            if mes_rt == last_rt_price:
                if not waiting_printed:
                    print(f"👀 {ts} waiting for market to update")
                    waiting_printed = True
                continue
            last_rt_price = mes_rt
            # Grouped summary
            exp_date = datetime.strptime(d['exp'], '%Y%m%d').strftime('%b %d, %Y')
            print(f"🕒 {ts} | MES: {mes_rt:.2f} | Option Strike: {d['strike']} | EXP ({exp_date})")
            print(f"📈 Bid/Ask: {d['bid']} / {d['ask']} |Spread: {d['spread']:.2f}")
            print(f"📊 Cost basis (💵 Credit received): ${d['cost']:.2f}")
            print(f"💰 P/L: ${d['pnl']:.2f} ({d['pnl_pct']:.1f}%)")
            print(f"💵 Cash balance: ${d['cash']:.2f}")
            # Display daily and weekly roll counts
            roll_counts = load_roll_counts()
            today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
            week = datetime.now(ZoneInfo('America/New_York')).isocalendar()
            week_key = f"{week[0]}-W{week[1]:02d}"
            daily_rolls = roll_counts['daily'].get(today, 0)
            weekly_rolls = roll_counts['weekly'].get(week_key, 0)
            print(f"🔄 Rolls today: {daily_rolls}, this week: {weekly_rolls}")
            print(f"⏳ Waiting to roll... (+{PROFIT_TARGET}% / {LOSS_LIMIT}%)")
            print(f"   ↳ {d['pct_to_profit']:.1f}% until profit target, {d['pct_to_loss']:.1f}% until loss limit")
            print(f"   ↳ {d['down_left']:.2f} pts until roll DOWN, "
                  f"MES Price at fill: {d['price']:.2f}, "
                  f"{d['up_left']:.2f} pts until roll UP")
            # Countdown until market close (17:00 ET) on weekdays
            now = datetime.now(ZoneInfo('America/New_York'))
            if now.weekday() < 5 and now.time() < dt_time(17, 0):
                close_dt = datetime.combine(now.date(), dt_time(17, 0), ZoneInfo('America/New_York'))
                delta = close_dt - now
                hrs, rem = divmod(int(delta.total_seconds()), 3600)
                mins, secs = divmod(rem, 60)
                print(f"⏰ Market closes in {hrs}h {mins}m {secs}s")
            else:
                # Countdown until next market open
                now2 = datetime.now(ZoneInfo('America/New_York'))
                # Determine next open datetime (CME MES: Sunday 18:00 ET, weekdays 18:00->17:00 daily maintenance)
                def next_open_time(n):
                    # Friday after 17:00 or Saturday: next Sunday 18:00
                    w, t = n.weekday(), n.time()
                    if (w == 4 and t >= dt_time(17, 0)) or w == 5:
                        # days until Sunday
                        days_ahead = (6 - w) % 7
                        sunday = n.date() + timedelta(days=days_ahead)
                        return datetime.combine(sunday, dt_time(18, 0), ZoneInfo('America/New_York'))
                    # Sunday before 18:00
                    if w == 6 and t < dt_time(18, 0):
                        return datetime.combine(n.date(), dt_time(18, 0), ZoneInfo('America/New_York'))
                    # Daily maintenance window (17:00-18:00)
                    if dt_time(17, 0) <= t < dt_time(18, 0):
                        return datetime.combine(n.date(), dt_time(18, 0), ZoneInfo('America/New_York'))
                    # Otherwise market is open (shouldn't hit here)
                    return n
                reopen_dt = next_open_time(now2)
                delta2 = reopen_dt - now2
                hrs2, rem2 = divmod(int(delta2.total_seconds()), 3600)
                mins2, secs2 = divmod(rem2, 60)
                print(f"⏰ Market reopens in {hrs2}h {mins2}m {secs2}s")
            print("─" * 60)
            waiting_printed = False

# Start summary printer daemon
threading.Thread(target=summary_thread, daemon=True).start()

def run_bot():
    global LAST_PRINTED_PNL, LAST_PRINTED_SPREAD, summary_paused, skip_summary_count
    # Load or initialize roll counts
    roll_counts = load_roll_counts()
    # Attempt to restore baseline MES price from previous run
    base_mes_price = load_base_mes_price()
    # Track MES price at time of initial short for hybrid roll logic
    ib = connect_ib()
    print('✅ Connected to IB Gateway.')
    # Reset summary state on restart
    LAST_PRINTED_PNL = None
    LAST_PRINTED_SPREAD = None
    # Initialize last trade variable for in-flight checks
    trade = None
    # Timestamp to delay roll checks after initial open
    roll_enable_time = 0

    # ─── Cancel any existing open MES option orders ───
    ib.reqOpenOrders()
    # ib.openOrders() returns Order objects; use openTrades() for contract info
    for trade in ib.openTrades():
        o = trade.order
        c = trade.contract
        if c.secType == 'FOP' and c.symbol == UNDERLYING and o.action in ('BUY', 'SELL'):
            print(f"⚠️ Cancelling stale order: {o.action} {c.localSymbol} LMT {o.lmtPrice}")
            try:
                ib.cancelOrder(o)
            except:
                pass
    ib.sleep(1)
    # ─── Close any stray long call positions ───
    ib.reqPositions()
    for pos in ib.positions():
        c = pos.contract
        # If a floating long call exists, close it
        if c.secType == 'FOP' and c.symbol == UNDERLYING and pos.position > 0:
            print(f"⚠️ Closing extra floating long call: {c.localSymbol}")
            ib.placeOrder(c, MarketOrder('SELL', pos.position))
            ib.sleep(1)
    # ───────────────────────────────────────────

    # Drift correction: ensure only one short call per long futures contract
    ib.reqPositions()
    positions = ib.positions()
    long_futs = [p for p in positions if p.contract.secType == 'FUT' and p.contract.symbol == UNDERLYING and p.position > 0]
    short_calls = [p for p in positions if p.contract.secType == 'FOP' and p.contract.symbol == UNDERLYING and p.position < 0]
    # Close any extra short calls
    if len(short_calls) > len(long_futs):
        for extra in short_calls[len(long_futs):]:
            print(f"⚠️ Closing extra short call: {extra.contract.localSymbol}")
            ib.placeOrder(extra.contract, MarketOrder('BUY', abs(extra.position)))
            ib.sleep(1)

    # 1) Ensure an open short call exists, retrying until successful
    while True:
        # ─── Enforce CME trading hours & maintenance ───
        ny = ZoneInfo('America/New_York')
        now_dt = datetime.now(ny)
        now_t = now_dt.time()
        wkd = now_dt.weekday()  # Monday=0 ... Friday=4, Saturday=5, Sunday=6

        def next_open_time(now_dt):
            """
            Return the next datetime (NY time) when CME MES re-opens.
            """
            ny = ZoneInfo('America/New_York')
            today = now_dt.date()
            t = now_dt.time()
            wkd = now_dt.weekday()
            # Friday after 17:00 → Sunday 18:00
            if (wkd == 4 and t >= dt_time(17, 0)) or wkd == 5:
                # Advance to Sunday
                days_ahead = (6 - wkd) % 7  # days until Sunday
                sunday = today + timedelta(days=days_ahead)
                return datetime.combine(sunday, dt_time(18, 0), ny)
            # Sunday before 18:00
            if wkd == 6 and t < dt_time(18, 0):
                return datetime.combine(today, dt_time(18, 0), ny)
            # Daily maintenance 17:00‑18:00
            if dt_time(17, 0) <= t < dt_time(18, 0):
                return datetime.combine(today, dt_time(18, 0), ny)
            # Otherwise we're open now
            return now_dt

        # Determine if in closed window:
        closed = (
            # Saturday always closed
            wkd == 5 or
            # Sunday before 18:00 ET closed
            (wkd == 6 and now_t < dt_time(18, 0)) or
            # Friday after 17:00 ET closed
            (wkd == 4 and now_t >= dt_time(17, 0)) or
            # Daily maintenance 17:00-18:00 ET
            (now_t >= dt_time(17, 0) and now_t < dt_time(18, 0))
        )
        if closed:
            nxt = next_open_time(now_dt)
            delta = nxt - now_dt
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            print(f"⏰ Market closed; reopening in {hours}h {minutes}m (at {nxt.strftime('%Y-%m-%d %H:%M ET')})")
            # Only auto‑flatten before the WEEKEND: Friday 16:50–17:00 ET
            if wkd == 4 and dt_time(16, 50) <= now_t < dt_time(17, 0):
                print("⚠️ Auto‑flattening positions ahead of weekend close")
                ib.reqPositions()
                for pos in ib.positions():
                    c = pos.contract
                    qty = pos.position
                    if c.secType in ('FOP', 'FUT') and c.symbol == UNDERLYING and qty != 0:
                        action = 'BUY' if qty < 0 else 'SELL'
                        print(f"⚠️ Closing position: {action} {c.localSymbol} qty {abs(qty)}")
                        order = MarketOrder(action, abs(qty))
                        ib.placeOrder(c, order)
                        ib.sleep(1)
            else:
                print("ℹ️ Maintenance window—holding positions, no auto‑flatten.")
            # Sleep until next check
            time.sleep(300)  # sleep 5 minutes before re-check
            continue
        close_filled = False
        ib.reqPositions()
        positions = ib.positions()
        short_positions = [p for p in positions if p.contract.secType == 'FOP'
                           and p.contract.symbol == UNDERLYING and p.position < 0]
        if short_positions:
            # Reuse the first existing short call
            existing = short_positions[0].contract
            ib.qualifyContracts(existing)
            contract = existing
            multiplier = int(contract.multiplier)
            avg_cost_total = short_positions[0].avgCost
            entry_px = abs(avg_cost_total) / multiplier
            print(f"🔍 Reusing open short call: {contract.localSymbol} @ ${entry_px:.2f}")
            # Only set baseline if not already loaded from previous run
            if base_mes_price is None:
                expiry, fut_month = get_expiry_and_future_expiry()
                fut_details = ib.reqContractDetails(Contract(
                    symbol=UNDERLYING,
                    secType='FUT',
                    exchange='CME',
                    currency='USD'
                ))
                if not fut_details:
                    raise Exception(f"No MES future contract found")
                fut_contract = fut_details[0].contract
                ib.qualifyContracts(fut_contract)
                fut_ticker = ib.reqMktData(fut_contract, '', False, True)
                ib.sleep(0.2)
                base_mes_price = (fut_ticker.bid + fut_ticker.ask) / 2 \
                                  if (fut_ticker.bid is not None and fut_ticker.ask is not None) \
                                  else float('nan')
                # Persist baseline MES price for future runs
                save_base_mes_price(base_mes_price)
            # If base_mes_price already loaded, leave it unchanged
            break
        else:
            # No open short—attempt to open one
            implied_contract = choose_option_contract(ib, strike_offset=0)
            ensure_single_short_call(ib)
            trade = place_stepped_limit(ib, implied_contract, 'SELL', 1)
            ensure_single_short_call(ib)
            if trade and hasattr(trade, 'fills') and trade.fills:
                contract = trade.contract
                entry_px = trade.fills[-1].execution.price
                multiplier = int(contract.multiplier)
                print(f"🎉 Short Call Opened: {contract.localSymbol} sold at ${entry_px:.2f}")
                # Capture baseline MES exactly at fill
                mid = fetch_mes_mid(ib)
                if not math.isnan(mid):
                    base_mes_price = mid
                    save_base_mes_price(base_mes_price)
                    with summary_lock:
                        summary_data['price'] = base_mes_price
                        summary_data['strike'] = contract.strike
                        summary_data['exp'] = contract.lastTradeDateOrContractMonth
                        summary_data['just_filled'] = True
                        skip_summary_count = 2
                else:
                    print("⚠️ Failed to fetch valid MES mid at fill; baseline remains unchanged")
                ib.sleep(CHECK_INTERVAL)
                roll_enable_time = time.time() + CHECK_INTERVAL
                break
            else:
                print("⚠️ Short call did not open; retrying in next interval.")
                ib.sleep(CHECK_INTERVAL)
                continue

    # Unpause summary after initial short is confirmed
    with summary_lock:
        summary_paused = False

    # Confirm short call position via existing positions
    ib.reqPositions()
    positions = ib.positions()
    for pos in positions:
        if pos.contract.localSymbol == contract.localSymbol and pos.position < 0:
            break

    # Subscribe to underlying MES futures price via reqContractDetails
    expiry, fut_month = get_expiry_and_future_expiry()
    fut_details = ib.reqContractDetails(Contract(
        symbol=UNDERLYING,
        secType='FUT',
        exchange='CME',
        currency='USD'
    ))
    if not fut_details:
        raise Exception(f"No MES future contract found")
    fut_contract = fut_details[0].contract
    ib.qualifyContracts(fut_contract)

    while True:
        close_filled = False
        # ─── Delay roll logic until cooldown expires ───
        if time.time() < roll_enable_time:
            ib.sleep(CHECK_INTERVAL)
            continue
        # ─── Connection watchdog ───
        if not ib.isConnected():
            print("❌ Disconnected from IB; attempting to reconnect...")
            while not ib.isConnected():
                try:
                    ib.disconnect()  # ensure clean state
                except:
                    pass
                try:
                    ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
                except Exception as e:
                    print(f"   ❌ Reconnect failed: {e}; retrying in 1s")
                    time.sleep(1)
                    continue
                print("✅ Reconnected to IB.")
            # Immediately fetch fresh market data on reconnect
            ib.reqTickers(contract, fut_contract)
            # Skip normal sleep to resume data polling right away
        # ───────────────────────────
        # ─── Skip printing while an order is in-flight (only pause on truly pending states) ───
        if trade and hasattr(trade, 'orderStatus') and trade.orderStatus.status not in ('Filled', 'Cancelled'):
            if ib.isConnected():
                ib.sleep(CHECK_INTERVAL)
            continue
        # ────────────────────────────────────────────────────────────────────────────────────
        # 1) Fetch fresh tickers snapshot for option and future
        tickers = ib.reqTickers(contract, fut_contract)
        if len(tickers) != 2:
            print("⚠️ Warning: reqTickers did not return both contract tickers")
            if ib.isConnected():
                ib.sleep(1)
            continue
        opt_ticker, fut_ticker = tickers
        # Guard against invalid NBBO; fallback to fresh snapshot if needed
        raw_bid = opt_ticker.bid
        raw_ask = opt_ticker.ask
        if raw_bid is None or raw_bid <= 0 or raw_ask is None or raw_ask <= raw_bid:
            md = ib.reqMktData(contract, '', False, True)
            ib.sleep(0.05)
            raw_bid = md.bid
            raw_ask = md.ask
        # Normalize bids/asks
        bid = raw_bid if (raw_bid is not None and raw_bid > 0) else 0.0
        ask = raw_ask if (raw_ask is not None and raw_ask > bid) else bid
        fut_bid = fut_ticker.bid
        fut_ask = fut_ticker.ask
        # 3) Calculate metrics
        mes_price = fut_ticker.last if fut_ticker.last is not None else fut_ticker.close or float('nan')
        # Initialize baseline if invalid
        if base_mes_price is None or (isinstance(base_mes_price, float) and math.isnan(base_mes_price)):
            base_mes_price = mes_price
        option_mid = (bid + ask) / 2 if (bid is not None and ask is not None) else float('nan')
        # Retrieve the live position's average cost from IB (total dollars paid, per contract)
        ib.reqPositions()
        positions = ib.positions()
        short_positions = [
            p for p in positions
            if p.contract.secType == 'FOP'
            and p.contract.localSymbol == contract.localSymbol
            and p.position < 0
        ]
        if short_positions:
            pos = short_positions[0]
            # avgCost on a short call is negative total premium received; invert sign for basis
            avg_cost_total = abs(pos.avgCost)
            cost_basis = avg_cost_total
            entry_px = avg_cost_total / multiplier
        # Recalculate PnL using the true basis
        unreal = (entry_px - option_mid) * multiplier
        pnl_pct = (unreal / cost_basis) * 100 if cost_basis else 0.0
        spread = ask - bid
        # --- Compute move from baseline MES price ---
        move_down = max(0, base_mes_price - mes_price)
        move_up   = max(0, mes_price - base_mes_price)
        # ─── Ensure a short call exists before proceeding ───
        ib.reqPositions()
        positions = ib.positions()
        short_positions = [
            p for p in positions
            if p.contract.secType == 'FOP'
            and p.contract.localSymbol == contract.localSymbol
            and p.position < 0
        ]
        if not short_positions:
            with summary_lock:
                summary_paused = True
            print("⚠️ No short call detected; selling ATM+1 to restore position")
            # Sell an ATM+1 strike
            atm_plus1 = choose_option_contract(ib, strike_offset=1)
            ensure_single_short_call(ib)
            trade = place_stepped_limit(ib, atm_plus1, 'SELL', 1)
            ensure_single_short_call(ib)
            if trade and hasattr(trade, 'fills') and trade.fills:
                fill_px = trade.fills[-1].execution.price
                print(f"✅ Restored short call: {trade.contract.localSymbol} at ${fill_px:.2f}")
                contract = trade.contract
                entry_px = fill_px
                multiplier = int(contract.multiplier)
                # Reset baseline MES price to current for hybrid roll logic
                base_mes_price = mes_price
                # Persist baseline MES price after roll
                save_base_mes_price(base_mes_price)
                # Reset roll cooldown
                roll_enable_time = time.time() + CHECK_INTERVAL
                with summary_lock:
                    summary_data['price'] = base_mes_price
                    summary_data['strike'] = contract.strike
                    summary_data['exp'] = contract.lastTradeDateOrContractMonth
                    summary_data['just_filled'] = True
                    skip_summary_count = 2
                # Confirm the restored position has appeared in IBKR (single immediate check)
                ib.reqPositions()
                if any(p.contract.localSymbol == contract.localSymbol and p.position < 0 for p in ib.positions()):
                    print(f"✅ Confirmed position for restored call: {contract.localSymbol}")
                else:
                    print(f"⚠️ Could not confirm restored call immediately; will verify next loop")
            else:
                print("⚠️ Failed to restore short call; will retry after interval")
            with summary_lock:
                summary_paused = False
            # Skip roll logic this iteration
            if ib.isConnected():
                ib.sleep(CHECK_INTERVAL)
            continue
        # ───────────────────────────────────────────────────
        # Always print summary each loop for real‑time updates
        LAST_PRINTED_PNL = pnl_pct
        LAST_PRINTED_SPREAD = spread
        ts_summary = datetime.now(ZoneInfo('America/New_York')).strftime('%H:%M:%S')
        # Compute summary values but do not print them here; only update shared state.
        # Distance to roll thresholds
        pct_to_profit = max(0, PROFIT_TARGET - pnl_pct)
        pct_to_loss   = max(0, pnl_pct - LOSS_LIMIT)

        # Distance (in points) from current MES price to each roll trigger (relative to baseline MES price)
        # move_down = base_mes_price - mes_price
        # move_up   = mes_price - base_mes_price
        remaining_down = max(0, MES_MOVE_DOWN_THRESHOLD - move_down)
        remaining_up   = max(0, MES_MOVE_UP_THRESHOLD   - move_up)
        # quantize distances to 0.25 increments
        remaining_down = round(remaining_down * 4) / 4
        remaining_up   = round(remaining_up   * 4) / 4

        # Fetch USD cash balance once per loop using TotalCashBalance tag (live snapshot)
        ib.reqAccountSummary()  # refresh snapshot
        account_summary = ib.accountSummary()
        cash_balance = next((float(row.value) for row in account_summary
                             if row.tag == 'TotalCashBalance' and row.currency == 'USD'), float('nan'))

        # Update shared summary state for printer thread
        with summary_lock:
            summary_data.update(dict(
                mes_rt_price = mes_price,
                price        = base_mes_price,
                strike       = contract.strike,
                exp          = contract.lastTradeDateOrContractMonth,
                bid          = bid,
                ask          = ask,
                spread       = spread,
                fut_bid      = fut_bid,
                fut_ask      = fut_ask,
                cost         = cost_basis,
                pnl          = unreal,
                pnl_pct      = pnl_pct,
                cash         = cash_balance,
                pct_to_profit = pct_to_profit,
                pct_to_loss   = pct_to_loss,
                down_left    = remaining_down,
                up_left      = remaining_up,
            ))
        # 5) Roll condition
        # Use move_down and move_up (relative to baseline MES), not strike

        if pnl_pct >= PROFIT_TARGET and move_down >= MES_MOVE_DOWN_THRESHOLD:
            print('▶️ Rolling DOWN')
            with summary_lock:
                summary_paused = True
            # 1) Buy to close existing short call
            print('📤 Buying to close short call')
            # Show current call spread before BUY-to-close using snapshot NBBO
            md = ib.reqMktData(contract, '', False, True)
            ib.sleep(0.1)  # allow snapshot to populate
            # Treat None or NaN bids/asks as zero
            raw_bid = md.bid
            bid_bc = raw_bid if (raw_bid is not None and not math.isnan(raw_bid)) else 0.0
            raw_ask = md.ask
            ask_bc = raw_ask if (raw_ask is not None and not math.isnan(raw_ask)) else bid_bc
            spread_bc = ask_bc - bid_bc
            print(f"⚖️ Call spread before BUY-to-close: {spread_bc:.2f} (bid {bid_bc:.2f} / ask {ask_bc:.2f})")
            # ─── Skip buy-to-close if spread is zero / negative ───
            if spread_bc <= 0:
                print(f"⚠️ Spread {spread_bc:.2f} ≤ 0; invalid quote, skipping buy-to-close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # ─── Skip buy-to-close when spread indicates low liquidity ───
            if spread_bc > 3.0:
                print(f"⚠️ Spread {spread_bc:.2f} > 3.0; spread too wide, skipping buy-to-close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # ─── Skip buy-to-close when spread too wide ───
            if spread_bc > 3.0:
                print(f"⚠️ Spread {spread_bc:.2f} > 3.0; skipping buy-to-close and roll-down")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # Ensure contract has up-to-date conId and localSymbol
            ib.qualifyContracts(contract)
            ib.reqPositions()
            pos_to_close = None
            for pos in ib.positions():
                if pos.contract.localSymbol == contract.localSymbol and pos.position < 0:
                    pos_to_close = pos
                    break
            if not pos_to_close:
                print(f"⚠️ No short call position found for {contract.localSymbol}, skipping close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            trade_close = place_stepped_limit(ib, contract, 'BUY', abs(pos_to_close.position))
            if trade_close and hasattr(trade_close, 'fills') and trade_close.fills:
                fill_px = trade_close.fills[-1].execution.price
                print(f"✅ Closed short call {contract.localSymbol} at ${fill_px:.2f}")
                # Record that a close succeeded
                close_filled = True
                # 2) Sell to open new short call
                print('📥 Selling to open new short call')
                # Show current call spread before SELL-to-open
                new_contract = choose_option_contract(ib, -STRIKE_STEP)
                so_ticker = ib.reqTickers(new_contract)[0]
                ib.sleep(0.1)
                bid_so = so_ticker.bid or 0.0
                ask_so = so_ticker.ask or bid_so
                spread_so = ask_so - bid_so
                print(f"⚖️ Call spread before SELL-to-open: {spread_so:.2f} (bid {bid_so:.2f} / ask {ask_so:.2f})")
                ensure_single_short_call(ib)
                trade = place_stepped_limit(ib, new_contract, 'SELL', 1)
                # Ensure only one short call after rolling down
                ensure_single_short_call(ib)
                # fallback if no fill
                if not (trade and hasattr(trade, 'fills') and trade.fills):
                    print("⚠️ Market‑to‑Limit fallback did not fill; will retry next loop")
                    if ib.isConnected():
                        ib.sleep(CHECK_INTERVAL)
                    continue
                # Confirm sell-to-open fill and update only if filled
                filled = False
                if trade and hasattr(trade, 'fills') and trade.fills:
                    filled = True
                    fill_px = trade.fills[-1].execution.price
                elif getattr(trade, 'orderStatus', None) and trade.orderStatus.status == 'Filled':
                    filled = True
                    fill_px = getattr(trade.orderStatus, 'avgFillPrice', trade.order.lmtPrice)
                if filled:
                    print(f"✅ Opened new short call: {trade.contract.localSymbol} at ${fill_px:.2f}")
                    # Clear stale summary to prevent printing old data
                    with summary_lock:
                        summary_data.clear()
                # Stamp new baseline MES at roll-down fill
                mid = fetch_mes_mid(ib)
                if not math.isnan(mid):
                    base_mes_price = mid
                    save_base_mes_price(base_mes_price)
                    # Increment roll count
                    today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
                    week = datetime.now(ZoneInfo('America/New_York')).isocalendar()
                    week_key = f"{week[0]}-W{week[1]:02d}"
                    roll_counts['daily'][today] = roll_counts['daily'].get(today, 0) + 1
                    roll_counts['weekly'][week_key] = roll_counts['weekly'].get(week_key, 0) + 1
                    save_roll_counts(roll_counts)
                    print(f"📊 Rolls today ({today}): {roll_counts['daily'][today]}, this week ({week_key}): {roll_counts['weekly'][week_key]}")
                    with summary_lock:
                        summary_data['price'] = base_mes_price
                        summary_data['strike'] = trade.contract.strike
                        summary_data['exp'] = trade.contract.lastTradeDateOrContractMonth
                        summary_data['just_filled'] = True
                        skip_summary_count = 2
                else:
                    print("⚠️ Failed to fetch valid MES mid at fill; baseline remains unchanged")
                with summary_lock:
                    summary_paused = False
                contract = trade.contract
                entry_px = fill_px
                multiplier = int(contract.multiplier)
                # Confirmation print already above; removed detailed summary block per instructions.
                # ──────────────────────────────────────────────────────
            else:
                print("⚠️ Close did not fill within timeout; no confirmation of fill.")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
        elif pnl_pct <= LOSS_LIMIT and move_up >= MES_MOVE_UP_THRESHOLD:
            print('⚠️ Rolling UP')
            with summary_lock:
                summary_paused = True
            # 1) Buy to close existing short call
            print('📤 Buying to close short call')
            # Show current call spread before BUY-to-close
            bc_ticker = ib.reqTickers(contract)[0]
            ib.sleep(0.2)
            bid_bc = bc_ticker.bid or 0.0
            ask_bc = bc_ticker.ask or bid_bc
            spread_bc = ask_bc - bid_bc
            print(f"⚖️ Call spread before BUY-to-close: {spread_bc:.2f} (bid {bid_bc:.2f} / ask {ask_bc:.2f})")
            # ─── Skip buy-to-close if spread is zero / negative ───
            if spread_bc <= 0:
                print(f"⚠️ Spread {spread_bc:.2f} ≤ 0; invalid quote, skipping buy-to-close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # ─── Skip buy-to-close when spread indicates low liquidity ───
            if spread_bc > 3.0:
                print(f"⚠️ Spread {spread_bc:.2f} > 3.0; spread too wide, skipping buy-to-close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # ─── Skip buy-to-close when spread too wide ───
            if spread_bc > 3.0:
                print(f"⚠️ Spread {spread_bc:.2f} > 3.0; skipping buy-to-close and roll-up")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            # Ensure contract has up-to-date conId and localSymbol
            ib.qualifyContracts(contract)
            ib.reqPositions()
            pos_to_close = None
            for pos in ib.positions():
                if pos.contract.localSymbol == contract.localSymbol and pos.position < 0:
                    pos_to_close = pos
                    break
            if not pos_to_close:
                print(f"⚠️ No short call position found for {contract.localSymbol}, skipping close")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
            trade_close = place_stepped_limit(ib, contract, 'BUY', abs(pos_to_close.position))
            if trade_close and hasattr(trade_close, 'fills') and trade_close.fills:
                fill_px = trade_close.fills[-1].execution.price
                print(f"✅ Closed short call {contract.localSymbol} at ${fill_px:.2f}")
                # Record that a close succeeded
                close_filled = True
                # 2) Sell to open new short call
                print('📥 Selling to open new short call')
                # Show current call spread before SELL-to-open
                new_contract = choose_option_contract(ib, STRIKE_STEP)
                so_ticker = ib.reqTickers(new_contract)[0]
                ib.sleep(0.1)
                bid_so = so_ticker.bid or 0.0
                ask_so = so_ticker.ask or bid_so
                spread_so = ask_so - bid_so
                print(f"⚖️ Call spread before SELL-to-open: {spread_so:.2f} (bid {bid_so:.2f} / ask {ask_so:.2f})")
                ensure_single_short_call(ib)
                trade = place_stepped_limit(ib, new_contract, 'SELL', 1)
                ensure_single_short_call(ib)
                # Always update to new short call after buy-to-close
                # Determine fill price, guarding against None trade
                if not (trade and hasattr(trade, 'fills') and trade.fills):
                    print("⚠️ Market‑to‑Limit fallback did not fill; will retry next loop")
                    if ib.isConnected():
                        ib.sleep(CHECK_INTERVAL)
                    continue
                if trade and hasattr(trade, 'fills') and trade.fills:
                    fill_px = trade.fills[-1].execution.price
                elif trade and getattr(trade, 'order', None) is not None:
                    fill_px = getattr(trade.order, 'lmtPrice', entry_px)
                print(f"✅ Opened new short call: {trade.contract.localSymbol} at ${fill_px:.2f}")
                # Clear stale summary to prevent printing old data
                with summary_lock:
                    summary_data.clear()
                # Stamp new baseline MES at roll-up fill
                mid = fetch_mes_mid(ib)
                if not math.isnan(mid):
                    base_mes_price = mid
                    save_base_mes_price(base_mes_price)
                    # Increment roll count
                    today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
                    week = datetime.now(ZoneInfo('America/New_York')).isocalendar()
                    week_key = f"{week[0]}-W{week[1]:02d}"
                    roll_counts['daily'][today] = roll_counts['daily'].get(today, 0) + 1
                    roll_counts['weekly'][week_key] = roll_counts['weekly'].get(week_key, 0) + 1
                    save_roll_counts(roll_counts)
                    print(f"📊 Rolls today ({today}): {roll_counts['daily'][today]}, this week ({week_key}): {roll_counts['weekly'][week_key]}")
                    with summary_lock:
                        summary_data['price'] = base_mes_price
                        summary_data['strike'] = trade.contract.strike
                        summary_data['exp'] = trade.contract.lastTradeDateOrContractMonth
                        summary_data['just_filled'] = True
                        skip_summary_count = 2
                else:
                    print("⚠️ Failed to fetch valid MES mid at fill; baseline remains unchanged")
                with summary_lock:
                    summary_paused = False
                contract = trade.contract
                entry_px = fill_px
                multiplier = int(contract.multiplier)
                # Confirmation print already above; removed detailed summary block per instructions.
                # ──────────────────────────────────────────────────────
            else:
                print("⚠️ Close did not fill within timeout; no confirmation of fill.")
                if ib.isConnected():
                    ib.sleep(CHECK_INTERVAL)
                continue
        if ib.isConnected():
            ib.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    while True:
        try:
            run_bot()
        except Exception as e:
            print(f"❌ Bot error: {e}; restarting in {CHECK_INTERVAL}s")
        time.sleep(CHECK_INTERVAL)
