# bot.py
# 🚀 FULL BOT — ATR-scaled SL/TP (SL = ATR*0.8, TP = ATR*2.4) + ATR shown in STOP ORDER alerts
import os, time, json
from datetime import datetime, timedelta, timezone
import pandas as pd, threading
from dotenv import load_dotenv
import ta, requests, gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client
from binance.enums import *
from flask import Flask
from collections import deque

load_dotenv()

# ✅ Config
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.001
SPREAD_THRESHOLD = 0.5
DAILY_TARGET = 1200
DAILY_LOSS_LIMIT = -700
RSI_LO, RSI_HI = 47, 53
ENTRY_BUFFER = 0.8  # price buffer when creating stop
ATR_PERIOD = 14     # ATR lookback used for scaling
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")

# ✅ Clients
client_testnet = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
try:
    client_testnet.futures_change_leverage(symbol=SYMBOL, leverage=10)
except Exception:
    pass
client_live = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ✅ State
in_position = False
pending_order_id = None
pending_order_side = None
pending_order_time = None

entry_price = None
sl_price = None
tp_price = None
trailing_peak = None
trailing_stop_price = None
current_trail_percent = 0.0

trade_direction = None
daily_trades = deque()
target_hit = False
last_tp_hit_time = None

recent_losses = deque(maxlen=4)
last_loss_pause_time = None

# 📩 Telegram
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception:
        pass

# 📊 Google Sheets
def get_gsheet_client():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds, scope))

def log_trade_to_sheet(data):
    try:
        get_gsheet_client().open_by_key(GSHEET_ID).sheet1.append_row(data)
    except Exception:
        pass

# 📊 Data & indicators
def get_klines(interval='5m', limit=100):
    df = pd.DataFrame(client_live.futures_klines(symbol=SYMBOL, interval=interval, limit=limit),
        columns=['open_time','open','high','low','close','volume','close_time',
                 'quote_asset_volume','number_of_trades','taker_buy_base','taker_buy_quote','ignore'])
    df['time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], 14)
    bb = ta.volatility.BollingerBands(df['close'], 20, 2)
    df['bb_mid'], df['bb_high'], df['bb_low'] = bb.bollinger_mavg(), bb.bollinger_hband(), bb.bollinger_lband()
    # ATR for scaling
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], ATR_PERIOD)
    return df

# 📊 Signal logic (unchanged)
def check_signal():
    global last_tp_hit_time
    if target_hit:
        return None
    if last_tp_hit_time and datetime.utcnow() - last_tp_hit_time < timedelta(minutes=30):
        return None

    df_5m = add_indicators(get_klines('5m'))
    df_1h = add_indicators(get_klines('1h'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]

    now = datetime.utcnow() + timedelta(hours=1)
    if now.minute >= 50:
        return None

    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI:
        return None

    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']:
        return None

    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'trend_buy'
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'trend_sell'
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'
    return None

# 🛠 Place stop order (ATR-scaled SL/TP + ATR included in Telegram message)
def place_order(order_type):
    global pending_order_id, pending_order_side, pending_order_time, sl_price, tp_price, trade_direction
    if target_hit or in_position:
        return

    side = 'buy' if 'buy' in order_type else 'sell'

    if pending_order_id and pending_order_side != side:
        try:
            client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except Exception:
            pass
        # notify cancelled pending opposite signal
        send_telegram("⚠ *Canceled previous pending order* (opposite signal)")
        pending_order_id = None

    ob = client_live.futures_order_book(symbol=SYMBOL)
    ask, bid = float(ob['asks'][0][0]), float(ob['bids'][0][0])
    if ask - bid > SPREAD_THRESHOLD:
        return

    stop = round(ask + ENTRY_BUFFER, 2) if 'buy' in order_type else round(bid - ENTRY_BUFFER, 2)

    df_1h = add_indicators(get_klines('1h'))
    df_5m = add_indicators(get_klines('5m'))
    c1h, c5 = df_1h.iloc[-1], df_5m.iloc[-1]

    # ATR: prefer 5m ATR for finer scaling; fall back to 1h ATR if NaN
    atr = float(c5['atr']) if not pd.isna(c5['atr']) and c5['atr'] > 0 else float(c1h['atr']) if not pd.isna(c1h['atr']) else 0.0

    # ATR-scaled offsets
    sl_offset = atr * 0.8
    tp_offset = atr * 2.4

    if 'buy' in order_type:
        sl_price = round(stop - sl_offset, 2)
        tp_price = round(stop + tp_offset, 2)
    else:  # sell
        sl_price = round(stop + sl_offset, 2)
        tp_price = round(stop - tp_offset, 2)

    trade_direction = 'long' if 'buy' in order_type else 'short'

    try:
        res = client_testnet.futures_create_order(
            symbol=SYMBOL,
            side=SIDE_BUY if 'buy' in order_type else SIDE_SELL,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=stop,
            quantity=TRADE_QUANTITY
        )
        pending_order_id, pending_order_side, pending_order_time = res['orderId'], side, datetime.utcnow()
    except Exception:
        # if order creation fails, keep function silent (telegram only for successful placement)
        return

    # Telegram message includes ATR
    atr_text = f"📊 ATR({ATR_PERIOD}): `{atr:.2f}`" if atr > 0 else ""
    msg = (
        f"🟩 *STOP ORDER PLACED*\n"
        f"*Type:* `{order_type.upper()}`\n"
        f"*Price:* `{stop}`\n"
        f"*SL:* `{sl_price}` | *TP:* `{tp_price}`\n"
        f"{atr_text}\n"
        f"📍 Pending *({trade_direction})*"
    )
    send_telegram(msg)
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, stop, sl_price, tp_price, f"Pending({trade_direction}),ATR:{atr:.2f}"])

