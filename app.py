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
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "INF_10Y_LIMIT": 0.25, "FED_PCT_LIMIT": 15.0,
    "ALERT_COOLDOWN": 3600
}

# Cache Vĩ mô (Giữ 5 phút)
GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W'},
    'spdr': {'v': 0, 'c': 0},
    'be_source': 'Chờ...',
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
# 2. NGUỒN CẢNH BÁO: BINANCE (CỰC NHANH - KHÔNG CHẶN)
# ==============================================================================
def get_gold_alert_source():
    """Dùng Binance để tính RSI và H1 Range cho cảnh báo"""
    for _ in range(3): # Retry 3 lần
        try:
            # Giá
            r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5)
            d = r.json()
            # Nến
            k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5)
            kd = k.json()
            closes = [float(x[4]) for x in kd]
            
            if len(closes) >= 15:
                delta = pd.Series(closes).diff()
                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
                curr_rsi = float(rsi.iloc[-1])
            else: curr_rsi = 50.0

            last = kd[-1]
            h1 = float(last[2]) - float(last[3])

            return {'p': float(d['lastPrice']), 'h1': h1, 'rsi': curr_rsi, 'src': 'Binance'}
        except: time.sleep(1)
    return None

# ==============================================================================
# 3. NGUỒN DASHBOARD: YAHOO (ĐẸP - CHỈ GỌI 30P/LẦN)
# ==============================================================================
def get_gold_dashboard_source():
    """Dùng Yahoo XAUUSD=X để hiển thị giá đẹp"""
    try:
        uas = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)']
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1d&range=2d"
        r = requests.get(url, headers={"User-Agent": random.choice(uas)}, timeout=8)
        d = r.json()
        meta = d['chart']['result'][0]['meta']
        p = meta['regularMarketPrice']
        prev = meta['chartPreviousClose']
        return {'p': p, 'c': p-prev, 'pct': (p-prev)/prev*100, 'src': 'Yahoo (Spot)'}
    except: return None # Nếu Yahoo lỗi thì trả về None

# ==============================================================================
# 4. MACRO (5 PHÚT/LẦN)
# ==============================================================================
def get_yahoo_data(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        d = r.json()
        c = [x for x in d['chart']['result'][0]['indicators']['quote'][0]['close'] if x]
        if len(c) >= 2: return c[-1], c[-1]-c[-2], (c[-1]-c[-2])/c[-2]*100
    except: return None

def get_fred_data(sid):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        df = pd.read_csv(io.StringIO(r.text))
        df = df[df[sid] != '.']
        df[sid] = pd.to_numeric(df[sid])
        if len(df) >= 2: return float(df.iloc[-1][sid]), float(df.iloc[-1][sid]) - float(df.iloc[-2][sid])
    except: return None

def get_spdr_smart():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        df = pd.read_csv(io.StringIO(r.text), skiprows=6)
        c = [x for x in df.columns if "Tonnes" in str(x)]
        if c:
            df = df.dropna(subset=[c[0]])
            return float(df.iloc[-1][c[0]]), float(df.iloc[-1][c[0]]) - float(df.iloc[-2][c[0]])
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    try:
        res = get_yahoo_data("^VIX")
        if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
        res = get_yahoo_data("^GVZ")
        if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
        res = get_spdr_smart()
        if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
        
        res10 = get_yahoo_data("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (Breakeven)"
            GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
        else:
            fred10 = get_fred_data("T10YIE")
            if fred10:
                GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
                GLOBAL_CACHE['inf10'] = {'p': fred10[0], 'c': fred10[1]}
            else: GLOBAL_CACHE['be_source'] = "Lạm phát (Chờ...)"

        res05 = get_yahoo_data("^T5YIE")
        if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
        else:
            fred05 = get_fred_data("T5YIE")
            if fred05: GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}

        res_fed = get_yahoo_data("^IRX")
        if res_fed: GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W'}
        
        GLOBAL_CACHE['last_success_time'] = current_time
    except: pass

# ==============================================================================
# 5. ROUTING & RUN
# ==============================================================================
@app.route('/')
def home(): return "Bot V66 - Split Source"

@app.route('/test')
def run_test():
    gold = get_gold_alert_source()
    send_tele(f"🔔 TEST OK. Binance Gold: {gold['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        # 1. LUÔN LẤY BINANCE ĐỂ CẢNH BÁO (KHÔNG CHỜ YAHOO)
        alert_gold = get_gold_alert_source()
        
        # 2. UPDATE MACRO (NGẦM)
        try: update_macro_data()
        except: pass
        macro = GLOBAL_CACHE
        
        alerts = []
        now = time.time()
        
        # --- LOGIC CẢNH BÁO (DÙNG BINANCE) ---
        if alert_gold:
            if alert_gold['rsi'] > CONFIG['RSI_HIGH'] and alert_gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {alert_gold['rsi']:.0f} + H1 chạy {alert_gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if alert_gold['rsi'] < CONFIG['RSI_LOW'] and alert_gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {alert_gold['rsi']:.0f} + H1 sập {alert_gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if alert_gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
                if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {alert_gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now
        
        # LOGIC MACRO
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] and macro['vix']['p'] < 100:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT'] and macro['inf10']['p'] < 20:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD (DÙNG YAHOO) ---
        vn_now = get_vn_time()
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1200):
            # LÚC NÀY MỚI GỌI YAHOO ĐỂ LẤY GIÁ ĐẸP
            dash_gold = get_gold_dashboard_source()
            # Nếu Yahoo lỗi thì dùng tạm Binance
            if not dash_gold: 
                dash_gold = alert_gold
                dash_gold['src'] = "Binance (Backup)"
                # RSI của Binance
                rsi_val = alert_gold['rsi']
            else:
                # Yahoo không tính RSI chuẩn trong hàm này, dùng RSI của Binance cho chuẩn
                rsi_val = alert_gold['rsi'] if alert_gold else 50.0

            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            def fmt_pts(val, chg): return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" if val else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"Nguồn Vàng: {dash_gold['src']}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (XAU/USD):</b> {dash_gold['p']:.1f}\n"
                f"   {i(dash_gold['c'])} {s(dash_gold['c'])}{dash_gold['c']:.1f}$ ({s(dash_gold['pct'])}{dash_gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {rsi_val:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{macro['be_source']}:</b>\n"
                f"   • 10Y: {fmt_pts(macro['inf10']['p'], macro['inf10']['c'])}\n"
                f"   • 05Y: {fmt_pts(macro['inf05']['p'], macro['inf05']['c'])}\n"
                f"-------------------------------\n"
                f"🏦 <b>FedWatch ({macro['fed']['name']}):</b>\n"
                f"   • Mức: {fmt(macro['fed']['p'], 0, macro['fed']['pct'])}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ: {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
