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
    
    # LẠM PHÁT (Điểm số)
    "INF_10Y_LIMIT": 0.25,
    "INF_05Y_LIMIT": 0.20,
    
    # FEDWATCH (%)
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600
}

# Bộ nhớ tạm đơn giản (chỉ để chống spam alert)
CACHE = {'last_alert': {}} 

# ==============================================================================
# 2. CÁC HÀM LẤY DỮ LIỆU (CỰC NHANH - TIMEOUT 5S)
# ==============================================================================
def get_headers():
    return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def get_gold_binance():
    try:
        # Lấy giá
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5)
        d = r.json()
        # Lấy nến
        k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5)
        k_data = k.json()
        
        # Tính RSI
        closes = [float(x[4]) for x in k_data]
        if len(closes) >= 15:
            delta = pd.Series(closes).diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            curr_rsi = float(rsi.iloc[-1])
        else: curr_rsi = 50.0

        # Tính H1
        last = k_data[-1]
        h1 = float(last[2]) - float(last[3])

        return {'p': float(d['lastPrice']), 'c': float(d['priceChange']), 'pct': float(d['priceChangePercent']), 'h1': h1, 'rsi': curr_rsi}
    except: return None

def get_yahoo_strict(symbol):
    try:
        # API V8 gọn nhẹ
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=get_headers(), timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) < 2: return None
        cur = closes[-1]; prev = closes[-2]
        return cur, cur - prev, (cur - prev)/prev*100
    except: return None

def get_fred_strict(series_id):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = requests.get(url, headers=get_headers(), timeout=5)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[series_id] != '.']
            df[series_id] = pd.to_numeric(df[series_id])
            if len(df) >= 2:
                return float(df.iloc[-1][series_id]), float(df.iloc[-1][series_id]) - float(df.iloc[-2][series_id])
        return None
    except: return None

def get_spdr():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers=get_headers(), timeout=5, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
        return None
    except: return None

# ==============================================================================
# 3. TỔNG HỢP DATA (KHÔNG TRÁO ĐỔI KHÁI NIỆM)
# ==============================================================================
def get_full_data():
    data = {}
    
    # 1. VÀNG
    data['gold'] = get_gold_binance()
    if not data['gold']: data['gold'] = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50}

    # 2. VIX & GVZ
    res = get_yahoo_strict("^VIX")
    data['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]} if res else {'p': 0, 'c': 0, 'pct': 0}
    
    res = get_yahoo_strict("^GVZ")
    data['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]} if res else {'p': 0, 'c': 0, 'pct': 0}

    # 3. SPDR
    res = get_spdr()
    data['spdr'] = {'v': res[0], 'c': res[1]} if res else {'v': 0, 'c': 0}

    # 4. LẠM PHÁT (Yahoo -> FRED -> N/A)
    # 10Y
    res10 = get_yahoo_strict("^T10YIE")
    if res10: 
        data['inf10'] = {'p': res10[0], 'c': res10[1]}
    else:
        fred10 = get_fred_strict("T10YIE")
        data['inf10'] = {'p': fred10[0], 'c': fred10[1]} if fred10 else {'p': 0, 'c': 0}
        
    # 5Y
    res05 = get_yahoo_strict("^T5YIE")
    if res05:
        data['inf05'] = {'p': res05[0], 'c': res05[1]}
    else:
        fred05 = get_fred_strict("T5YIE")
        data['inf05'] = {'p': fred05[0], 'c': fred05[1]} if fred05 else {'p': 0, 'c': 0}

    # 5. FEDWATCH (Yield 13W)
    res_fed = get_yahoo_strict("^IRX")
    data['fed'] = {'p': res_fed[0], 'pct': res_fed[2]} if res_fed else {'p': 0, 'pct': 0}

    return data

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================================
# 4. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V42 - V33 Reborn"

@app.route('/test')
def test():
    d = get_full_data()
    send_tele(f"🔔 TEST OK. Gold: {d['gold']['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        d = get_full_data()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO KHẨN ---
        if d['gold']['rsi'] > CONFIG['RSI_HIGH'] and d['gold']['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - CACHE['last_alert'].get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['gold']['rsi']:.0f} + H1 {d['gold']['h1']:.1f}$")
                CACHE['last_alert']['RSI'] = now
        
        if d['gold']['rsi'] < CONFIG['RSI_LOW'] and d['gold']['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - CACHE['last_alert'].get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['gold']['rsi']:.0f} + H1 {d['gold']['h1']:.1f}$")
                CACHE['last_alert']['RSI'] = now

        if d['gold']['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - CACHE['last_alert'].get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {d['gold']['h1']:.1f} giá")
                CACHE['last_alert']['H1'] = now
        
        # VIX
        if d['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or d['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - CACHE['last_alert'].get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {d['vix']['p']:.2f}")
                CACHE['last_alert']['VIX'] = now
        
        # LẠM PHÁT
        if abs(d['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - CACHE['last_alert'].get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(d['inf10']['c']):.3f} điểm")
                CACHE['last_alert']['INF'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD (00-05, 30-35) ---
        vn_now = datetime.utcnow() + timedelta(hours=7)
        # Chỉ gửi nếu chưa gửi trong 20 phút gần đây
        last_sent = CACHE.get('last_dash', 0)
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            # Format hiển thị
            spdr_str = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chờ..."
            spdr_chg = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""
            
            be10_str = f"{d['inf10']['p']:.2f}%" if d['inf10']['p'] > 0 else "N/A"
            be05_str = f"{d['inf05']['p']:.2f}%" if d['inf05']['p'] > 0 else "N/A"
            fed_str = f"{d['fed']['p']:.2f}%" if d['fed']['p'] > 0 else "N/A"
            
            vix_str = f"{d['vix']['p']:.2f}" if d['vix']['p'] > 0 else "N/A"
            gvz_str = f"{d['gvz']['p']:.2f}" if d['gvz']['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {d['gold']['p']:.1f}\n"
                f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {d['gold']['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_str} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>Lạm phát (Breakeven):</b>\n"
                f"   • 10Y: {be10_str} (Chg: {s(d['inf10']['c'])}{d['inf10']['c']:.3f})\n"
                f"   • 05Y: {be05_str} (Chg: {s(d['inf05']['c'])}{d['inf05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"🏦 <b>FedWatch (Yield 13W):</b>\n"
                f"   • Mức: {fed_str} ({s(d['fed']['pct'])}{d['fed']['pct']:.2f}%)\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {vix_str}\n"
                f"   • GVZ: {gvz_str}\n"
            )
            send_tele(msg)
            CACHE['last_dash'] = now
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e: return f"Err: {e}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
