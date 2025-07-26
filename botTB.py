import os
import time
import json
from datetime import datetime, timedelta, timezone
import pandas as pd
import threading
from dotenv import load_dotenv
import ta
import requests
import gspread
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
SPREAD_THRESHOLD = 600  # USD
DAILY_TARGET = 1000  # USD
RSI_LO, RSI_HI = 47, 53
ENTRY_BUFFER = 0.8  # ≈ 80 pips

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")

# ✅ Clients
client_testnet = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client_testnet.futures_change_leverage(symbol=SYMBOL, leverage=10)
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
current_trail_percent = 0.0
trade_direction = None
daily_trades = deque()
target_hit = False

# 📩 Telegram
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception:
        pass

# 📊 Google Sheets
def get_gsheet_client():
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def log_trade_to_sheet(data):
    try:
        gc = get_gsheet_client()
        sheet = gc.open_by_key(GSHEET_ID).sheet1
        sheet.append_row(data)
    except Exception:
        pass

# 📊 Get data
def get_klines(interval='5m', limit=100):
    klines = client_live.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    df['time'] = pd.to_datetime(df['open_time'].astype(float), unit='ms')
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

# 📈 Indicators
def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_mid'] = bb.bollinger_mavg()
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal():
    if target_hit:
        return None
    df_5m = add_indicators(get_klines('5m'))
    df_1h = add_indicators(get_klines('1h'))
    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]
    now = datetime.now(timezone.utc) + timedelta(hours=1)
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
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'
    return None

# 🛠 Place stop order
def place_order(order_type):
    global pending_order_id, pending_order_side, pending_order_time
    global sl_price, tp_price, trade_direction
    if target_hit or in_position:
        return

    new_side = 'buy' if 'buy' in order_type else 'sell'
    if pending_order_id and pending_order_side != new_side:
        try:
            client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except:
            pass
        pending_order_id = None

    order_book = client_live.futures_order_book(symbol=SYMBOL)
    ask = float(order_book['asks'][0][0])
    bid = float(order_book['bids'][0][0])
    spread = ask - bid
    if spread > SPREAD_THRESHOLD:
        return

    df_1h = add_indicators(get_klines('1h'))
    df_5m = add_indicators(get_klines('5m'))
    c1h = df_1h.iloc[-1]
    c5 = df_5m.iloc[-1]

    stop_price = round(ask + ENTRY_BUFFER, 2) if 'buy' in order_type else round(bid - ENTRY_BUFFER, 2)
    sl_price = c1h['open'] if 'trend' in order_type else c5['open']
    tp_price = max(stop_price, c5['bb_high']) if 'buy' in order_type else min(stop_price, c5['bb_low'])
    trade_direction = 'long' if 'buy' in order_type else 'short'

    res = client_testnet.futures_create_order(
        symbol=SYMBOL, side=SIDE_BUY if 'buy' in order_type else SIDE_SELL,
        type=FUTURE_ORDER_TYPE_STOP_MARKET, stopPrice=stop_price, quantity=TRADE_QUANTITY
    )
    pending_order_id = res['orderId']
    pending_order_side = new_side
    pending_order_time = datetime.utcnow()

    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, stop_price, sl_price, tp_price, f"Pending ({trade_direction})"])

# 🛑 Cancel if pending >10min
def cancel_pending_if_needed():
    global pending_order_id, pending_order_time
    if pending_order_id and pending_order_time:
        if datetime.utcnow() - pending_order_time > timedelta(minutes=10):
            try:
                client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
            except:
                pass
            pending_order_id = None
            pending_order_time = None

# 🔄 Manage trade (✅ fixed trailing stop)
def manage_trade():
    global in_position, trailing_peak, current_trail_percent
    price = float(client_live.futures_symbol_ticker(symbol=SYMBOL)['price'])
    if not entry_price:
        return

    profit_pct = abs((price - entry_price) / entry_price)

    if profit_pct >= 0.03:
        current_trail_percent = 0.015
    elif profit_pct >= 0.02:
        current_trail_percent = 0.01
    elif profit_pct >= 0.01:
        current_trail_percent = 0.005

    # Update trailing_peak only in favorable direction
    if trade_direction == 'long' and price > trailing_peak:
        trailing_peak = price
    elif trade_direction == 'short' and price < trailing_peak:
        trailing_peak = price

    if current_trail_percent > 0:
        if trade_direction == 'long' and price <= trailing_peak * (1 - current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return
        elif trade_direction == 'short' and price >= trailing_peak * (1 + current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return

    if trade_direction == 'long':
        if price <= sl_price:
            close_position(price, "Stop Loss Hit")
        elif price >= tp_price:
            close_position(price, "Take Profit Hit")
    else:
        if price >= sl_price:
            close_position(price, "Stop Loss Hit")
        elif price <= tp_price:
            close_position(price, "Take Profit Hit")

# ❌ Close trade
def close_position(exit_price, reason):
    global in_position, target_hit
    side = SIDE_SELL if trade_direction == 'long' else SIDE_BUY
    client_testnet.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    pnl = round((exit_price - entry_price) if trade_direction == 'long' else (entry_price - exit_price), 2)
    daily_trades.append((pnl, pnl > 0))
    if sum(p for p, _ in daily_trades) >= DAILY_TARGET:
        target_hit = True

    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"close ({trade_direction})", entry_price, sl_price, tp_price, f"{reason}, PnL: {pnl}"])
    in_position = False

# 📊 Daily summary
def send_daily_summary():
    global daily_trades, target_hit
    daily_trades.clear()
    target_hit = False

# 🚀 Bot loop
def bot_loop():
    global in_position, pending_order_id, entry_price, trailing_peak, current_trail_percent
    while True:
        try:
            if not in_position:
                cancel_pending_if_needed()
                if pending_order_id:
                    try:
                        order = client_testnet.futures_get_order(symbol=SYMBOL, orderId=pending_order_id)
                        if order['status'] == 'FILLED':
                            entry_price = float(order.get('avgFillPrice') or order.get('stopPrice'))
                            in_position = True
                            trailing_peak = entry_price
                            current_trail_percent = 0.0
                            log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"Triggered ({trade_direction})", entry_price, sl_price, tp_price, "Opened"])
                            pending_order_id = None
                            pending_order_time = None
                    except:
                        pass
                else:
                    signal = check_signal()
                    if signal:
                        place_order(signal)
            else:
                manage_trade()
        except Exception:
            pass
        time.sleep(120)

# 🕒 Daily scheduler
def daily_scheduler():
    while True:
        now = datetime.utcnow() + timedelta(hours=1)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        send_daily_summary()

# 🌐 Flask app
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Live bot running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
