from flask import Flask
import requests
import pandas as pd
import io
import time
import csv
import random
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (GIỮ NGUYÊN CỦA BẠN)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # 1. VÀNG
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # 2. RỦI RO & VĨ MÔ
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0, 
    "INF_10Y_LIMIT": 0.25, 
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600,
    "SPDR_CACHE_TIME": 1800
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W'},
    'spdr': {'v': 0, 'c': 0, 'd': '', 'alert_msg': '', 'is_emergency': False}, 
    'be_source': 'Chờ...',
    'last_success_time': 0,
    'last_spdr_time': 0,
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
# 2. HÀM LẤY VÀNG (GIỮ NGUYÊN CODE BẠN - TWELVE DATA)
# ==============================================================================
def calculate_rsi(prices, periods=14):
    if len(prices) < periods + 1: return 50
    delta = pd.Series(prices).diff()
    gain = (delta.where(delta > 0, 0)).rolling(periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def get_gold_api_full():
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1h&outputsize=20&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=15)
        data = r.json()
        
        if 'values' in data:
            candles = data['values']
            current = candles[0]
            price = float(current['close'])
            open_price = float(current['open'])
            change = price - open_price
            percent = (change / open_price) * 100
            closes_history = [float(c['close']) for c in candles][::-1]
            rsi = calculate_rsi(closes_history)
            h1_move = float(current['high']) - float(current['low'])

            return {
                'p': price, 'c': change, 'pct': percent, 'h1': h1_move, 'rsi': rsi, 'src': 'API TwelveData'
            }
    except: pass
    
    if GLOBAL_CACHE['gold']['p'] > 0:
        old_data = GLOBAL_CACHE['gold'].copy()
        old_data['src'] = 'Old Cache (Lỗi API)'
        return old_data
        
    return {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}

# ==============================================================================
# 3. HÀM YAHOO ĐÃ FIX (KHÔNG DÙNG THƯ VIỆN NỮA)
# ==============================================================================
def get_yahoo_data(symbol):
    """
    SỬA LỖI: Dùng requests trực tiếp thay vì yfinance.
    Tránh lỗi 429 và Database Locked trên Render.
    """
    try:
        # Header giả lập để Yahoo không chặn
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15'
        ]
        headers = {"User-Agent": random.choice(uas)}
        
        # Gọi API JSON trực tiếp
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        
        # Bóc tách dữ liệu
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        
        if len(closes) >= 2:
            cur = closes[-1]
            prev = closes[-2]
            return cur, cur - prev, (cur - prev)/prev*100
            
    except: return None
    return None

# ==============================================================================
# 4. FRED & SPDR (GIỮ NGUYÊN CODE BẠN)
# ==============================================================================
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

def get_spdr_advanced():
    url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=15, verify=False)
        if response.status_code != 200: return None
        
        content = response.content.decode('utf-8')
        lines = [line for line in content.splitlines() if len(line) > 10 and line[0].isdigit()]
        reader = csv.reader(lines)
        rows = list(reader)
        if not rows: return None

        valid_values = [] 
        for row in reversed(rows):
            date_str = row[0]
            try:
                val = float(row[10].replace(',', '')) # Cột Tonnes thường ở index 10
                valid_values.append({'d': date_str, 'v': val})
            except: 
                # Fallback tìm cột nếu index lệch
                for item in row:
                    try:
                        v = float(item.replace(',', ''))
                        if 600 < v < 2000: 
                            valid_values.append({'d': date_str, 'v': v})
                            break
                    except: continue
            if len(valid_values) >= 2: break
        
        if len(valid_values) < 2: return None

        t0 = valid_values[0]['v']; d0 = valid_values[0]['d']
        t1 = valid_values[1]['v']
        change_today = t0 - t1
        
        alert_msg = ""
        is_emergency = False
        if abs(change_today) >= 5.0:
            action = "MUA KHỦNG" if change_today > 0 else "XẢ KHỦNG"
            alert_msg = f"🐋 <b>SPDR ({d0}) {action}:</b> {abs(change_today):.2f} tấn!"
            is_emergency = True
            
        return {'v': t0, 'c': change_today, 'd': d0, 'alert_msg': alert_msg, 'is_emergency': is_emergency}
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # SPDR Check
    if current_time - GLOBAL_CACHE.get('last_spdr_time', 0) > CONFIG['SPDR_CACHE_TIME']:
        spdr_res = get_spdr_advanced()
        if spdr_res:
            GLOBAL_CACHE['spdr'] = spdr_res
            GLOBAL_CACHE['last_spdr_time'] = current_time

    # Macro Check
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    inf10 = get_fred_data("T10YIE")
    if inf10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
        GLOBAL_CACHE['inf10'] = {'p': inf10[0], 'c': inf10[1]}
    else:
        res10 = get_yahoo_data("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (Yahoo)"
            GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}

    res05 = get_yahoo_data("^T5YIE") # Fallback nếu FRED lỗi
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    else:
        fred05 = get_fred_data("T5YIE")
        if fred05: GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}

    res_fed = get_yahoo_data("^IRX")
    if res_fed: GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W'}
    
    GLOBAL_CACHE['last_success_time'] = current_time

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V86 - Fixed Yahoo Source"

@app.route('/test')
def run_test():
    gold = get_gold_api_full()
    GLOBAL_CACHE['gold'] = gold
    update_macro_data()
    macro = GLOBAL_CACHE
    d_str = macro['spdr'].get('d', 'N/A')
    send_tele(f"🔔 TEST OK.\nGold: {gold['p']} ({gold['src']})\nSPDR: {macro['spdr']['v']}t")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold = get_gold_api_full()
        GLOBAL_CACHE['gold'] = gold
        
        update_macro_data()
        macro = GLOBAL_CACHE
        
        alerts = []
        now = time.time()
        
        # ALERT SPDR
        if macro['spdr'].get('is_emergency'):
            if now - last_alert_times.get('SPDR', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(macro['spdr']['alert_msg'])
                last_alert_times['SPDR'] = now

        # ALERT GOLD
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
        
        # ALERT MACRO
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
             if now - last_alert_times.get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {macro['inf10']['c']:.3f} điểm")
                last_alert_times['INF'] = now
        if abs(macro['fed']['pct']) > CONFIG['FED_PCT_LIMIT']:
             if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED BIẾN ĐỘNG:</b> Đổi {macro['fed']['pct']:.1f}%")
                last_alert_times['FED'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # DASHBOARD
        vn_now = get_vn_time()
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1500): 
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            d_str = f"[{macro['spdr'].get('d','')}]" if macro['spdr'].get('d') else ""
            spdr_txt = f"{d_str} {macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật..."
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
                f"🐋 <b>SPDR Gold:</b>\n"
                f"   • {spdr_txt} {spdr_chg}\n"
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
