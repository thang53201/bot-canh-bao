from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz
import json

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
    "INF_10Y_LIMIT": 0.25, 
    "FED_PCT_LIMIT": 15.0,
    "ALERT_COOLDOWN": 3600
}

# Cache khởi tạo sẵn giá trị trung bình để không bị N/A lúc mới bật
GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Đang tải...'},
    'vix': {'p': 18.5, 'c': 0, 'pct': 0}, # Giá trị mồi
    'gvz': {'p': 22.0, 'c': 0, 'pct': 0}, # Giá trị mồi
    'inf10': {'p': 2.23, 'c': 0}, 
    'inf05': {'p': 2.29, 'c': 0}, 
    'fed': {'prob': 0, 'label': 'Đang tải...', 'change': 0},
    'spdr': {'v': 0, 'c': 0},
    'be_source': 'Đang tải...',
    'last_macro_update': 0,
    'last_dashboard_time': 0
}

last_alert_times = {}

def get_vn_time(): return datetime.utcnow() + timedelta(hours=7)

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=8)
    except: pass

# ==============================================================================
# 2. NGUỒN DỰ PHÒNG MỚI: CNBC API (CHO VIX)
# ==============================================================================
def get_cnbc_vix():
    """Lấy VIX từ CNBC nếu Yahoo chết"""
    try:
        url = "https://quote.cnbc.com/quote-html-webservice/quote.htm?partnerId=2&requestMethod=quick&exthrs=1&noform=1&fund=1&output=json&symbols=.VIX"
        r = requests.get(url, timeout=5)
        data = r.json()['QuickQuoteResult']['QuickQuote']
        
        if isinstance(data, list): item = data[0]
        else: item = data
            
        return {'p': float(item['last']), 'c': float(item['change']), 'pct': float(item['change_pct'].replace('%',''))}
    except: return None

# ==============================================================================
# 3. CÁC NGUỒN KHÁC (GIỮ NGUYÊN TỪ V70)
# ==============================================================================
def get_fred_breakeven(series_id):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[series_id] != '.']
            df[series_id] = pd.to_numeric(df[series_id])
            if len(df) >= 2:
                return float(df.iloc[-1][series_id]), float(df.iloc[-1][series_id]) - float(df.iloc[-2][series_id])
    except: return None

def get_cme_fedwatch():
    try:
        url = "https://www.cmegroup.com/CmeWS/mvc/XS/json/FedWatch/ALL"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cmegroup.com/",
            "Origin": "https://www.cmegroup.com"
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            meeting = data[0]
            prob_list = meeting['problist']
            best = max(prob_list, key=lambda x: float(x['probability']))
            return float(best['probability']) * 100, f"{best['min']}-{best['max']}"
    except: return None, None

def get_gold_binance():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5)
        d = r.json()
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
        return {'p': float(d['lastPrice']), 'c': float(d['priceChange']), 'pct': float(d['priceChangePercent']), 'h1': h1, 'rsi': curr_rsi, 'src': 'Binance'}
    except: return None

def get_yahoo_data(symbol):
    try:
        uas = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)']
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": random.choice(uas)}, timeout=8)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            return closes[-1], closes[-1]-closes[-2], (closes[-1]-closes[-2])/closes[-2]*100
    except: return None

def get_spdr_smart():
    try:
        r = requests.get("https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv", headers={"User-Agent": "Mozilla/5.0"}, timeout=10, verify=False)
        df = pd.read_csv(io.StringIO(r.text), skiprows=6)
        c = [x for x in df.columns if "Tonnes" in str(x)]
        if c:
            df = df.dropna(subset=[c[0]])
            return float(df.iloc[-1][c[0]]), float(df.iloc[-1][c[0]]) - float(df.iloc[-2][c[0]])
    except: return None

# ==============================================================================
# 4. UPDATE LOGIC (NÂNG CẤP VIX BACKUP)
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_macro_update'] < 300: return

    # 1. VIX (Yahoo -> CNBC)
    res = get_yahoo_data("^VIX")
    if res: 
        GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    else:
        cnbc_vix = get_cnbc_vix() # Backup
        if cnbc_vix: GLOBAL_CACHE['vix'] = cnbc_vix

    # 2. GVZ (Yahoo -> Giữ cũ)
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 3. SPDR
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    # 4. LẠM PHÁT (FRED -> Yahoo)
    # Ưu tiên FRED vì chuẩn hơn
    inf10 = get_fred_breakeven("T10YIE")
    if inf10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
        GLOBAL_CACHE['inf10'] = {'p': inf10[0], 'c': inf10[1]}
    else:
        res10 = get_yahoo_data("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (Yahoo)"
            GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
        else:
             GLOBAL_CACHE['be_source'] = "Lạm phát (Chờ...)"

    inf05 = get_fred_breakeven("T5YIE")
    if inf05: GLOBAL_CACHE['inf05'] = {'p': inf05[0], 'c': inf05[1]}

    # 5. CME FEDWATCH (Real API)
    prob, label = get_cme_fedwatch()
    if prob:
        old_prob = GLOBAL_CACHE['fed']['prob']
        change = prob - old_prob if old_prob > 0 else 0.0
        GLOBAL_CACHE['fed'] = {'prob': prob, 'label': label, 'change': change}
    
    GLOBAL_CACHE['last_macro_update'] = current_time

def get_data_final():
    gold = get_gold_binance()
    if not gold: 
        if GLOBAL_CACHE['gold']['p'] > 0: gold = GLOBAL_CACHE['gold']
        else: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'}
    
    try: update_macro_data()
    except: pass
    
    GLOBAL_CACHE['gold'] = gold
    return gold, GLOBAL_CACHE

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V71 - CNBC VIX Backup"

@app.route('/test')
def run_test():
    gold, _ = get_data_final()
    send_tele(f"🔔 TEST OK. Gold: {gold['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # ALERT VÀNG
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

        # ALERT MACRO
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG:</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT'] and macro['inf10']['p'] > 0:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now

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
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ..."
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            def fmt_pts(val, chg): return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" if val else "N/A"
            
            # Format Fed
            if macro['fed']['prob'] > 0:
                fed_txt = f"Kịch bản {macro['fed']['label']}: <b>{macro['fed']['prob']:.1f}%</b>"
            else: fed_txt = "Đang tải..."

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {gold['p']:.1f}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{macro['be_source']}:</b>\n"
                f"   • 10Y: {fmt_pts(macro['inf10']['p'], macro['inf10']['c'])}\n"
                f"   • 05Y: {fmt_pts(macro['inf05']['p'], macro['inf05']['c'])}\n"
                f"-------------------------------\n"
                f"🏦 <b>CME FedWatch (Dự báo):</b>\n"
                f"   • {fed_txt}\n"
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
