import os
import time
import json
from datetime import datetime, timedelta
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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")
SPREAD_THRESHOLD = 15  # USD

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client.futures_change_leverage(symbol=SYMBOL, leverage=10)




# ✅ State
in_position = False
@@ -37,7 +39,7 @@
trailing_peak = None
current_trail_percent = 0.0
trade_direction = None  # 'long' or 'short'
daily_trades = deque()  # store (pnl, is_win)

RSI_LO, RSI_HI = 47, 53

@@ -66,17 +68,17 @@ def log_trade_to_sheet(data):
        pass

# 📊 Get data
def get_klines(interval='5m', limit=100):
    klines = client.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[

        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    df['time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

# 📈 Indicators
def add_indicators(df):
@@ -89,15 +91,13 @@ def add_indicators(df):

# 📊 Signal logic
def check_signal():
    df_5m = add_indicators(get_klines('5m'))
    df_1h = add_indicators(get_klines('1h'))
    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]

    # Skip last 10 minutes of 1h candle
    now = datetime.utcnow()
    minutes = now.minute
    if minutes >= 50:
        return None

    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI:
@@ -120,23 +120,23 @@ def check_signal():
def place_order(order_type):
    global in_position, entry_price, sl_price, tp_price, trailing_peak, current_trail_percent, trade_direction

    # Spread check
    order_book = client.futures_order_book(symbol=SYMBOL)
    ask = float(order_book['asks'][0][0])
    bid = float(order_book['bids'][0][0])
    spread = ask - bid
    if spread > SPREAD_THRESHOLD:
        send_telegram(f"⚠ Spread too wide (${spread:.2f}), skipping trade.")
        return

    side = SIDE_BUY if 'buy' in order_type else SIDE_SELL
    trade_direction = 'long' if 'buy' in order_type else 'short'

    order = client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    price = float(order['fills'][0]['price']) if 'fills' in order and order['fills'] else float(order.get('avgFillPrice') or client.futures_symbol_ticker(symbol=SYMBOL)['price'])


    df_1h = add_indicators(get_klines('1h'))
    df_5m = add_indicators(get_klines('5m'))
    c1h = df_1h.iloc[-1]
    c5 = df_5m.iloc[-1]

@@ -149,12 +149,13 @@ def place_order(order_type):
    in_position = True

    send_telegram(f"✅ Opened {order_type.upper()} at {entry_price}\nSL: {sl_price}\nTP: {tp_price}")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, entry_price, sl_price, tp_price, f"Opened ({trade_direction})"])

# 🔄 Manage trade
def manage_trade():
    global in_position, trailing_peak, current_trail_percent
    price = float(client.futures_symbol_ticker(symbol=SYMBOL)['price'])

    profit_pct = abs((price - entry_price) / entry_price) if entry_price else 0

    if profit_pct >= 0.03:
@@ -167,10 +168,10 @@ def manage_trade():
    if current_trail_percent > 0:
        trailing_peak = max(trailing_peak, price) if trade_direction == 'long' else min(trailing_peak, price)
        if trade_direction == 'long' and price < trailing_peak * (1 - current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return
        elif trade_direction == 'short' and price > trailing_peak * (1 + current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return

    if trade_direction == 'long':
@@ -187,14 +188,14 @@ def manage_trade():
# ❌ Close trade
def close_position(exit_price, reason):
    global in_position
    side = SIDE_SELL if trade_direction == 'long' else SIDE_BUY
    client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    pnl = round((exit_price - entry_price) * 1 if trade_direction == 'long' else (entry_price - exit_price) * 1, 2)
    is_win = pnl > 0
    daily_trades.append((pnl, is_win))

    send_telegram(f"❌ Closed at {exit_price} ({reason}) | PnL: {pnl}")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"close ({trade_direction})", entry_price, sl_price, tp_price, f"{reason}, PnL: {pnl}"])
    in_position = False

# 📊 Daily summary
@@ -203,19 +204,9 @@ def send_daily_summary():
        send_telegram("📊 Daily Summary:\nNo trades today.")
        return
    total_pnl = sum(p for p, _ in daily_trades)
    num_trades = len(daily_trades)
    num_wins = sum(1 for _, win in daily_trades if win)
    win_rate = (num_wins / num_trades) * 100 if num_trades else 0
    biggest_win = max((p for p, _ in daily_trades if p > 0), default=0)
    biggest_loss = min((p for p, _ in daily_trades if p < 0), default=0)
    msg = (
        f"📊 *Daily Summary* (UTC)\n"
        f"Total trades: {num_trades}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Total PnL: {total_pnl:.2f}\n"
        f"Biggest win: {biggest_win}\n"
        f"Biggest loss: {biggest_loss}"
    )
    send_telegram(msg)
    daily_trades.clear()

@@ -236,17 +227,15 @@ def bot_loop():
# 🕒 Daily scheduler
def daily_scheduler():
    while True:
        now = datetime.utcnow()
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
