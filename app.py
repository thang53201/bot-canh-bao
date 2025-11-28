from flask import Flask
import requests
import pandas as pd
import io
import time
import csv  # Thêm thư viện này để xử lý file SPDR
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
    
    # API KEY TWELVE DATA
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # NGƯỠNG CẢNH BÁO
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0, 
    
    "INF_10Y_LIMIT": 0.25, 
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600,
    "SPDR_CACHE_TIME": 1800 # 30 phút cập nhật SPDR 1 lần
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W'},
    # Cập nhật cấu trúc cache cho SPDR
    'spdr': {'v': 0, 'c': 0, 'alert_msg': '', 'is_emergency': False}, 
    'be_source': 'Chờ...',
    'last_success_time': 0,
    'last_spdr_time': 0, # Time riêng cho SPDR
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
# 2. HÀM LẤY VÀNG (TWELVE DATA + BINANCE TECH)
# ==============================================================================
def get_gold_forex_api():
    try:
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        d = r.json()
        if 'close' in d:
            tech = get_gold_binance_tech()
            return {'p': float(d['close']), 'c': float(d['change']), 'pct': float(d['percent_change']), 'h1': tech['h1'], 'rsi': tech['rsi'], 'src': 'Forex API (Chuẩn)'}
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

# ==============================================================================
# 3. MACRO (YAHOO, FRED, SPDR MỚI)
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

def get_fred_data(sid):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[sid] != '.']
            df[sid] = pd.to_numeric(df[sid])
            if len(df) >= 2:
                return float(df.iloc[-1][sid]), float(df.iloc[-1][sid]) - float(df.iloc[-2][sid])
    except: return None

# --- LOGIC SPDR MỚI (CHECK 5 TẤN HOẶC 3 NGÀY LIÊN TIẾP) ---
def get_spdr_advanced():
    """
    Trả về dict: {tonnes, change_today, alert_msg, is_emergency}
    """
    url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        response = requests.get(url, headers=headers, timeout=15, verify=False)
        if response.status_code != 200: return None
        
        # Parse CSV thủ công để tránh lỗi header
        content = response.content.decode('utf-8')
        lines = [line for line in content.splitlines() if len(line) > 10 and line[0].isdigit()]
        reader = csv.reader(lines)
        rows = list(reader)
        
        if len(rows) < 4: return None

        last_4 = rows[-4:] # Lấy 4 ngày cuối
        
        def extract_tonnes(row):
            for item in row:
                try:
                    val = float(item.replace(',', ''))
                    if 600 < val < 2000: return val
                except: continue
            return 0.0

        t0 = extract_tonnes(last_4[3]) # Nay
        t1 = extract_tonnes(last_4[2]) # Qua
        t2 = extract_tonnes(last_4[1]) # Kia
        t3 = extract_tonnes(last_4[0]) # Kìa

        change_today = t0 - t1
        change_1 = t1 - t2
        change_2 = t2 - t3
        
        alert_msg = ""
        is_emergency = False
        
        # 1. Check > 5 tấn
        if abs(change_today) >= 5.0:
            action = "MUA KHỦNG" if change_today > 0 else "XẢ KHỦNG"
            alert_msg = f"🐋 <b>SPDR {action}:</b> {abs(change_today):.2f} tấn!"
            is_emergency = True
            
        # 2. Check 3 ngày liên tiếp
        elif change_today > 0 and change_1 > 0 and change_2 > 0:
            alert_msg = f"⚠️ <b>SPDR MUA RÒNG:</b> 3 ngày liên tiếp (+{change_today:.2f}t)"
            is_emergency = True
        elif change_today < 0 and change_1 < 0 and change_2 < 0:
            alert_msg = f"⚠️ <b>SPDR XẢ RÒNG:</b> 3 ngày liên tiếp ({change_today:.2f}t)"
            is_emergency = True
            
        return {
            'v': t0, 
            'c': change_today, 
            'alert_msg': alert_msg, 
            'is_emergency': is_emergency
        }
        
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # Update SPDR mỗi 30 phút (1800s)
    if current_time - GLOBAL_CACHE.get('last_spdr_time', 0) > CONFIG['SPDR_CACHE_TIME']:
        spdr_res = get_spdr_advanced()
        if spdr_res:
            GLOBAL_CACHE['spdr'] = spdr_res
            GLOBAL_CACHE['last_spdr_time'] = current_time

    # Các chỉ số khác cập nhật mỗi 5 phút (300s)
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    # VIX & GVZ
    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # LẠM PHÁT
    inf10 = get_fred_data("T10YIE")
    if inf10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
        GLOBAL_CACHE['inf10'] = {'p': inf10[0], 'c': inf10[1]}
    else:
        res10 = get_yahoo_data("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (Yahoo)"
            GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
        else: GLOBAL_CACHE['be_source'] = "Lạm phát (Chờ...)"

    res05 = get_yahoo_data("^T5YIE")
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    else:
        fred05 = get_fred_data("T5YIE")
        if fred05: GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}

    # FEDWATCH
    res_fed = get_yahoo_data("^IRX")
    if res_fed: GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W'}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    curr_min = datetime.utcnow().minute
    gold = get_gold_forex_api() if curr_min in [0,1,30,31] else get_gold_binance_full()
    if not gold: gold = get_gold_binance_full()
    if not gold: 
        if GLOBAL_CACHE['gold']['p'] > 0: gold = GLOBAL_CACHE['gold']
        else: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'}
    try: update_macro_data()
    except: pass
    GLOBAL_CACHE['gold'] = gold
    return gold, GLOBAL_CACHE

# ==============================================================================
# 4. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V81 - SPDR Advanced"

@app.route('/test')
def run_test():
    gold, macro = get_data_final()
    spdr_status = "Có dữ liệu" if macro['spdr']['v'] > 0 else "Chưa có"
    send_tele(f"🔔 TEST OK. Gold: {gold['p']}. SPDR: {spdr_status}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO SPDR (MỚI THÊM) ---
        spdr = macro['spdr']
        if spdr['is_emergency']:
            # Dùng cooldown riêng cho SPDR để tránh spam nếu nó giữ nguyên trạng thái
            if now - last_alert_times.get('SPDR', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(spdr['alert_msg'])
                last_alert_times['SPDR'] = now

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
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now
        
        # CẢNH BÁO VIX
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        
        # CẢNH BÁO GVZ
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
            
            # Format hiển thị SPDR
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            def fmt_pts(val, chg): return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" if val else "N/A"

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
    except Exception as e: 
        return f"Err: {e}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

