import os
import time
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv
import ta
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client
from binance.enums import *

load_dotenv()

# ✅ Config
TD_API_KEYS = os.getenv("TD_API_KEYS").split(",")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SYMBOL = "BTC/USD"  # Twelve Data uses BTC/USD not BTCUSDT
TRADE_QUANTITY = 0.001

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GSHEET_ID = os.getenv("GSHEET_ID")
GSHEET_CLIENT_EMAIL = os.getenv("GSHEET_CLIENT_EMAIL")
GSHEET_PRIVATE_KEY = os.getenv("GSHEET_PRIVATE_KEY").replace('\\n', '\n')

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)

# ✅ State
in_position = False
entry_price = None
sl_price = None
tp_price = None
trailing_activated = False

# 📩 Telegram
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": msg}
        requests.post(url, data=data)
    except Exception as e:
        print(f"Telegram send failed: {e}")

# 📊 Google Sheets
def get_gsheet_client():
    creds_dict = {
        "type": "service_account",
        "client_email": GSHEET_CLIENT_EMAIL,
        "private_key": GSHEET_PRIVATE_KEY,
        "token_uri": "https://oauth2.googleapis.com/token"
    }
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def log_trade_to_sheet(data):
    try:
        gc = get_gsheet_client()
        sheet = gc.open_by_key(GSHEET_ID).sheet1
        sheet.append_row(data)
    except Exception as e:
        print(f"GSheet log failed: {e}")

# 📊 Get data from Twelve Data
def get_klines(symbol, interval='5min', limit=100):
    for api_key in TD_API_KEYS:
        try:
            url = "https://api.twelvedata.com/time_series"
            params = {
                "symbol": symbol,
                "interval": interval,
                "outputsize": limit,
                "apikey": api_key
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if "values" not in data:
                print(f"TwelveData error: {data}")
                continue
            df = pd.DataFrame(data['values'])
            df = df.rename(columns={'datetime':'time'})
            df['time'] = pd.to_datetime(df['time'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
            df = df.sort_values('time').reset_index(drop=True)
            return df
        except Exception as e:
            print(f"TwelveData key failed: {api_key}, error: {e}")
            continue
    raise Exception("All TwelveData API keys failed!")

# 📈 Indicators
def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    df['bb_mid'] = bb.bollinger_mavg()
    return df

# 📊 Signal logic
def check_signal():
    df_5m = add_indicators(get_klines(SYMBOL, '5min'))
    df_1h = add_indicators(get_klines(SYMBOL, '1h'))

    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]

    # No trade if 1h candle indecisive or touching BB
    if abs(c1h['close'] - c1h['open']) / c1h['open'] < 0.001:
        return None
    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']:
        return None

    # Trend Buy
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] - 100 and \
       c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'trend_buy'

    # Trend Sell
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] + 100 and \
       c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'trend_sell'

    # Reversal Buy
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and \
       c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'

    # Reversal Sell
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and \
       c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'

    return None

# 🛠 Trade
def place_order(order_type):
    global in_position, entry_price, sl_price, tp_price, trailing_activated
    side = SIDE_BUY if 'buy' in order_type else SIDE_SELL

    order = client.futures_create_order(
        symbol='BTCUSDT', side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    price = float(order['avgFillPrice'] if 'avgFillPrice' in order else order['fills'][0]['price'])

    # SL = open of current 1h candle
    df_1h = get_klines(SYMBOL, '1h')
    sl_price = df_1h.iloc[-1]['open']

    # TP = nearest BB on 5m
    df_5m = add_indicators(get_klines(SYMBOL, '5min'))
    tp_price = df_5m.iloc[-1]['bb_high'] if side == SIDE_BUY else df_5m.iloc[-1]['bb_low']

    in_position = True
    entry_price = price
    trailing_activated = False

    msg = f"✅ Opened {order_type.upper()} at {entry_price}\nSL: {sl_price}\nTP: {tp_price}"
    print(msg)
    send_telegram(msg)
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, entry_price, sl_price, tp_price, "Opened"])

def manage_trade():
    global in_position, trailing_activated
    ticker = client.futures_symbol_ticker(symbol='BTCUSDT')
    price = float(ticker['price'])

    if not trailing_activated and (price - entry_price) / entry_price >= 0.05:
        trailing_activated = True
        send_telegram("🚀 Trailing stop activated")

    if trailing_activated:
        peak = max(price, entry_price * 1.05)
        if price < peak * 0.99:
            close_position(price, "Trailing Stop Hit")
    else:
        if price <= sl_price:
            close_position(price, "Stop Loss Hit")
        elif price >= tp_price:
            close_position(price, "Take Profit Hit")

def close_position(exit_price, reason):
    global in_position
    side = SIDE_SELL if entry_price < exit_price else SIDE_BUY
    client.futures_create_order(symbol='BTCUSDT', side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)

    msg = f"❌ Closed trade at {exit_price} ({reason})"
    print(msg)
    send_telegram(msg)
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, "close", entry_price, sl_price, tp_price, f"Closed: {reason}"])
    in_position = False

# 🚀 Run loop
def run_bot():
    global in_position
    while True:
        try:
            if not in_position:
                signal = check_signal()
                if signal:
                    place_order(signal)
            else:
                manage_trade()
        except Exception as e:
            print(f"Error: {e}")
            send_telegram(f"⚠ Error: {e}")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
