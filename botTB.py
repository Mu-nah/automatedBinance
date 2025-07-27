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
SYMBOL, TRADE_QUANTITY, SPREAD_THRESHOLD, DAILY_TARGET = "BTCUSDT", 0.001, 17, 1000
RSI_LO, RSI_HI, ENTRY_BUFFER = 47, 53, 0.8
TELEGRAM_TOKEN, CHAT_ID, GSHEET_ID = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), os.getenv("GSHEET_ID")

# ✅ Clients
client_testnet = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
client_testnet.futures_change_leverage(symbol=SYMBOL, leverage=10)
client_live = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ✅ State
in_position, pending_order_id, pending_order_side, pending_order_time = False, None, None, None
entry_price, sl_price, tp_price, trailing_peak, current_trail_percent = None, None, None, None, 0.0
trade_direction, daily_trades, target_hit = None, deque(), False

# 📩 Telegram
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except: pass

# 📊 Google Sheets
def get_gsheet_client():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope=['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds,scope))
def log_trade_to_sheet(data):
    try: get_gsheet_client().open_by_key(GSHEET_ID).sheet1.append_row(data)
    except: pass

# 📊 Data & indicators
def get_klines(interval='5m', limit=100):
    df = pd.DataFrame(client_live.futures_klines(symbol=SYMBOL, interval=interval, limit=limit),
        columns=['open_time','open','high','low','close','volume','close_time',
                 'quote_asset_volume','number_of_trades','taker_buy_base','taker_buy_quote','ignore'])
    df['time']=pd.to_datetime(df['open_time'],unit='ms')
    for col in ['open','high','low','close','volume']: df[col]=df[col].astype(float)
    return df

def add_indicators(df):
    df['rsi']=ta.momentum.rsi(df['close'],14)
    bb=ta.volatility.BollingerBands(df['close'],20,2)
    df['bb_mid'],df['bb_high'],df['bb_low']=bb.bollinger_mavg(),bb.bollinger_hband(),bb.bollinger_lband()
    return df

# 📊 Signal logic (live forming candles)
def check_signal():
    if target_hit: return None
    df_5m, df_1h = add_indicators(get_klines('5m')), add_indicators(get_klines('1h'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    if now.minute >= 50: return None
    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI: return None
    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']: return None

    if c5['close']>c5['bb_mid'] and c5['close']<c5['bb_high'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'trend_buy'
    if c5['close']<c5['bb_mid'] and c5['close']>c5['bb_low'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'trend_sell'
    if c5['close']<c5['bb_mid'] and c5['close']>c5['bb_low'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'reversal_buy'
    if c5['close']>c5['bb_mid'] and c5['close']<c5['bb_high'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'reversal_sell'
    return None

# 🛠 Place stop order (TP ≈ 100 pips above/below BB line)
def place_order(order_type):
    global pending_order_id, pending_order_side, pending_order_time, sl_price, tp_price, trade_direction
    if target_hit or in_position: return
    side='buy' if 'buy' in order_type else 'sell'

    if pending_order_id and pending_order_side!=side:
        try: client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except: pass
        send_telegram("⚠ *Canceled previous pending order* (opposite signal)")
        pending_order_id=None

    ob=client_live.futures_order_book(symbol=SYMBOL)
    ask,bid=float(ob['asks'][0][0]),float(ob['bids'][0][0])
    if ask-bid>SPREAD_THRESHOLD: return
    stop=round(ask+ENTRY_BUFFER,2) if 'buy' in order_type else round(bid-ENTRY_BUFFER,2)
    df_1h, df_5m = add_indicators(get_klines('1h')), add_indicators(get_klines('5m'))
    c1h,c5 = df_1h.iloc[-1], df_5m.iloc[-1]
    sl_price = c1h['open'] if 'trend' in order_type else c5['open']
    bb_tp = c5['bb_high'] if 'buy' in order_type else c5['bb_low']
    tp_price = round(bb_tp + 100 if 'buy' in order_type else bb_tp - 100, 2)
    trade_direction = 'long' if 'buy' in order_type else 'short'

    res=client_testnet.futures_create_order(symbol=SYMBOL, side=SIDE_BUY if 'buy' in order_type else SIDE_SELL,
        type=FUTURE_ORDER_TYPE_STOP_MARKET, stopPrice=stop, quantity=TRADE_QUANTITY)
    pending_order_id, pending_order_side, pending_order_time = res['orderId'], side, datetime.utcnow()

    send_telegram(f"🟩 *STOP ORDER PLACED*\n*Type:* `{order_type.upper()}`\n*Price:* `{stop}`\n*SL:* `{sl_price}` | *TP:* `{tp_price}`\n📍 Pending *({trade_direction})*")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, order_type, stop, sl_price, tp_price, f"Pending({trade_direction})"])

# 🛑 Cancel if pending >10min
def cancel_pending_if_needed():
    global pending_order_id, pending_order_time
    if pending_order_id and pending_order_time and datetime.utcnow()-pending_order_time>timedelta(minutes=10):
        try: client_testnet.futures_cancel_order(symbol=SYMBOL, orderId=pending_order_id)
        except: pass
        send_telegram("🕒 *Pending order canceled after 10 minutes*")
        pending_order_id,pending_order_time=None,None

# 🔄 Manage trade (unchanged)
def manage_trade():
    global in_position,trailing_peak,current_trail_percent
    price=float(client_live.futures_symbol_ticker(symbol=SYMBOL)['price'])
    if not entry_price: return
    profit_pct=abs((price-entry_price)/entry_price)
    if profit_pct>=0.03: current_trail_percent=0.015
    elif profit_pct>=0.02: current_trail_percent=0.01
    elif profit_pct>=0.01: current_trail_percent=0.005
    if trade_direction=='long' and price>trailing_peak: trailing_peak=price
    elif trade_direction=='short' and price<trailing_peak: trailing_peak=price
    if current_trail_percent>0:
        if trade_direction=='long' and price<=trailing_peak*(1-current_trail_percent): close_position(price,"Trailing Stop Hit")
        elif trade_direction=='short' and price>=trailing_peak*(1+current_trail_percent): close_position(price,"Trailing Stop Hit")
    if trade_direction=='long':
        if price<=sl_price: close_position(price,"Stop Loss Hit")
        elif price>=tp_price: close_position(price,"Take Profit Hit")
    else:
        if price>=sl_price: close_position(price,"Stop Loss Hit")
        elif price<=tp_price: close_position(price,"Take Profit Hit")

# ❌ Close trade
def close_position(exit_price,reason):
    global in_position,target_hit
    side=SIDE_SELL if trade_direction=='long' else SIDE_BUY
    client_testnet.futures_create_order(symbol=SYMBOL,side=side,type=ORDER_TYPE_MARKET,quantity=TRADE_QUANTITY)
    pnl=round((exit_price-entry_price) if trade_direction=='long' else (entry_price-exit_price),2)
    daily_trades.append((pnl,pnl>0))
    if sum(p for p,_ in daily_trades)>=DAILY_TARGET: target_hit=True
    send_telegram(f"❌ *Closed at:* `{exit_price}`\n*Reason:* {reason}\n*PnL:* `{pnl}`")
    log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"close({trade_direction})", entry_price, sl_price, tp_price, f"{reason},PnL:{pnl}"])
    in_position=False

# 📊 Daily summary
def send_daily_summary():
    if not daily_trades:
        send_telegram("📊 *Daily Summary:*\n_No trades today._")
        return
    total_pnl = sum(p for p,_ in daily_trades)
    num_wins = sum(1 for _,w in daily_trades if w)
    msg = f"""📊 *Daily Summary*
Total Trades: *{len(daily_trades)}*
Win Rate: *{(num_wins/len(daily_trades))*100:.1f}%*
Total PnL: *{total_pnl:.2f}*
Biggest Win: *{max((p for p,_ in daily_trades if p>0),default=0)}*
Biggest Loss: *{min((p for p,_ in daily_trades if p<0),default=0)}*
{'🎯 *Target hit ✅*' if target_hit else '🎯 *Target not reached ❌*'}"""
    send_telegram(msg)

# 🚀 Bot loop
def bot_loop():
    global in_position,pending_order_id,entry_price,trailing_peak,current_trail_percent
    while True:
        try:
            if not in_position:
                cancel_pending_if_needed()
                if pending_order_id:
                    order=client_testnet.futures_get_order(symbol=SYMBOL,orderId=pending_order_id)
                    if order['status']=='FILLED':
                        entry_price=float(order.get('avgFillPrice') or order.get('stopPrice'))
                        in_position,trailing_peak,current_trail_percent=True,entry_price,0.0
                        send_telegram(f"✅ *STOP order triggered*\n*Entry Price:* `{entry_price}`\n*Direction:* `{trade_direction}`")
                        log_trade_to_sheet([str(datetime.utcnow()), SYMBOL, f"Triggered({trade_direction})", entry_price, sl_price, tp_price, "Opened"])
                        pending_order_id,pending_order_time=None,None
                else:
                    s=check_signal()
                    if s: place_order(s)
            else: manage_trade()
        except: pass
        time.sleep(120)

# 🌐 Flask & daily summary
app=Flask(__name__)
@app.route('/')
def home(): return "🚀 Live bot running!"

def daily_report_loop():
    while True:
        now=datetime.utcnow()+timedelta(hours=1)
        next_midnight=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        time.sleep((next_midnight-now).total_seconds())
        send_daily_summary()
        daily_trades.clear()

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    threading.Thread(target=bot_loop,daemon=True).start()
    threading.Thread(target=daily_report_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=port)
