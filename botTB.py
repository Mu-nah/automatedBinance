# 🚀 START OF FULL BOT CODE
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
SYMBOL, TRADE_QUANTITY, SPREAD_THRESHOLD, DAILY_TARGET = "BTCUSDT", 0.001, 0.5, 3000
DAILY_LOSS_LIMIT = -1500
RSI_LO, RSI_HI, ENTRY_BUFFER = 47, 53, 0.8
TELEGRAM_TOKEN, CHAT_ID, GSHEET_ID = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), os.getenv("GSHEET_ID")

# ✅ Clients
client_testnet = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client_testnet.futures_change_leverage(symbol=SYMBOL, leverage=10)
client_live = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ✅ State
in_position, pending_order_id, pending_order_side, pending_order_time = False, None, None, None
entry_price, sl_price, tp_price, trailing_peak, trailing_stop_price, current_trail_percent = None, None, None, None, None, 0.0
trade_direction, daily_trades, target_hit = None, deque(), False
last_tp_hit_time = None
recent_losses = deque(maxlen=4)  # 🆕 Track recent SL streak
last_loss_pause_time = None      # 🆕 Pause timer after SL streak

# 📩 Telegram
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except: pass

# 📊 Google Sheets
def get_gsheet_client():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds, scope))

def log_trade_to_sheet(data):
    try:
        get_gsheet_client().open_by_key(GSHEET_ID).sheet1.append_row(data)
    except: pass

# 📊 Data & indicators
def get_klines(interval='5m', limit=100):
    df = pd.DataFrame(client_live.futures_klines(symbol=SYMBOL, interval=interval, limit=limit),
        columns=['open_time','open','high','low','close','volume','close_time',
                 'quote_asset_volume','number_of_trades','taker_buy_base','taker_buy_quote','ignore'])
    df['time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open','high','low','close','volume']: df[col]=df[col].astype(float)
    return df

