from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime
import pytz
import json

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # --- NGƯỠNG CẢNH BÁO ---
    "GOLD_H1_LIMIT": 40.0,       # Vàng H1
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # Vĩ mô
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "INF_10Y_LIMIT": 0.25,       # Breakeven
    
    # FEDWATCH: Báo nếu % thay đổi quá 15%
    "FED_CHANGE_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600
}

# Cache
GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'spdr': {'v': 0, 'c': 0},
    
    # Cache riêng cho FedWatch
    'fed': {
        'rate_label': 'N/A', # Ví dụ: "350-375"
        'prob': 0.0,         # Ví dụ: 82.7
        'change': 0.0        # Thay đổi so với lần trước
    },
    
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
            'h1': h1, 'rsi': curr_rsi, 'src': 'Binance'
        }
    except: return None

# ==============================================================================
# 3. FEDWATCH REAL (CME API) - CÁI BẠN CẦN
# ==============================================================================
def get_cme_fedwatch():
    """
    Lấy dữ liệu trực tiếp từ API ẩn của CME Group.
    Trả về: (Khoảng lãi suất dự đoán cao nhất, % Xác suất)
    Ví dụ: ("350-375", 82.7)
    """
    try:
        # API chính chủ CME (Thường trả về JSON cho biểu đồ)
        url = "https://www.cmegroup.com/CmeWS/mvc/XS/json/FedWatch/ALL"
        
        # Header giả lập cực mạnh để qua mặt tường lửa
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
            "Origin": "https://www.cmegroup.com"
        }
        
        r = requests.get(url, headers=headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            # Lấy cuộc họp sắp tới nhất (Phần tử đầu tiên)
            next_meeting = data[0]
            prob_list = next_meeting['problist']
            
            # Tìm kịch bản có xác suất cao nhất
            best_scenario = max(prob_list, key=lambda x: float(x['probability']))
            
            label = f"{best_scenario['min']}-{best_scenario['max']}" # Ví dụ: 350-375
            prob = float(best_scenario['probability']) # Ví dụ: 82.7
            
            return label, prob
            
        return None, 0.0
    except Exception as e:
        print(f"CME Error: {e}")
        return None, 0.0

# ==============================================================================
# 4. YAHOO & SPDR
# ==============================================================================
def get_yahoo_strict(symbol):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()['chart']['result'][0]['indicators']['quote'][0]['close']
        closes = [c for c in data if c is not None]
        if len(closes) < 2: return None
        return closes[-1], closes[-1] - closes[-2], (closes[-1] - closes[-2])/closes[-2]*100
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
                    return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
        return None
    except: return None

# ==============================================================================
# 5. LOGIC UPDATE (CÓ TÍNH TOÁN THAY ĐỔI FED)
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 5 phút cập nhật 1 lần
    if current_time - GLOBAL_CACHE['last_success_time'] < 300:
        return
        
    # 1. VIX/GVZ/SPDR/Lạm phát (Như cũ)
    res = get_yahoo_strict("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_strict("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    res10 = get_yahoo_strict("^T10YIE")
    if res10: GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}
    res05 = get_yahoo_strict("^T5YIE")
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    
    # 2. FEDWATCH (MỚI)
    label, prob = get_cme_fedwatch()
    if label:
        # Tính thay đổi so với lần trước
        old_prob = GLOBAL_CACHE['fed']['prob']
        change = prob - old_prob if old_prob > 0 else 0.0
        
        GLOBAL_CACHE['fed'] = {
            'rate_label': label,
            'prob': prob,
            'change': change
        }
    
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
# 6. ROUTING & CẢNH BÁO
# ==============================================================================
@app.route('/')
def home(): return "Bot V31 - CME FedWatch"

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- A. CẢNH BÁO VÀNG (1 PHÚT) ---
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
        
        # --- B. CẢNH BÁO VĨ MÔ (5 PHÚT) ---
        # FedWatch: Nếu % thay đổi > 15%
        if abs(macro['fed']['change']) > CONFIG['FED_CHANGE_LIMIT']:
             if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED QUAY XE:</b> Cược {macro['fed']['rate_label']} đổi {macro['fed']['change']:.1f}%")
                last_alert_times['FED'] = now

        # VIX/GVZ/Lạm phát (Như cũ)
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD 30 PHÚT ---
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        if vn_now.minute in [0, 1, 30, 31]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_str = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "N/A"
            be10_str = f"{macro['inf10']['p']:.2f}%" if macro['inf10']['p'] > 0 else "N/A"
            be05_str = f"{macro['inf05']['p']:.2f}%" if macro['inf05']['p'] > 0 else "N/A"
            
            # Hiển thị FedWatch
            if macro['fed']['prob'] > 0:
                fed_str = f"{macro['fed']['rate_label']}: <b>{macro['fed']['prob']}%</b> ({s(macro['fed']['change'])}{macro['fed']['change']:.1f}%)"
            else:
                fed_str = "Đang tải..."

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {gold['p']:.1f}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🏦 <b>CME FedWatch (Dự báo):</b>\n"
                f"   • {fed_str}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>Lạm phát (Breakeven):</b>\n"
                f"   • 10Y: {be10_str} (Chg: {s(macro['inf10']['c'])}{macro['inf10']['c']:.3f})\n"
                f"   • 05Y: {be05_str} (Chg: {s(macro['inf05']['c'])}{macro['inf05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR:</b> {spdr_str}\n"
                f"📉 <b>VIX:</b> {macro['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {macro['gvz']['p']:.2f}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Err: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
