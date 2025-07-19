import os
import time
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
LEVERAGE = 10

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GSHEET_ID = os.getenv("GSHEET_ID")
GSHEET_CLIENT_EMAIL = os.getenv("GSHEET_CLIENT_EMAIL")
GSHEET_PRIVATE_KEY = os.getenv("GSHEET_PRIVATE_KEY").replace('\\n', '\n')

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

# ✅ State
in_position = False
entry_price = None
sl_price = None
tp_price = None
trailing_activated = False
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
    creds = {
        "type": "service_account",
        "client_email": GSHEET_CLIENT_EMAIL,
        "private_key": GSHEET_PRIVATE_KEY,
        "token_uri": "https://oauth2.googleapis.com/token"
    }
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds, scope))

def log_trade_to_sheet(data):
    try:
        gc = get_gsheet_client()
        sheet = gc.open_by_key(GSHEET_ID).sheet1
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
    return df[['time','open','high','low','close','volume']]

# 📈 Add indicators
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

    # Filter: 1h candle must match direction and not touching BB lines
    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']:
        return None
    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI:
        return None

    # Trend Buy
    if c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high']:
            return 'trend_buy'

    # Trend Sell
    if c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low']:
            return 'trend_sell'

    # Reversal Buy
    if c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low']:
            return 'reversal_buy'

    # Reversal Sell
    if c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high']:
            return 'reversal_sell'

    return None

# 🛠 Place order
def place_order(order_type):
    global in_position, entry_price, sl_price, tp_price, trailing_activated, trailing_peak, current_trail_percent
    side = SIDE_BUY if 'buy' in order_type else SIDE_SELL

    order = client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    price = float(order.get('avgFillPrice') or client.futures_symbol_ticker(symbol=SYMBOL)['price'])

    df_1h = add_indicators(get_klines('1h'))
    df_5m = add_indicators(get_klines('5m'))
    c1h = df_1h.iloc[-1]
    c5 = df_5m.iloc[-1]

    # SL logic
    if 'trend' in order_type:
        sl_price = c1h['open']
        tp_price = c5['bb_high'] if side == SIDE_BUY else c5['bb_low']
    else:
        sl_price = c5['open']
        tp_price = c5['bb_mid']

    in_position = True
    entry_price = price
    trailing_activated = False
    trailing_peak = price
    current_trail_percent = 0.0

    send_telegram(f"✅ Opened {order_type.upper()} at {entry_price}\nSL: {sl_price}\nTP: {tp_price}")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, entry_price, sl_price, tp_price, "Opened"])

# 🔄 Manage trade
def manage_trade():
    global in_position, trailing_activated, trailing_peak, current_trail_percent
    price = float(client.futures_symbol_ticker(symbol=SYMBOL)['price'])
    profit_pct = (price - entry_price) / entry_price if entry_price else 0

    # Activate trailing based on profit levels
    if profit_pct >= 0.03:
        current_trail_percent = 0.015
        trailing_activated = True
    elif profit_pct >= 0.02:
        current_trail_percent = 0.01
        trailing_activated = True
    elif profit_pct >= 0.01:
        current_trail_percent = 0.005
        trailing_activated = True

    if trailing_activated:
        trailing_peak = max(trailing_peak, price)
        if price < trailing_peak * (1 - current_trail_percent):
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
    else:
        if price <= sl_price:
            close_position(price, "Stop Loss Hit")
        elif price >= tp_price:
            close_position(price, "Take Profit Hit")

# ❌ Close trade
def close_position(exit_price, reason):
    global in_position
    side = SIDE_SELL if entry_price < exit_price else SIDE_BUY
    client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    send_telegram(f"❌ Closed at {exit_price} ({reason})")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, "close", entry_price, sl_price, tp_price, f"Closed: {reason}"])
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
        time.sleep(180)  # every 3 minutes

# 🌐 Flask app
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Live bot running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
