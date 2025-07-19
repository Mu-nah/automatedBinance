import os
import time
import json
from datetime import datetime
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

load_dotenv()

# ✅ Config
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.001

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client.futures_change_leverage(symbol=SYMBOL, leverage=10)

# ✅ State
in_position = False
entry_price = None
sl_price = None
tp_price = None
trailing_peak = None
current_trail_percent = 0.0

RSI_LO, RSI_HI = 45, 55

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
        sheet = gc.open_by_key(os.getenv("GSHEET_ID")).sheet1
        sheet.append_row(data)
    except Exception:
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
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_mid']  = bb.bollinger_mavg()
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low']  = bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal():
    df_5m = add_indicators(get_klines('5m'))
    df_1h = add_indicators(get_klines('1h'))
    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]

    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI:
        return None

    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']:
        return None

    # Trend Buy
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'trend_buy'

    # Trend Sell
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'trend_sell'

    # Reversal Buy
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'

    # Reversal Sell
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'

    return None

# 🛠 Place order
def place_order(order_type):
    global in_position, entry_price, sl_price, tp_price, trailing_peak, current_trail_percent
    side = SIDE_BUY if 'buy' in order_type else SIDE_SELL

    order = client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    price = float(order.get('avgFillPrice') or client.futures_symbol_ticker(symbol=SYMBOL)['price'])

    df_1h = add_indicators(get_klines('1h'))
    df_5m = add_indicators(get_klines('5m'))
    c1h = df_1h.iloc[-1]
    c5 = df_5m.iloc[-1]

    sl_price = c1h['open'] if 'trend' in order_type else c5['open']
    tp_price = c5['bb_high'] if 'buy' in order_type else c5['bb_low'] if 'trend' in order_type else c5['bb_mid']

    entry_price = price
    trailing_peak = price
    current_trail_percent = 0.0
    in_position = True

    send_telegram(f"✅ Opened {order_type.upper()} at {entry_price}\nSL: {sl_price}\nTP: {tp_price}")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, entry_price, sl_price, tp_price, "Opened"])

# 🔄 Manage trade
def manage_trade():
    global in_position, trailing_peak, current_trail_percent
    price = float(client.futures_symbol_ticker(symbol=SYMBOL)['price'])
    profit_pct = (price - entry_price) / entry_price if entry_price else 0

    # Dynamic trail
    if profit_pct >= 0.03:
        current_trail_percent = 0.015
    elif profit_pct >= 0.02:
        current_trail_percent = 0.01
    elif profit_pct >= 0.01:
        current_trail_percent = 0.005

    if current_trail_percent > 0:
        trailing_peak = max(trailing_peak, price)
        if price < trailing_peak * (1 - current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
            return

    if price <= sl_price:
        close_position(price, "Stop Loss Hit")
    elif price >= tp_price:
        close_position(price, "Take Profit Hit")

# ❌ Close trade
def close_position(exit_price, reason):
    global in_position
    side = SIDE_SELL if entry_price < exit_price else SIDE_BUY
    client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    pnl = round(exit_price - entry_price, 2)
    send_telegram(f"❌ Closed at {exit_price} ({reason}) | PnL: {pnl}")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, "close", entry_price, sl_price, tp_price, f"{reason}, PnL: {pnl}"])
    in_position = False

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

# 🌐 Flask app
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Live bot running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
