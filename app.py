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
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO (BẢO VỆ DCA)
    "GOLD_H1_LIMIT": 30.0,       # Nến H1 > 30 giá
    "RSI_HIGH": 80,              # RSI Quá mua
    "RSI_LOW": 20,               # RSI Quá bán
    
    # NGƯỠNG VĨ MÔ
    "VIX_LIMIT": 30,
    "BE_CHANGE_LIMIT": 0.15,
    
    # CHỐNG SPAM TIN NHẮN
    "ALERT_COOLDOWN": 3600       # Báo xong im lặng 60 phút
}

# Bộ nhớ đệm cho Yahoo (Để tránh bị chặn khi chạy 1 phút/lần)
GLOBAL_CACHE = {
    'vix': {'p': 0, 'pct': 0},
    'gvz': {'p': 0, 'pct': 0},
    'be10': {'p': 0, 'c': 0},
    'be05': {'p': 0, 'c': 0},
    'spdr': {'v': 0, 'c': 0},
    'be_name': "Đang tải...",
    'last_success_time': 0
}

last_alert_times = {}

# ==============================================================================
# 2. VÀNG BINANCE (CHẠY TỐC ĐỘ CAO - 1 PHÚT/LẦN)
# ==============================================================================
def get_gold_binance():
    try:
        # Tăng timeout lên 20s để đảm bảo bắt được dữ liệu kể cả mạng lag
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=20)
        data = r.json()
        
        # Lấy nến để tính RSI & H1 Range
        kr = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=20)
        k_data = kr.json()
        closes = [float(x[4]) for x in k_data]
        
        # Tính RSI 14
        if len(closes) >= 15:
            prices = pd.Series(closes)
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            curr_rsi = float(rsi.iloc[-1])
        else: curr_rsi = 50.0

        # Tính H1 Range
        last = k_data[-1]
        h1 = float(last[2]) - float(last[3])

        return {
            'p': float(data['lastPrice']), 
            'c': float(data['priceChange']), 
            'pct': float(data['priceChangePercent']),
            'h1': h1, 'rsi': curr_rsi, 'src': 'Binance (1 phút)'
        }
    except: return None

# ==============================================================================
# 3. YAHOO & SPDR (CƠ CHẾ TIẾT KIỆM - 5 PHÚT/LẦN)
# ==============================================================================
def get_yahoo_smart(symbol):
    try:
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        ]
        headers = {"User-Agent": random.choice(uas)}
        # Lấy JSON trực tiếp để nhẹ và nhanh
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
# 4. LOGIC ĐIỀU PHỐI (QUAN TRỌNG)
# ==============================================================================
def get_data_final():
    # 1. Luôn lấy Vàng (Mỗi phút đều lấy) -> Bảo vệ DCA
    gold = get_gold_binance()
    if not gold: 
        gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}
    
    # 2. Cập nhật Vĩ mô (Chỉ lấy khi cache cũ quá 5 phút)
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 300 giây = 5 phút. Dù cron chạy 1 phút, Yahoo vẫn chỉ bị gọi 5 phút/lần.
    if current_time - GLOBAL_CACHE['last_success_time'] >= 300:
        # --- BẮT ĐẦU CẬP NHẬT YAHOO ---
        # VIX & GVZ
        res = get_yahoo_smart("^VIX")
        if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'pct': res[2]}
        
        res = get_yahoo_smart("^GVZ")
        if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'pct': res[2]}
        
        # SPDR
        res = get_spdr_smart()
        if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
        
        # Lạm phát (Breakeven -> Yield)
        res10 = get_yahoo_smart("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_name'] = "Breakeven (Lạm phát)"
            GLOBAL_CACHE['be10'] = {'p': res10[0], 'c': res10[1]}
            res05 = get_yahoo_smart("^T5YIE")
            if res05: GLOBAL_CACHE['be05'] = {'p': res05[0], 'c': res05[1]}
        else:
            res10y = get_yahoo_smart("^TNX")
            if res10y:
                GLOBAL_CACHE['be_name'] = "Fed Proxy (Yields)"
                GLOBAL_CACHE['be10'] = {'p': res10y[0], 'c': res10y[1]}
                res05y = get_yahoo_smart("^FVX")
                if res05y: GLOBAL_CACHE['be05'] = {'p': res05y[0], 'c': res05y[1]}
                
        GLOBAL_CACHE['last_success_time'] = current_time
    
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
def home(): return "Bot V24 - 1 Min Speed"

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- CHECK MỖI PHÚT (VÀNG & RSI) ---
        # Cứu tinh cho DCA
        if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > 20:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {gold['rsi']:.0f} + H1 chạy {gold['h1']:.1f}$")
                last_alert_times['RSI'] = now
                
        if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > 20:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {gold['rsi']:.0f} + H1 sập {gold['h1']:.1f}$")
                last_alert_times['RSI'] = now

        if gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {gold['h1']:.1f} giá")
                last_alert_times['H1'] = now

        # Cảnh báo Vĩ mô (Dựa trên dữ liệu Cache 5 phút)
        if macro['vix']['p'] > CONFIG['VIX_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX CAO:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now

        if abs(macro['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
            if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(macro['be10']['c']):.3f} điểm")
                last_alert_times['BE'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- BÁO CÁO 30 PHÚT ---
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        # Chỉ gửi vào đúng giây phút 00 và 30
        if vn_now.minute in [0, 1, 30, 31]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ..."
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            vix_txt = f"{macro['vix']['p']:.2f}" if macro['vix']['p'] > 0 else "N/A"
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
                f"🇺🇸 <b>{macro['be_name']}:</b>\n"
                f"   • 10Y: {be10_txt} (Chg: {s(macro['be10']['c'])}{macro['be10']['c']:.3f})\n"
                f"   • 05Y: {be05_txt} (Chg: {s(macro['be05']['c'])}{macro['be05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"📉 <b>VIX:</b> {vix_txt} | 🌪 <b>GVZ:</b> {macro['gvz']['p']:.2f}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Err: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
