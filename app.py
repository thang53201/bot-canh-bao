from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime
import pytz
import re

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

# Cache
GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed_prob': {'p': 0, 'c': 0}, # Tỷ lệ FedWatch thực tế
    'spdr': {'v': 0, 'c': 0},
    'last_success_time': 0
}

last_alert_times = {}

# ==============================================================================
# 2. VÀNG BINANCE (CƠ CHẾ RETRY 3 LẦN)
# ==============================================================================
def get_gold_binance():
    url = "https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT"
    k_url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20"
    
    for _ in range(3): # Thử 3 lần nếu lỗi
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            
            kr = requests.get(k_url, timeout=10)
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
                'h1': h1, 'rsi': curr_rsi, 'src': 'Binance'
            }
        except:
            time.sleep(1) # Nghỉ 1s rồi thử lại
            continue
            
    return None # Nếu 3 lần đều chết thì chịu

# ==============================================================================
# 3. YAHOO & FRED & CME (CÁC NGUỒN KHÓ)
# ==============================================================================
def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

# Lấy Yahoo (VIX, GVZ)
def get_yahoo_strict(symbol):
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=get_headers(), timeout=10)
        data = r.json()
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        if len(closes) < 2: return None
        cur = closes[-1]; prev = closes[-2]
        return cur, cur - prev, (cur - prev)/prev*100
    except: return None

# Lấy Lạm phát (Yahoo -> FRED Backup)
def get_breakeven_real(years=10):
    symbol = "^T10YIE" if years == 10 else "^T5YIE"
    # 1. Thử Yahoo
    res = get_yahoo_strict(symbol)
    if res: return res[0], res[1]
    
    # 2. Thử FRED (Nguồn gốc)
    fred_id = "T10YIE" if years == 10 else "T5YIE"
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[fred_id] != '.'] # Lọc ngày nghỉ
            if len(df) >= 2:
                cur = float(df.iloc[-1][fred_id])
                prev = float(df.iloc[-2][fred_id])
                return cur, cur - prev
    except: pass
    return 0.0, 0.0

# Lấy FedWatch (CME Scraping - Thử nghiệm)
# Chúng ta sẽ thử lấy từ một API public khác thường mirror dữ liệu CME
# hoặc dùng Logic Yield nếu không lấy được.
def get_fedwatch_real():
    # Hiện tại không có cách cào CME trực tiếp ổn định trên Render Free.
    # Giải pháp: Tiếp tục dùng Yield nhưng đổi tên hiển thị cho đỡ nhầm lẫn
    # Hoặc thử API của Investing (nhưng hay bị chặn).
    # Tạm thời dùng Yield 2Y (^IRX) làm chỉ báo tốt nhất.
    return get_yahoo_strict("^IRX") # 13 Week Treasury Bill (Rất sát lãi suất Fed)

def get_spdr_smart():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers=get_headers(), timeout=10, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
        return None
    except: return None

# ==============================================================================
# 4. UPDATE LOGIC
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_success_time'] < 300:
        return
        
    # VIX/GVZ
    res = get_yahoo_strict("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    res = get_yahoo_strict("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # SPDR
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    # LẠM PHÁT (Yahoo + FRED)
    p10, c10 = get_breakeven_real(10)
    if p10 > 0: GLOBAL_CACHE['inf10'] = {'p': p10, 'c': c10}
    
    p05, c05 = get_breakeven_real(5)
    if p05 > 0: GLOBAL_CACHE['inf05'] = {'p': p05, 'c': c05}
    
    # FEDWATCH (Dùng Yield 13 Tuần - ^IRX - Sát nhất với Fed Fund Rate)
    res_fed = get_fedwatch_real()
    if res_fed: GLOBAL_CACHE['fed_prob'] = {'p': res_fed[0], 'c': res_fed[1]}
    
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
def home(): return "Bot V30 - Final Fixes"

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO ---
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
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f} (Tăng {macro['vix']['pct']:.1f}%)")
                last_alert_times['VIX'] = now

        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now

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

            # FedWatch Proxy
            if macro['fed_prob']['p'] > 0:
                fed_txt = f"{macro['fed_prob']['p']:.2f}% (Lãi suất ngắn hạn)"
            else: fed_txt = "N/A"

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
                f"🏦 <b>FedWatch (Proxy):</b>\n"
                f"   • US 13-Week Bill: {fed_txt}\n"
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
