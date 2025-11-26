from flask import Flask
import requests
import pandas as pd
import io
import time
import random
import re
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
    "INF_10Y_LIMIT": 0.25, 
    "FED_PCT_LIMIT": 15.0,
    "ALERT_COOLDOWN": 3600
}

GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W (Proxy)'},
    'spdr': {'v': 0, 'c': 0},
    'be_source': 'Chờ...',
    'last_success_time': 0,
    'last_dashboard_time': 0
}

last_alert_times = {}

# ==============================================================================
# 2. BỘ LỌC DỮ LIỆU RÁC (QUAN TRỌNG NHẤT BẢN NÀY)
# ==============================================================================
def is_sane(symbol, value):
    """
    Kiểm tra xem con số có 'điên' không.
    Nếu số liệu quá vô lý so với thực tế thị trường -> Trả về False (Rác).
    """
    if value is None or value == 0: return False
    
    # Lạm phát/Yield Mỹ: Không bao giờ vượt quá 10% trong điều kiện hiện nay
    if symbol in ['^T10YIE', '^T5YIE', '^TNX', '^FVX', '^IRX']:
        if value > 10.0 or value < -5.0: return False
        
    # VIX/GVZ: Mức kỷ lục lịch sử là ~89. Nếu > 90 là số ảo.
    if symbol in ['^VIX', '^GVZ']:
        if value > 90.0 or value < 0: return False
        
    return True

# ==============================================================================
# 3. HÀM LẤY DATA (CÓ ÁP DỤNG BỘ LỌC)
# ==============================================================================
def get_yahoo_robust(symbol):
    val, chg, pct = None, None, None
    
    # CÁCH 1: API JSON (Ưu tiên)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            val, chg, pct = closes[-1], closes[-1]-closes[-2], (closes[-1]-closes[-2])/closes[-2]*100
    except: pass

    # CÁCH 2: HTML REGEX (Dự phòng)
    if not val:
        try:
            url = f"https://finance.yahoo.com/quote/{symbol}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            p = re.search(r'regularMarketPrice":{"raw":([0-9\.]+)', r.text)
            c = re.search(r'regularMarketChange":{"raw":(-?[0-9\.]+)', r.text)
            per = re.search(r'regularMarketChangePercent":{"raw":(-?[0-9\.]+)', r.text)
            if p and c and per:
                val, chg, pct = float(p.group(1)), float(c.group(1)), float(per.group(1))
        except: pass

    # KIỂM TRA HỢP LÝ: Nếu số liệu "điên" -> Trả về None ngay
    if is_sane(symbol, val):
        return val, chg, pct
    else:
        return None

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

def get_spdr_smart():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
    except: return None

# ==============================================================================
# 4. UPDATE LOGIC
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    # VIX & GVZ
    res = get_yahoo_robust("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_robust("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    # LẠM PHÁT (Chỉ lấy Breakeven chuẩn, nếu lỗi thì N/A)
    res10 = get_yahoo_robust("^T10YIE")
    if res10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (Breakeven)"
        GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
    else:
        GLOBAL_CACHE['be_source'] = "Lạm phát (Chờ cập nhật)"
        # Không ghi đè nếu lỗi, giữ cache cũ

    res05 = get_yahoo_robust("^T5YIE")
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}

    # FEDWATCH (Proxy ^IRX)
    res_fed = get_yahoo_robust("^IRX")
    if res_fed:
        GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W (Proxy)'}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    gold = get_gold_binance()
    if not gold: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    try: update_macro_data()
    except: pass
    return gold, GLOBAL_CACHE

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ==============================================================================
# 5. ROUTING & CHECK
# ==============================================================================
@app.route('/')
def home(): return "Bot V48 - Sanity Check Added"

@app.route('/test')
def run_test():
    try:
        gold, _ = get_data_final()
        send_tele(f"🔔 TEST OK. Gold: {gold['p']}")
        return "OK", 200
    except: return "Err", 500

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # CẢNH BÁO
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
        
        # CHỈ CẢNH BÁO VĨ MÔ NẾU DỮ LIỆU HỢP LÝ ( < 100 )
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] and macro['vix']['p'] < 90:
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

        # DASHBOARD
        vn_now = datetime.utcnow() + timedelta(hours=7)
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            def fmt_pts(val, chg): return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" if val else "N/A"

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
