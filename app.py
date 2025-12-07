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
# 1. CẤU HÌNH (GỌN NHẸ - CHỈ CÒN RISK & GOLD)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # API KEY TWELVE DATA (Của bạn)
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # CẢNH BÁO VÀNG (1 PHÚT)
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # CẢNH BÁO RỦI RO (5 PHÚT)
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0, # Chứng khoán
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0, # Vàng
    "MOVE_PCT_LIMIT": 5.0,                      # Trái phiếu (Mới)
    
    "ALERT_COOLDOWN": 3600
}

# Cache chỉ lưu đúng 4 món này
GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'move': {'p': 0, 'c': 0, 'pct': 0},
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
# 2. HÀM LẤY VÀNG (TWELVE DATA + BINANCE BACKUP)
# ==============================================================================
def get_gold_forex_api():
    try:
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        d = r.json()
        if 'close' in d:
            tech = get_gold_binance_tech()
            return {'p': float(d['close']), 'c': float(d['change']), 'pct': float(d['percent_change']), 'h1': tech['h1'], 'rsi': tech['rsi'], 'src': 'Forex API'}
    except: pass
    return None

def get_gold_binance_tech():
    try:
        k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5).json()
        closes = [float(x[4]) for x in k]
        if len(closes) >= 15:
            delta = pd.Series(closes).diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            last = k[-1]
            h1 = float(last[2]) - float(last[3])
            return {'h1': h1, 'rsi': float(rsi.iloc[-1])}
    except: return {'h1': 0, 'rsi': 50}

def get_gold_binance_full():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5).json()
        tech = get_gold_binance_tech()
        return {'p': float(r['lastPrice']), 'c': float(r['priceChange']), 'pct': float(r['priceChangePercent']), 'h1': tech['h1'], 'rsi': tech['rsi'], 'src': 'Binance (Backup)'}
    except: return None

def get_gold_final():
    gold = get_gold_forex_api()
    if not gold: gold = get_gold_binance_full()
    if not gold:
        if GLOBAL_CACHE['gold']['p'] > 0: gold = GLOBAL_CACHE['gold']
        else: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    GLOBAL_CACHE['gold'] = gold
    return gold

# ==============================================================================
# 3. HÀM LẤY VĨ MÔ (VIX, GVZ, MOVE - YAHOO)
# ==============================================================================
def get_yahoo_data(symbol):
    try:
        # Random User-Agent
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15'
        ]
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": random.choice(uas)}, timeout=5)
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

    # 1. VIX (Sợ hãi CK)
    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 2. GVZ (Sợ hãi Vàng)
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 3. MOVE (Sợ hãi Trái phiếu)
    res = get_yahoo_data("^MOVE")
    if res: GLOBAL_CACHE['move'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

# ==============================================================================
# 4. ROUTING & RUN
# ==============================================================================
@app.route('/')
def home(): return "Bot V90 - Clean & Volatility Focused"

@app.route('/test')
def run_test():
    gold = get_gold_final()
    send_tele(f"🔔 TEST OK. Gold: {gold['p']} ({gold['src']})")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold = get_gold_final() # Lấy Vàng
        try: update_macro_data() # Lấy VIX/GVZ/MOVE
        except: pass
        
        macro = GLOBAL_CACHE
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO VÀNG (1 PHÚT) ---
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
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now

        # --- CẢNH BÁO BIẾN ĐỘNG (5 PHÚT) ---
        
        # 1. MOVE (>5% là bão lớn)
        if macro['move']['pct'] > CONFIG['MOVE_PCT_LIMIT']:
             if now - last_alert_times.get('MOVE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌋 <b>MOVE INDEX TĂNG SỐC:</b> +{macro['move']['pct']:.2f}% (Bão Trái Phiếu)")
                last_alert_times['MOVE'] = now

        # 2. VIX
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        
        # 3. GVZ
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG:</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD ---
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
                f"🥇 <b>GOLD (XAU/USD):</b> {gold_p}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk Sentiment (Nỗi sợ):</b>\n"
                f"   • VIX (Chứng khoán): {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ (Vàng): {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
                f"   • MOVE (Trái phiếu): {fmt(macro['move']['p'], macro['move']['c'], macro['move']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
