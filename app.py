from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (ĐÃ BỔ SUNG 2Y/5Y)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO VÀNG & RSI
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # VIX & GVZ
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    
    # LẠM PHÁT (BREAKEVEN) - TÍNH BẰNG ĐIỂM (POINTS)
    "INF_10Y_LIMIT": 0.25,  # 10 Năm: > 0.25 điểm là báo
    "INF_05Y_LIMIT": 0.20,  # 2 Năm (5Y): > 0.20 điểm là báo
    
    # FEDWATCH (YIELD) - TÍNH BẰNG %
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600
}

# Cache dữ liệu (5 phút)
GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed10': {'p': 0, 'c': 0, 'pct': 0},
    'fed05': {'p': 0, 'c': 0, 'pct': 0},
    'spdr': {'v': 0, 'c': 0},
    'last_success_time': 0
}

last_alert_times = {}

# ==============================================================================
# 2. VÀNG BINANCE (REALTIME 1 PHÚT)
# ==============================================================================
def get_gold_binance():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=15)
        data = r.json()
        
        kr = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=15)
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

        return {
            'p': float(data['lastPrice']), 
            'c': float(data['priceChange']), 
            'pct': float(data['priceChangePercent']),
            'h1': h1, 'rsi': curr_rsi, 'src': 'Binance (1p)'
        }
    except: return None

# ==============================================================================
# 3. YAHOO MACRO (5 PHÚT/LẦN)
# ==============================================================================
def get_yahoo_strict(symbol):
    try:
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15'
        ]
        headers = {"User-Agent": random.choice(uas)}
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        
        if len(closes) < 2: return None
        
        cur = closes[-1]
        prev = closes[-2]
        # c = Thay đổi điểm số (Dùng cho Lạm phát)
        # pct = Thay đổi % (Dùng cho VIX/Fed)
        return cur, cur - prev, (cur - prev)/prev*100
    except: return None

def get_spdr_smart():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    curr = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return curr, curr - prev
        return None
    except: return None

# ==============================================================================
# 4. LOGIC UPDATE
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_success_time'] < 300:
        return
        
    # 1. VIX & GVZ
    res = get_yahoo_strict("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    res = get_yahoo_strict("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 2. SPDR
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    # 3. LẠM PHÁT (BREAKEVEN)
    res10 = get_yahoo_strict("^T10YIE") # 10Y
    if res10: GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
    
    res05 = get_yahoo_strict("^T5YIE") # 5Y (Proxy cho 2Y)
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    
    # 4. FEDWATCH (YIELD)
    res_tnx = get_yahoo_strict("^TNX") 
    if res_tnx: GLOBAL_CACHE['fed10'] = {'p': res_tnx[0], 'c': res_tnx[1], 'pct': res_tnx[2]}
    
    res_fvx = get_yahoo_strict("^FVX") 
    if res_fvx: GLOBAL_CACHE['fed05'] = {'p': res_fvx[0], 'c': res_fvx[1], 'pct': res_fvx[2]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    gold = get_gold_binance()
    if not gold: 
        gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    update_macro_data()
    return gold, GLOBAL_CACHE

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V29 - Config Fixed"

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- A. CẢNH BÁO VÀNG ---
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
        
        # --- B. CẢNH BÁO VĨ MÔ ---
        # VIX
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f} (Tăng {macro['vix']['pct']:.1f}%)")
                last_alert_times['VIX'] = now

        # GVZ
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {macro['gvz']['p']:.2f} (Tăng {macro['gvz']['pct']:.1f}%)")
                last_alert_times['GVZ'] = now

        # Lạm phát 10Y (Check điểm số > 0.25)
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now

        # Lạm phát 2Y/5Y (Check điểm số > 0.20) -- ĐÃ THÊM VÀO
        if abs(macro['inf05']['c']) > CONFIG['INF_05Y_LIMIT']:
            if now - last_alert_times.get('INF05', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT NGẮN HẠN:</b> Đổi {abs(macro['inf05']['c']):.3f} điểm")
                last_alert_times['INF05'] = now

        # FedWatch (% biến động > 15%)
        if abs(macro['fed05']['pct']) > CONFIG['FED_PCT_LIMIT']:
            if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED WATCH BIẾN ĐỘNG:</b> Đổi {abs(macro['fed05']['pct']):.1f}%")
                last_alert_times['FED'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD ---
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        if vn_now.minute in [0, 1, 30, 31]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct):
                if val == 0: return "N/A"
                return f"{val:.2f} ({s(pct)}{pct:.2f}%)"
            
            def fmt_pts(val, chg):
                if val == 0: return "N/A"
                return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" 

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
                f"🇺🇸 <b>Lạm phát (Breakeven):</b>\n"
                f"   • 10Y: {fmt_pts(macro['inf10']['p'], macro['inf10']['c'])}\n"
                f"   • 05Y: {fmt_pts(macro['inf05']['p'], macro['inf05']['c'])}\n"
                f"-------------------------------\n"
                f"🏦 <b>FedWatch (Yield Proxy):</b>\n"
                f"   • 10Y: {fmt(macro['fed10']['p'], macro['fed10']['c'], macro['fed10']['pct'])}\n"
                f"   • 05Y: {fmt(macro['fed05']['p'], macro['fed05']['c'], macro['fed05']['pct'])}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ: {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Err: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
