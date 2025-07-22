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
from pybit.unified_trading import HTTP  # Bybit testnet
from flask import Flask
from collections import deque

load_dotenv()

# ✅ Config
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.001
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")
SPREAD_THRESHOLD = 50  # USD

session = HTTP(
    testnet=True,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

# ✅ State
in_position = False
entry_price = None
sl_price = None
tp_price = None
trailing_peak = None
current_trail_percent = 0.0
trade_direction = None  # 'long' or 'short'
daily_trades = deque()

RSI_LO, RSI_HI = 47, 53

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
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
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
def get_klines(interval='5'):
    res = session.get_kline(category="linear", symbol=SYMBOL, interval=interval, limit=100)
    k = res.get('result', {}).get('list', [])
    df = pd.DataFrame(k, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'turnover'  # last element not used
    ])
    df['time'] = pd.to_datetime(pd.to_numeric(df['open_time']), unit='s')
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df[::-1]  # reverse to get oldest first

# 📈 Indicators
def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_mid']  = bb.bollinger_mavg()
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low']  = bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal():
    df_5m = add_indicators(get_klines('5'))
    df_1h = add_indicators(get_klines('60'))
    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]

    now = datetime.now(timezone.utc)
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

# 🛠 Place order
def place_order(order_type):
    global in_position, entry_price, sl_price, tp_price, trailing_peak, current_trail_percent, trade_direction

    orderbook = session.get_orderbook(category="linear", symbol=SYMBOL)
    bid = float(orderbook['result'][0]['b'])
    ask = float(orderbook['result'][0]['a'])
    spread = ask - bid
    if spread > SPREAD_THRESHOLD:
        send_telegram(f"⚠ Spread too wide (${spread:.2f}), skipping trade.")
        return

    side = "Buy" if 'buy' in order_type else "Sell"
    trade_direction = 'long' if 'buy' in order_type else 'short'

    order = session.place_order(category="linear", symbol=SYMBOL, side=side,
                                orderType="Market", qty=TRADE_QUANTITY, reduceOnly=False)
    price = float(order.get('result', {}).get('avgFillPrice', bid if side=="Buy" else ask))

    df_1h = add_indicators(get_klines('60'))
    df_5m = add_indicators(get_klines('5'))
    c1h = df_1h.iloc[-1]
    c5 = df_5m.iloc[-1]

    sl_price = c1h['open'] if 'trend' in order_type else c5['open']
    tp_price = c5['bb_high'] if 'trend_buy' in order_type else c5['bb_low'] if 'trend_sell' in order_type else c5['bb_mid']

    entry_price = price
    trailing_peak = price
    current_trail_percent = 0.0
    in_position = True

    send_telegram(f"✅ Opened {order_type.upper()} at {entry_price}\nSL: {sl_price}\nTP: {tp_price}")
    log_trade_to_sheet([str(datetime.now(timezone.utc)), SYMBOL, order_type, entry_price, sl_price, tp_price, f"Opened ({trade_direction})"])

# 🔄 Manage trade
def manage_trade():
    global in_position, trailing_peak, current_trail_percent
    ticker = session.get_ticker(category="linear", symbol=SYMBOL)
    price = float(ticker['result'][0]['lastPrice'])
    profit_pct = abs((price - entry_price) / entry_price) if entry_price else 0

    if profit_pct >= 0.03:
        current_trail_percent = 0.015
    elif profit_pct >= 0.02:
        current_trail_percent = 0.01
    elif profit_pct >= 0.01:
        current_trail_percent = 0.005

    if current_trail_percent > 0:
        trailing_peak = max(trailing_peak, price) if trade_direction == 'long' else min(trailing_peak, price)
        if trade_direction == 'long' and price < trailing_peak * (1 - current_trail_percent):
            close_position(price, f"Trailing Stop Hit")
            return
        elif trade_direction == 'short' and price > trailing_peak * (1 + current_trail_percent):
            close_position(price, f"Trailing Stop Hit")
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
    global in_position
    side = "Sell" if trade_direction == 'long' else "Buy"
    session.place_order(category="linear", symbol=SYMBOL, side=side,
                        orderType="Market", qty=TRADE_QUANTITY, reduceOnly=True)
    pnl = round((exit_price - entry_price) if trade_direction == 'long' else (entry_price - exit_price), 2)
    daily_trades.append((pnl, pnl > 0))

    send_telegram(f"❌ Closed at {exit_price} ({reason}) | PnL: {pnl}")
    log_trade_to_sheet([str(datetime.now(timezone.utc)), SYMBOL, f"close ({trade_direction})", entry_price, sl_price, tp_price, f"{reason}, PnL: {pnl}"])
    in_position = False

# 📊 Daily summary
def send_daily_summary():
    if not daily_trades:
        send_telegram("📊 Daily Summary:\nNo trades today.")
        return
    total_pnl = sum(p for p, _ in daily_trades)
    win_rate = (sum(1 for _, win in daily_trades if win) / len(daily_trades)) * 100
    msg = (f"📊 *Daily Summary* (UTC)\nTotal trades: {len(daily_trades)}\nWin rate: {win_rate:.1f}%\n"
           f"Total PnL: {total_pnl:.2f}")
    send_telegram(msg)
    daily_trades.clear()

# 🚀 Bot loop
def bot_loop():
    while True:
        try:
            if not in_position:
                signal = check_signal()
                if signal:
                    place_order(signal)
            else:
                manage_trade()
        except Exception:
            pass
        time.sleep(180)

# 🕒 Daily scheduler
def daily_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        send_daily_summary()

# 🌐 Flask
app = Flask(__name__)
@app.route('/')
def home(): return "🚀 Bybit bot running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