# 🔄 Manage trade (unchanged logic)
def manage_trade():
    global trailing_peak, trailing_stop_price, current_trail_percent
    try:
        price = float(client_live.futures_symbol_ticker(symbol=SYMBOL)['price'])
    except Exception:
        return

    if entry_price is None or trade_direction is None:
        return

    if trade_direction == 'long':
        signed_profit = (price - entry_price) / entry_price
    else:
        signed_profit = (entry_price - price) / entry_price
    abs_profit = abs(signed_profit)

    if abs_profit >= 0.03:
        current_trail_percent = 0.015
    elif abs_profit >= 0.02:
        current_trail_percent = 0.01
    elif abs_profit >= 0.01:
        current_trail_percent = 0.005
    else:
        current_trail_percent = 0.0

    if trade_direction == 'long':
        if trailing_peak is None or price > trailing_peak:
            trailing_peak = price
            if current_trail_percent > 0:
                trailing_stop_price = trailing_peak * (1 - current_trail_percent)
        if current_trail_percent > 0 and trailing_stop_price is not None and price <= trailing_stop_price:
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return
        if tp_price is not None and price >= tp_price:
            close_position(price, "Take Profit Hit")
            return
        if sl_price is not None and price <= sl_price:
            close_position(price, "Stop Loss Hit")
            return
    else:
        if trailing_peak is None or price < trailing_peak:
            trailing_peak = price
            if current_trail_percent > 0:
                trailing_stop_price = trailing_peak * (1 + current_trail_percent)
        if current_trail_percent > 0 and trailing_stop_price is not None and price >= trailing_stop_price:
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return
        if tp_price is not None and price <= tp_price:
            close_position(price, "Take Profit Hit")
            return
        if sl_price is not None and price >= sl_price:
            close_position(price, "Stop Loss Hit")
            return