def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'],14)
    bb = ta.volatility.BollingerBands(df['close'],20,2)
    df['bb_mid'], df['bb_high'], df['bb_low'] = bb.bollinger_mavg(), bb.bollinger_hband(), bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal():
    global last_tp_hit_time
    if target_hit: return None
    if last_tp_hit_time and datetime.utcnow() - last_tp_hit_time < timedelta(minutes=30):
        return None
    df_5m, df_1h = add_indicators(get_klines('5m')), add_indicators(get_klines('1h'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    if now.minute >= 50: return None
    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI: return None
    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']: return None
    if c5['close']>c5['bb_mid'] and c5['close']<c5['bb_high'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'trend_buy'
    if c5['close']<c5['bb_mid'] and c5['close']>c5['bb_low'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'trend_sell'
    if c5['close']<c5['bb_mid'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'reversal_buy'
    if c5['close']>c5['bb_mid'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'reversal_sell'
    return None

# 🛠 Place stop order
def place_order(order_type):
    global pending_order_id, pending_order_side, pending_order_time, sl_price, tp_price, trade_direction
    if target_hit or in_position: return
    side = 'buy' if 'buy' in order_type else 'sell'

    if pending_order_id and pending_order_side != side:
        try: client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except: pass
        send_telegram("⚠ *Canceled previous pending order* (opposite signal)")
        pending_order_id = None

    ob = client_live.futures_order_book(symbol=SYMBOL)
    ask, bid = float(ob['asks'][0][0]), float(ob['bids'][0][0])
    if ask-bid > SPREAD_THRESHOLD: return
    stop = round(ask+ENTRY_BUFFER,2) if 'buy' in order_type else round(bid-ENTRY_BUFFER,2)

    df_1h, df_5m = add_indicators(get_klines('1h')), add_indicators(get_klines('5m'))
    c1h, c5 = df_1h.iloc[-1], df_5m.iloc[-1]
    sl_price = c1h['open'] if 'trend' in order_type else c5['open']

    if 'reversal' in order_type:
        bb_mid = c5['bb_mid']
        tp_price = round(bb_mid + 100 if 'buy' in order_type else bb_mid - 100, 2)
    else:
        bb_tp = c5['bb_high'] if 'buy' in order_type else c5['bb_low']
        tp_price = round(bb_tp + 100 if 'buy' in order_type else bb_tp - 100, 2)

    trade_direction = 'long' if 'buy' in order_type else 'short'

    res = client_testnet.futures_create_order(symbol=SYMBOL, side=SIDE_BUY if 'buy' in order_type else SIDE_SELL,
        type=FUTURE_ORDER_TYPE_STOP_MARKET, stopPrice=stop, quantity=TRADE_QUANTITY)
    pending_order_id, pending_order_side, pending_order_time = res['orderId'], side, datetime.utcnow()

    send_telegram(f"🟩 *STOP ORDER PLACED*\n*Type:* `{order_type.upper()}`\n*Price:* `{stop}`\n*SL:* `{sl_price}` | *TP:* `{tp_price}`\n📍 Pending *({trade_direction})*")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, stop, sl_price, tp_price, f"Pending({trade_direction})"])

# 🔄 Manage trade
def manage_trade():
    global trailing_peak, trailing_stop_price, current_trail_percent
    price = float(client_live.futures_symbol_ticker(symbol=SYMBOL)['price'])
    if not entry_price: return

    profit_pct = (price - entry_price) / entry_price if trade_direction == 'long' else (entry_price - price) / entry_price
    profit_pct = abs(profit_pct)

    if profit_pct >= 0.03: current_trail_percent = 0.015
    elif profit_pct >= 0.02: current_trail_percent = 0.01
    elif profit_pct >= 0.01: current_trail_percent = 0.005

    if trade_direction == 'long':
        if trailing_peak is None or price > trailing_peak:
            trailing_peak = price
            trailing_stop_price = trailing_peak * (1 - current_trail_percent)
        if current_trail_percent > 0 and price <= trailing_stop_price:
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
        elif price >= tp_price:
            close_position(price, "Take Profit Hit")
        elif price <= sl_price:
            close_position(price, "Stop Loss Hit")
    else:
        if trailing_peak is None or price < trailing_peak:
            trailing_peak = price
            trailing_stop_price = trailing_peak * (1 + current_trail_percent)
        if current_trail_percent > 0 and price >= trailing_stop_price:
            close_position(price, f"Trailing Stop Hit ({current_trail_percent*100:.1f}%)")
        elif price <= tp_price:
            close_position(price, "Take Profit Hit")
        elif price >= sl_price:
            close_position(price, "Stop Loss Hit")

# ❌ Close trade
def close_position(exit_price, reason):
    global in_position, target_hit, last_tp_hit_time, last_loss_pause_time
    side = SIDE_SELL if trade_direction == 'long' else SIDE_BUY
    client_testnet.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    pnl = round((exit_price - entry_price) if trade_direction == 'long' else (entry_price - exit_price), 2)
    is_win = pnl > 0
    daily_trades.append((pnl, is_win))

    # 🆕 Track SL streak
    if "Stop Loss" in reason:
        recent_losses.append("SL")
        if len(recent_losses) == 4 and all(r == "SL" for r in recent_losses):
            last_loss_pause_time = datetime.utcnow()
            send_telegram("⏸ Bot pausing for 1 hour due to 4 consecutive Stop Losses.")
    else:
        recent_losses.clear()

    if "Take Profit" in reason:
        last_tp_hit_time = datetime.utcnow()

    total_pnl = sum(p for p,_ in daily_trades)
    if total_pnl >= DAILY_TARGET or total_pnl <= DAILY_LOSS_LIMIT:
        target_hit = True

    send_telegram(f"❌ *Closed at:* `{exit_price}`\n*Reason:* {reason}\n*PnL:* `{pnl}`")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"close({trade_direction})", entry_price, sl_price, tp_price, f"{reason},PnL:{pnl}"])
    in_position = False

# ⏱ Cancel untriggered stop orders after 10 minutes
def cancel_expired_order():
    global pending_order_id, pending_order_time
    if pending_order_id and pending_order_time:
        age = datetime.utcnow() - pending_order_time
        if age.total_seconds() > 600:
            try:
                client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
                send_telegram("⌛ *Pending stop order canceled after 10 minutes*")
            except: pass
            pending_order_id, pending_order_time = None, None

# 🚀 Bot loop
def bot_loop():
    global in_position, pending_order_id, entry_price, trailing_peak, trailing_stop_price, current_trail_percent, last_loss_pause_time
    while True:
        try:
            # 🆕 Pause for 1 hour after 4 SL in a row
            if last_loss_pause_time:
                if datetime.utcnow() - last_loss_pause_time < timedelta(hours=1):
                    time.sleep(60)
                    continue
                else:
                    last_loss_pause_time = None

            if not in_position:
                if pending_order_id:
                    order = client_testnet.futures_get_order(symbol=SYMBOL, orderId=pending_order_id)
                    if order['status'] == 'FILLED':
                        entry_price = float(order.get('avgFillPrice') or order.get('stopPrice'))
                        in_position, trailing_peak, trailing_stop_price, current_trail_percent = True, entry_price, None, 0.0
                        send_telegram(f"✅ *STOP order triggered*\n*Entry Price:* `{entry_price}`\n*Direction:* `{trade_direction}`")
                        log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"Triggered({trade_direction})", entry_price, sl_price, tp_price, "Opened"])
                        pending_order_id, pending_order_time = None, None
                    else:
                        cancel_expired_order()
                else:
                    s = check_signal()
                    if s: place_order(s)
            else:
                manage_trade()
        except Exception as e:
            print("Error in loop:", e)
        time.sleep(120)

# 🌐 Flask & daily report
app = Flask(__name__)
@app.route('/')
def home(): return "🚀 Bot is live."

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
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
