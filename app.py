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
# 1. CẤU HÌNH (NỚI LỎNG CHO BOT GỒNG 150 GIÁ)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # --- NGƯỠNG CẢNH BÁO MỚI ---
    "GOLD_H1_LIMIT": 40.0,       # Nâng lên 40 giá (Bot chịu được 100 giá thì 40 mới đáng báo)
    
    # RSI Cực đoan hơn: Chỉ báo khi quá mua/bán rất nặng
    "RSI_HIGH": 82,              
    "RSI_LOW": 18,
    # Điều kiện kèm theo: Giá phải chạy thêm bao nhiêu thì mới báo?
    "RSI_PRICE_MOVE": 30.0,      # RSI > 82 VÀ Giá chạy > 30$ thì mới báo
    
    "VIX_LIMIT": 33,             # Nâng lên 33
    "BE_CHANGE_LIMIT": 0.20,     # Nâng lên 0.20 điểm
    
    "ALERT_COOLDOWN": 3600
}

# Cache Vĩ mô (Lưu 5 phút)
GLOBAL_CACHE = {
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'be10': {'p': 0, 'c': 0},
    'be05': {'p': 0, 'c': 0},
    'spdr': {'v': 0, 'c': 0},
    'last_success_time': 0
}

last_alert_times = {}

# ==============================================================================
# 2. VÀNG BINANCE (1 PHÚT/LẦN)
# ==============================================================================
def get_gold_binance():
    try:
        # Giá Realtime
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=15)
        data = r.json()
        
        # Nến H1 để tính RSI & Range
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

        # H1 Range (High - Low của cây nến hiện tại)
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
def get_yahoo_smart(symbol):
    try:
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15'
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
# 4. LOGIC UPDATE (ĐIỀU PHỐI THỜI GIAN)
# ==============================================================================
def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # Chỉ gọi Yahoo mỗi 5 phút (300s)
    if current_time - GLOBAL_CACHE['last_success_time'] < 300:
        return
        
    # 1. VIX & GVZ
    res = get_yahoo_smart("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    res = get_yahoo_smart("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    # 2. SPDR
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    # 3. LẠM PHÁT
    res10 = get_yahoo_smart("^T10YIE")
    if res10: GLOBAL_CACHE['be10'] = {'p': res10[0], 'c': res10[1]}
    
    res05 = get_yahoo_smart("^T5YIE")
    if res05: GLOBAL_CACHE['be05'] = {'p': res05[0], 'c': res05[1]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    # Vàng: Luôn lấy mới
    gold = get_gold_binance()
    if not gold: 
        gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    
    # Macro: Lấy từ cache (cập nhật 5p/lần)
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
def home(): return "Bot V26 - High Risk Tolerance"

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO (KHẮT KHE HƠN) ---
        
        # 1. Siêu Trend Tăng: RSI > 82 VÀ H1 > 30 giá (Đã quá mua mà vẫn chạy điên cuồng)
        if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG (Nguy hiểm):</b> RSI {gold['rsi']:.0f} + H1 chạy {gold['h1']:.1f}$")
                last_alert_times['RSI'] = now
        
        # 2. Siêu Trend Giảm
        if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM (Nguy hiểm):</b> RSI {gold['rsi']:.0f} + H1 sập {gold['h1']:.1f}$")
                last_alert_times['RSI'] = now

        # 3. Vàng H1 Sốc (>40 giá)
        if gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG SỐC CỰC MẠNH:</b> H1 {gold['h1']:.1f} giá")
                last_alert_times['H1'] = now
        
        # 4. VIX (>33)
        if macro['vix']['p'] > CONFIG['VIX_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX HOẢNG LOẠN:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now

        # 5. Lạm phát (>0.20 điểm)
        if abs(macro['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
            if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>VĨ MÔ ĐẢO CHIỀU:</b> Đổi {abs(macro['be10']['c']):.3f} điểm")
                last_alert_times['BE'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO NGUY HIỂM</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD 30 PHÚT ---
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        if vn_now.minute in [0, 1, 30, 31]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            vix_txt = f"{macro['vix']['p']:.2f} ({s(macro['vix']['pct'])}{macro['vix']['pct']:.2f}%)" if macro['vix']['p'] > 0 else "N/A"
            be10_txt = f"{macro['be10']['p']:.2f}%" if macro['be10']['p'] > 0 else "N/A"
            be05_txt = f"{macro['be05']['p']:.2f}%" if macro['be05']['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"Nguồn Vàng: {gold['src']}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {gold['p']:.1f}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>Lạm phát (Breakeven):</b>\n"
                f"   • 10Y: {be10_txt} (Chg: {s(macro['be10']['c'])}{macro['be10']['c']:.3f})\n"
                f"   • 05Y: {be05_txt} (Chg: {s(macro['be05']['c'])}{macro['be05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {vix_txt}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Err: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