# ❌ Close trade (unchanged)
def close_position(exit_price, reason):
    global in_position, entry_price, trade_direction, daily_trades, target_hit, last_tp_hit_time, recent_losses, last_loss_pause_time
    try:
        side = SIDE_SELL if trade_direction == 'long' else SIDE_BUY
        client_testnet.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)

        if trade_direction == 'long':
            signed_pnl = round(exit_price - entry_price, 2)
        else:
            signed_pnl = round(entry_price - exit_price, 2)

        is_win = signed_pnl > 0
        daily_trades.append((signed_pnl, is_win))

        if "Stop Loss" in reason:
            recent_losses.append("SL")
            if len(recent_losses) == recent_losses.maxlen and all(r == "SL" for r in recent_losses):
                last_loss_pause_time = datetime.utcnow()
                send_telegram("⏸ Bot pausing for 1 hour due to consecutive Stop Losses.")
        else:
            recent_losses.clear()

        if "Take Profit" in reason:
            last_tp_hit_time = datetime.utcnow()

        total_pnl = sum(p for p, _ in daily_trades)
        if total_pnl >= DAILY_TARGET or total_pnl <= DAILY_LOSS_LIMIT:
            target_hit = True

        # reason mismatch check
        if ("Stop Loss" in reason and signed_pnl > 0) or ("Take Profit" in reason and signed_pnl < 0):
            send_telegram(
                f"⚠️ *Reason mismatch detected*\n"
                f"Reason: `{reason}`\n"
                f"Entry: `{entry_price}` | Exit: `{exit_price}`\n"
                f"Computed PnL: `{signed_pnl}` (direction={trade_direction})\n"
            )

        send_telegram(f"❌ *Closed at:* `{exit_price}`\n*Reason:* {reason}\n*PnL:* `{signed_pnl}`")
        log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"close({trade_direction})", entry_price, sl_price, tp_price, f"{reason},PnL:{signed_pnl}"])
    except Exception:
        pass
    finally:
        in_position = False
        entry_price = None
        trailing_peak = None
        trailing_stop_price = None
        current_trail_percent = 0.0
        trade_direction = None

# ⏱ Cancel untriggered stop orders after 10 minutes (keeps the telegram alert)
def cancel_expired_order():
    global pending_order_id, pending_order_time
    if pending_order_id and pending_order_time:
        age = datetime.utcnow() - pending_order_time
        if age.total_seconds() > 600:
            try:
                client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
                send_telegram("⌛ *Pending stop order canceled after 10 minutes*")
            except:
                pass
            pending_order_id, pending_order_time = None, None

# 🚀 Bot loop
def bot_loop():
    global in_position, pending_order_id, entry_price, trailing_peak, trailing_stop_price, current_trail_percent, last_loss_pause_time, trade_direction
    while True:
        try:
            if last_loss_pause_time:
                if datetime.utcnow() - last_loss_pause_time < timedelta(hours=1):
                    time.sleep(30)
                    continue
                else:
                    last_loss_pause_time = None

            if not in_position:
                if pending_order_id:
                    order = client_testnet.futures_get_order(symbol=SYMBOL, orderId=pending_order_id)
                    if order.get('status') == 'FILLED':
                        entry_price = float(order.get('avgFillPrice') or order.get('stopPrice'))
                        in_position = True
                        trailing_peak = entry_price
                        trailing_stop_price = None
                        current_trail_percent = 0.0
                        send_telegram(f"✅ *STOP order triggered*\n*Entry Price:* `{entry_price}`\n*Direction:* `{trade_direction}`")
                        log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"Triggered({trade_direction})", entry_price, sl_price, tp_price, "Opened"])
                        pending_order_id, pending_order_time = None, None
                    else:
                        cancel_expired_order()
                else:
                    s = check_signal()
                    if s:
                        place_order(s)
            else:
                manage_trade()
        except Exception:
            # silent on exceptions (notifications only via Telegram/sheets)
            pass
        time.sleep(120)

# 🌐 Flask & daily report
app = Flask(__name__)
@app.route('/')
def home():
    return "🚀 Bot is live."

def daily_report_loop():
    global target_hit
    while True:
        now = datetime.utcnow() + timedelta(hours=1)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        total_trades = len(daily_trades)
        total_pnl = sum(p for p, _ in daily_trades)
        num_wins = sum(1 for _, win in daily_trades if win)
        win_rate = (num_wins / total_trades) * 100 if total_trades > 0 else 0
        biggest_win = max((p for p, _ in daily_trades if p > 0), default=0)
        biggest_loss = min((p for p, _ in daily_trades if p < 0), default=0)

        msg = f"""📊 *Yesterday's Summary*
Total Trades: {total_trades}
Win Rate: {win_rate:.1f}%
Total PnL: {total_pnl:.2f}
Biggest Win: {biggest_win}
Biggest Loss: {biggest_loss}
{'🎯 Target hit ✅' if target_hit else '🎯 Target not reached ❌'}"""
        send_telegram(msg)
        daily_trades.clear()
        target_hit = False

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
