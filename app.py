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
    "INF_10Y_LIMIT": 0.25, 
    "FED_CHANGE_LIMIT": 15.0, # Cảnh báo nếu % thay đổi quá 15%
    "ALERT_COOLDOWN": 3600
}

GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    # Cache FedWatch chuẩn CME
    'fed': {
        'label': 'Đang tải...', # Ví dụ: "350-375"
        'prob': 0.0,            # Ví dụ: 82.7
        'change': 0.0           # Biến động so với lần trước
    },
    'spdr': {'v': 0, 'c': 0},
    'be_source': 'Chờ...',
    'last_success_time': 0,
    'last_dashboard_time': 0
}

last_alert_times = {}

def get_vn_time():
    return datetime.utcnow() + timedelta(hours=7)

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ==============================================================================
# 2. HÀM LẤY CME FEDWATCH (QUAN TRỌNG)
# ==============================================================================
def get_cme_real():
    """
    Chọc trực tiếp vào API JSON của CME Group để lấy % chính xác.
    """
    try:
        url = "https://www.cmegroup.com/CmeWS/mvc/XS/json/FedWatch/ALL"
        
        # Header bắt buộc để không bị chặn 403
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
            "Origin": "https://www.cmegroup.com",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }
        
        r = requests.get(url, headers=headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            # Lấy cuộc họp sắp tới (Meeting đầu tiên)
            meeting = data[0]
            prob_list = meeting['problist']
            
            # Tìm kịch bản có xác suất cao nhất (Cột cao nhất trong biểu đồ)
            best = max(prob_list, key=lambda x: float(x['probability']))
            
            prob = float(best['probability']) # Ví dụ 82.7
            label = f"{best['min']}-{best['max']}" # Ví dụ 350-375
            
            return prob, label
            
    except Exception as e:
        print(f"CME Error: {e}")
    return None, None

# ==============================================================================
# 3. CÁC HÀM KHÁC (VÀNG, YAHOO, FRED, SPDR)
# ==============================================================================
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
        return {'p': float(d['lastPrice']), 'c': float(d['priceChange']), 'pct': float(d['priceChangePercent']), 'h1': h1, 'rsi': curr_rsi}
    except: return None

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
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        df = pd.read_csv(io.StringIO(r.text))
        df = df[df[sid] != '.']
        df[sid] = pd.to_numeric(df[sid])
        if len(df) >= 2: return float(df.iloc[-1][sid]), float(df.iloc[-1][sid]) - float(df.iloc[-2][sid])
    except: return None

def get_spdr():
    try:
        r = requests.get("https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv", headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        df = pd.read_csv(io.StringIO(r.text), skiprows=6)
        c = [x for x in df.columns if "Tonnes" in str(x)]
        if c:
            df = df.dropna(subset=[c[0]])
            return float(df.iloc[-1][c[0]]), float(df.iloc[-1][c[0]]) - float(df.iloc[-2][c[0]])
    except: return None

# ==============================================================================
# 4. UPDATE LOGIC
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return # 5 phút

    try:
        # 1. FedWatch (CME Real)
        prob, label = get_cme_real()
        if prob:
            # Tính thay đổi so với lần trước lưu trong cache
            old_prob = GLOBAL_CACHE['fed']['prob']
            change = prob - old_prob if old_prob > 0 else 0.0
            
            GLOBAL_CACHE['fed'] = {'prob': prob, 'label': label, 'change': change}
        
        # 2. Yahoo (VIX/GVZ)
        res = get_yahoo_data("^VIX")
        if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
        res = get_yahoo_data("^GVZ")
        if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
        
        # 3. SPDR
        res = get_spdr()
        if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
        
        # 4. Lạm phát (Yahoo -> Fred)
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
                GLOBAL_CACHE['be_source'] = "Lạm phát (Chờ...)" # Không lấy Yield nữa

        res05 = get_yahoo_data("^T5YIE")
        if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
        else:
            fred05 = get_fred_data("T5YIE")
            if fred05: GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}

        GLOBAL_CACHE['last_success_time'] = current_time
    except: pass

def get_data_final():
    gold = get_gold_binance()
    if not gold: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50}
    try: update_macro_data()
    except: pass
    return gold, GLOBAL_CACHE

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V45 - CME Sniper"

@app.route('/test')
def test():
    gold, _ = get_data_final()
    send_tele(f"🔔 TEST OK. Gold: {gold['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # CẢNH BÁO VÀNG
        if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {gold['rsi']:.0f} + H1 {gold['h1']:.1f}")
                last_alert_times['RSI'] = now
        if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {gold['rsi']:.0f} + H1 {gold['h1']:.1f}")
                last_alert_times['RSI'] = now
        if gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {gold['h1']:.1f} giá")
                last_alert_times['H1'] = now
        
        # CẢNH BÁO VĨ MÔ
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG:</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF'] = now
        
        # CẢNH BÁO CME FEDWATCH (MỚI)
        if abs(macro['fed']['change']) > CONFIG['FED_CHANGE_LIMIT']:
            if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED ĐỔI KÈO ({macro['fed']['label']}):</b> Đổi {macro['fed']['change']:.1f}%")
                last_alert_times['FED'] = now

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
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            
            be10_txt = f"{macro['inf10']['p']:.3f}%" if macro['inf10']['p'] > 0 else "N/A"
            be05_txt = f"{macro['inf05']['p']:.3f}%" if macro['inf05']['p'] > 0 else "N/A"
            
            # Hiển thị FedWatch CME
            if macro['fed']['prob'] > 0:
                fed_txt = f"{macro['fed']['label']}: <b>{macro['fed']['prob']}%</b>"
            else:
                fed_txt = "Đang tải CME..."

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {gold['p']:.1f}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{macro['be_source']}:</b>\n"
                f"   • 10Y: {be10_txt} (Chg: {s(macro['inf10']['c'])}{macro['inf10']['c']:.3f})\n"
                f"   • 05Y: {be05_txt} (Chg: {s(macro['inf05']['c'])}{macro['inf05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"🏦 <b>CME FedWatch (Dự báo):</b>\n"
                f"   • {fed_txt}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {macro['vix']['p']:.2f} ({s(macro['vix']['pct'])}{macro['vix']['pct']:.2f}%)\n"
                f"   • GVZ: {macro['gvz']['p']:.2f} ({s(macro['gvz']['pct'])}{macro['gvz']['pct']:.2f}%)\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
