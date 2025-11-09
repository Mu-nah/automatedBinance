# 🚀 FULL BOT CODE (ATR + VOLUME INFO IN ALERT & GSHEET)
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

# ✅ CONFIG
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SYMBOL, TRADE_QUANTITY, SPREAD_THRESHOLD, DAILY_TARGET = "BTCUSDT", 0.001, 0.5, 1200
DAILY_LOSS_LIMIT = -700
RSI_LO, RSI_HI, ENTRY_BUFFER = 47, 53, 0.8
TELEGRAM_TOKEN, CHAT_ID, GSHEET_ID = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), os.getenv("GSHEET_ID")

# ✅ CLIENTS
client_testnet = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client_testnet.futures_change_leverage(symbol=SYMBOL, leverage=10)
client_live = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ✅ STATE
in_position, pending_order_id, pending_order_side, pending_order_time = False, None, None, None
entry_price, sl_price, tp_price, trailing_peak, trailing_stop_price, current_trail_percent = None, None, None, None, None, 0.0
trade_direction, daily_trades, target_hit = None, deque(), False
last_tp_hit_time = None
recent_losses = deque(maxlen=4)
last_loss_pause_time = None

# 📩 TELEGRAM
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except:
        pass

# 📊 GOOGLE SHEETS
def get_gsheet_client():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds, scope))

def log_trade_to_sheet(data):
    try:
        get_gsheet_client().open_by_key(GSHEET_ID).sheet1.append_row(data)
    except:
        pass

# 📊 DATA & INDICATORS
def get_klines(interval='5m', limit=100):
    df = pd.DataFrame(client_live.futures_klines(symbol=SYMBOL, interval=interval, limit=limit),
                      columns=['open_time','open','high','low','close','volume','close_time',
                               'quote_asset_volume','number_of_trades','taker_buy_base','taker_buy_quote','ignore'])
    df['time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    return df

def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], 14)
    bb = ta.volatility.BollingerBands(df['close'], 20, 2)
    df['bb_mid'], df['bb_high'], df['bb_low'] = bb.bollinger_mavg(), bb.bollinger_hband(), bb.bollinger_lband()
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['vol_avg'] = df['volume'].rolling(20).mean()
    return df

# 📊 SIGNAL LOGIC
def check_signal():
    global last_tp_hit_time
    if target_hit:
        return None
    if last_tp_hit_time and datetime.utcnow() - last_tp_hit_time < timedelta(minutes=30):
        return None
    df_5m, df_1h = add_indicators(get_klines('5m')), add_indicators(get_klines('1h'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]
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
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'
    return None

# 🛠 PLACE STOP ORDER
def place_order(order_type):
    global pending_order_id, pending_order_side, pending_order_time, sl_price, tp_price, trade_direction
    if target_hit or in_position:
        return
    side = 'buy' if 'buy' in order_type else 'sell'

    if pending_order_id and pending_order_side != side:
        try:
            client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except:
            pass
        send_telegram("⚠ *Canceled previous pending order* (opposite signal)")
        pending_order_id = None

    ob = client_live.futures_order_book(symbol=SYMBOL)
    ask, bid = float(ob['asks'][0][0]), float(ob['bids'][0][0])
    if ask - bid > SPREAD_THRESHOLD:
        return
    stop = round(ask + ENTRY_BUFFER, 2) if 'buy' in order_type else round(bid - ENTRY_BUFFER, 2)

    df_1h, df_5m = add_indicators(get_klines('1h')), add_indicators(get_klines('5m'))
    c1h, c5 = df_1h.iloc[-1], df_5m.iloc[-1]

    atr_value = float(c5['atr'])
    current_volume = float(c5['volume'])
    avg_volume = float(c5['vol_avg'])
    high_vol = current_volume >= 1.5 * avg_volume

    sl_price = c1h['open'] if 'trend' in order_type else c5['open']
    if 'reversal' in order_type:
        bb_mid = c5['bb_mid']
        tp_price = round(bb_mid + 100 if 'buy' in order_type else bb_mid - 100, 2)
    else:
        bb_tp = c5['bb_high'] if 'buy' in order_type else c5['bb_low']
        tp_price = round(bb_tp + 100 if 'buy' in order_type else bb_tp - 100, 2)

    trade_direction = 'long' if 'buy' in order_type else 'short'
    res = client_testnet.futures_create_order(
        symbol=SYMBOL,
        side=SIDE_BUY if 'buy' in order_type else SIDE_SELL,
        type=FUTURE_ORDER_TYPE_STOP_MARKET,
        stopPrice=stop,
        quantity=TRADE_QUANTITY
    )
    pending_order_id, pending_order_side, pending_order_time = res['orderId'], side, datetime.utcnow()

    # 🟩 ATR + VOLUME ADDED TO ALERT
    alert = (
        f"🟩 *STOP ORDER PLACED*\n"
        f"*Type:* `{order_type.upper()}`\n"
        f"*Price:* `{stop}`\n"
        f"*SL:* `{sl_price}` | *TP:* `{tp_price}`\n"
        f"📊 *ATR(14):* `{atr_value:.2f}`\n"
        f"📈 *Volume(5m):* `{current_volume:.0f}` | *Avg(20):* `{avg_volume:.0f}`"
    )
    if high_vol:
        alert += "\n🔥 *High Volume Spike!*"
    alert += f"\n📍 Pending *({trade_direction})*"
    send_telegram(alert)

    log_trade_to_sheet([
        str(datetime.utcnow()), SYMBOL, order_type, stop, sl_price, tp_price,
        f"Pending({trade_direction}),ATR:{atr_value:.2f},Vol:{current_volume:.0f},AvgVol:{avg_volume:.0f},{'HighVol🔥' if high_vol else ''}"
    ])

# 🔄 manage_trade, close_position, cancel_expired_order, bot_loop, daily_report_loop remain UNCHANGED
# (keep from your previous version exactly)

# 🌐 FLASK & MAIN
app = Flask(__name__)
@app.route('/')
def home(): return "🚀 Bot is live."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
