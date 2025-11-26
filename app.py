from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta

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
    "INF_10Y_LIMIT": 0.25, "INF_05Y_LIMIT": 0.20,
    "FED_PCT_LIMIT": 15.0,
    "ALERT_COOLDOWN": 3600
}

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

# ==============================================================================
# 2. HÀM LẤY GIỜ VIỆT NAM (CHUẨN UTC+7)
# ==============================================================================
def get_vn_time():
    # Lấy giờ UTC gốc của server + 7 tiếng
    return datetime.utcnow() + timedelta(hours=7)

# ==============================================================================
# 3. CÁC HÀM LẤY DỮ LIỆU
# ==============================================================================
def get_fred_data(series_id):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=5) # Giảm timeout
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[series_id] != '.']
            df[series_id] = pd.to_numeric(df[series_id])
            if len(df) >= 2:
                curr = float(df.iloc[-1][series_id])
                prev = float(df.iloc[-2][series_id])
                return curr, curr - prev
        return None
    except: return None

def get_gold_binance():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5)
        data = r.json()
        kr = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5)
        k_data = kr.json()
        closes = [float(x[4]) for x in k_data]
        
        if len(closes) >= 15:
            prices = pd.Series(closes)
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            curr_rsi = float(rsi.iloc[-1])
        else: curr_rsi = 50.0

        last = k_data[-1]
        h1 = float(last[2]) - float(last[3])

        return {'p': float(data['lastPrice']), 'c': float(data['priceChange']), 'pct': float(data['priceChangePercent']), 'h1': h1, 'rsi': curr_rsi, 'src': 'Binance'}
    except: return None

def get_yahoo_data(symbol):
    try:
        uas = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)']
        headers = {"User-Agent": random.choice(uas)}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        res = data['chart']['result'][0]
        quote = res['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        if len(closes) < 2: return None
        cur = closes[-1]; prev = closes[-2]
        return cur, cur - prev, (cur - prev)/prev*100
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
                if len(df) >= 2:
                    return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
        return None
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 5 phút (300s) cập nhật 1 lần
    if current_time - GLOBAL_CACHE['last_success_time'] < 300:
        return
        
    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    res10 = get_yahoo_data("^T10YIE")
    if res10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (Yahoo)"
        GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
    else:
        fred10 = get_fred_data("T10YIE")
        if fred10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
            GLOBAL_CACHE['inf10'] = {'p': fred10[0], 'c': fred10[1]}
        else:
            res10y = get_yahoo_data("^TNX")
            if res10y:
                GLOBAL_CACHE['be_source'] = "Lợi suất (Backup)"
                GLOBAL_CACHE['inf10'] = {'p': res10y[0], 'c': res10y[1]}

    res05 = get_yahoo_data("^T5YIE")
    if res05:
        GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    else:
        fred05 = get_fred_data("T5YIE")
        if fred05:
            GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}
        else:
            res05y = get_yahoo_data("^FVX")
            if res05y:
                GLOBAL_CACHE['inf05'] = {'p': res05y[0], 'c': res05y[1]}

    res_fed = get_yahoo_data("^IRX")
    if res_fed:
        GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W'}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    gold = get_gold_binance()
    if not gold: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    update_macro_data()
    return gold, GLOBAL_CACHE

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ==============================================================================
# 4. ROUTING & TEST (DEBUG THỜI GIAN)
# ==============================================================================
@app.route('/')
def home(): return "Bot V38 - Time Debug"

@app.route('/test')
def run_test():
    try:
        vn_now = get_vn_time() # Lấy giờ VN
        gold, _ = get_data_final()
        
        msg = (
            f"🔔 <b>TEST KẾT NỐI</b>\n"
            f"🕒 Giờ Server (VN): {vn_now.strftime('%H:%M:%S')}\n"
            f"👉 Nếu giờ này sai, bảng tin sẽ không gửi.\n"
            f"🥇 Gold: {gold['p']}"
        )
        send_tele(msg)
        return "OK", 200
    except Exception as e: return f"Err: {e}", 500

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
        
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now
        if abs(macro['inf05']['c']) > CONFIG['INF_05Y_LIMIT']:
            if now - last_alert_times.get('INF05', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT NGẮN HẠN:</b> Đổi {abs(macro['inf05']['c']):.3f} điểm")
                last_alert_times['INF05'] = now
        if abs(macro['fed']['pct']) > CONFIG['FED_PCT_LIMIT']:
            if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED WATCH BIẾN ĐỘNG:</b> Đổi {abs(macro['fed']['pct']):.1f}%")
                last_alert_times['FED'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # DASHBOARD
        vn_now = get_vn_time() # Dùng hàm tính giờ chuẩn
        
        # Cho phép gửi trong khoảng 10 phút đầu (0-10 và 30-40) để tránh miss khi server lag
        is_time = vn_now.minute in [0,1,2,3,4,5,6,7,8,9,10,30,31,32,33,34,35,36,37,38,39,40]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1200): # Cách nhau ít nhất 20p
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
                f"   • Mức: {fmt(macro['fed']['p'], macro['fed']['c'], macro['fed']['pct'])}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ: {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Err: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
