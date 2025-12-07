from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (GỌN NHẸ - CHUYÊN BIẾN ĐỘNG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # API KEY TWELVE DATA (Của bạn)
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # 1. CẢNH BÁO VÀNG
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # 2. CẢNH BÁO NỖI SỢ (RISK)
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "MOVE_PCT_LIMIT": 5.0,  # MOVE tăng > 5% là bão
    
    "ALERT_COOLDOWN": 3600
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'move': {'p': 0, 'c': 0, 'pct': 0}, # Chỉ số MOVE
    'last_success_time': 0,
    'last_dashboard_time': 0
}

last_alert_times = {}

def get_vn_time(): return datetime.utcnow() + timedelta(hours=7)

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================================
# 2. VÀNG (TWELVE DATA - KHÔNG BACKUP)
# ==============================================================================
def calculate_rsi(prices, periods=14):
    if len(prices) < periods + 1: return 50
    delta = pd.Series(prices).diff()
    gain = (delta.where(delta > 0, 0)).rolling(periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def get_gold_api():
    try:
        # Giá
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        d = r.json()
        
        # Nến
        url2 = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1h&outputsize=20&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r2 = requests.get(url2, timeout=10)
        d2 = r2.json()
        
        if 'close' in d and 'values' in d2:
            candles = d2['values']
            current = candles[0]
            
            # Giá & RSI
            closes = [float(c['close']) for c in candles][::-1]
            rsi = calculate_rsi(closes)
            
            # H1
            h1 = float(current['high']) - float(current['low'])

            return {
                'p': float(d['close']), 
                'c': float(d['change']), 
                'pct': float(d['percent_change']), 
                'h1': h1, 'rsi': rsi, 'src': 'API Forex'
            }
    except: pass
    
    # Nếu lỗi, dùng Cache cũ
    if GLOBAL_CACHE['gold']['p'] > 0:
        old = GLOBAL_CACHE['gold'].copy()
        old['src'] = "Mất kết nối (Giá cũ)"
        return old
    
    return {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Lỗi API'}

# ==============================================================================
# 3. MACRO (YAHOO - VIX, GVZ, MOVE)
# ==============================================================================
def get_yahoo_data(symbol):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            cur = closes[-1]; prev = closes[-2]
            return cur, cur - prev, (cur - prev)/prev*100
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 5 phút cập nhật 1 lần
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    # 1. VIX
    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 2. GVZ
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 3. MOVE (MỚI)
    # Mã MOVE trên Yahoo thường là ^MOVE hoặc ^MOVEINDEX. Nếu Yahoo chặn thì sẽ N/A.
    res = get_yahoo_data("^MOVE")
    if res: GLOBAL_CACHE['move'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

# ==============================================================================
# 4. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V95 - Volatility Trio"

@app.route('/test')
def run_test():
    gold = get_gold_api()
    GLOBAL_CACHE['gold'] = gold
    send_tele(f"🔔 TEST OK. Gold: {gold['p']} ({gold['src']})")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold = get_gold_api()
        GLOBAL_CACHE['gold'] = gold
        
        try: update_macro_data()
        except: pass
        macro = GLOBAL_CACHE
        
        alerts = []
        now = time.time()
        
        # CẢNH BÁO VÀNG
        if gold['p'] > 0:
            if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {gold['rsi']:.0f} + H1 chạy {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {gold['rsi']:.0f} + H1 sập {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
                if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 biến động {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now

        # CẢNH BÁO BIẾN ĐỘNG
        if macro['move']['pct'] > CONFIG['MOVE_PCT_LIMIT']:
             if now - last_alert_times.get('MOVE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌋 <b>MOVE SỐC:</b> +{macro['move']['pct']:.2f}% (Bão Trái Phiếu)")
                last_alert_times['MOVE'] = now

        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG:</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # DASHBOARD
        vn_now = get_vn_time()
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            gold_p = f"{gold['p']:.1f}" if gold['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"Nguồn Vàng: {gold['src']}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (FOREX):</b> {gold_p}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk Sentiment (Nỗi sợ):</b>\n"
                f"   • VIX (CK): {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ (Vàng): {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
                f"   • MOVE (Bond): {fmt(macro['move']['p'], macro['move']['c'], macro['move']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
