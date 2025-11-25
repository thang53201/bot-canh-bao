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
# 1. CẤU HÌNH & BỘ NHỚ ĐỆM (CACHE)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    "GOLD_H1_LIMIT": 30.0,
    "RSI_HIGH": 80, 
    "RSI_LOW": 20,
    "VIX_LIMIT": 30,
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

# Bộ nhớ lưu trữ dữ liệu Yahoo để dùng lại (Tránh spam)
DATA_CACHE = {
    'last_update': 0,
    'data': None
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY VÀNG BINANCE (TỐI ƯU KẾT NỐI)
# ==============================================================================
def get_gold_binance():
    """
    Lấy giá PAXG/USDT. Tăng timeout lên 15s để tránh bị lỗi mạng 
    khiến bot nhảy sang Yahoo.
    """
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT"
        r = requests.get(url, timeout=15) # Tăng timeout
        data = r.json()
        
        current = float(data['lastPrice'])
        change = float(data['priceChange'])
        pct = float(data['priceChangePercent'])
        
        # Lấy nến để tính RSI
        k_url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20"
        k_r = requests.get(k_url, timeout=15)
        k_data = k_r.json()
        closes = [float(x[4]) for x in k_data]
        
        # Tính RSI
        if len(closes) >= 15:
            prices = pd.Series(closes)
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1])
        else:
            current_rsi = 50.0

        # Tính H1 Range
        last = k_data[-1]
        h1 = float(last[2]) - float(last[3])

        return {
            'p': current, 'c': change, 'pct': pct,
            'h1': h1, 'rsi': current_rsi,
            'src': 'Binance (Ổn định)'
        }
    except Exception as e:
        print(f"Binance Error: {e}")
        return None

# ==============================================================================
# 3. HÀM LẤY YAHOO (CHỈ CHẠY 10 PHÚT 1 LẦN)
# ==============================================================================
def get_yahoo_smart(symbol):
    try:
        # Random User Agent
        uas = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15'
        ]
        headers = {"User-Agent": random.choice(uas)}
        
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        
        if len(closes) < 2: return 0.0, 0.0, 0.0
        
        cur = closes[-1]
        prev = closes[-2]
        return cur, cur - prev, (cur - prev)/prev*100
    except: return 0.0, 0.0, 0.0

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
                    cur = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return cur, cur - prev
        return 0.0, 0.0
    except: return 0.0, 0.0

# ==============================================================================
# 4. TỔNG HỢP DỮ LIỆU (CƠ CHẾ CACHE THÔNG MINH)
# ==============================================================================
def get_data_combined():
    d = {}
    current_time = time.time()
    
    # A. VÀNG (LUÔN LẤY MỚI TỪ BINANCE)
    gold = get_gold_binance()
    if gold:
        d['gold'] = gold
    else:
        # Nếu Binance lỗi, dùng tạm dữ liệu cũ hoặc báo lỗi (Không gọi Yahoo để tránh bị chặn thêm)
        d['gold'] = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Mất kết nối'}

    # B. CÁC CHỈ SỐ KHÁC (LẤY TỪ CACHE HOẶC CẬP NHẬT MỖI 10 PHÚT)
    # Nếu Cache trống HOẶC đã quá 10 phút (600s) -> Mới gọi Yahoo
    if DATA_CACHE['data'] is None or (current_time - DATA_CACHE['last_update'] > 600):
        print("--- Cập nhật dữ liệu Yahoo (10p/lần) ---")
        macro = {}
        
        # VIX & GVZ
        p, _, pct = get_yahoo_smart("^VIX")
        macro['vix'] = {'p': p, 'pct': pct}
        p, _, pct = get_yahoo_smart("^GVZ")
        macro['gvz'] = {'p': p, 'pct': pct}
        
        # Lạm phát
        p10, c10, _ = get_yahoo_smart("^T10YIE")
        p05, c05, _ = get_yahoo_smart("^T5YIE")
        
        if p10 == 0:
            macro['be_name'] = "Fed Proxy (Yields)"
            p10, c10, _ = get_yahoo_smart("^TNX")
            p05, c05, _ = get_yahoo_smart("^FVX")
        else:
            macro['be_name'] = "Breakeven (Lạm phát)"
            
        macro['be10'] = {'p': p10, 'c': c10}
        macro['be05'] = {'p': p05, 'c': c05}
        
        # SPDR
        v, c = get_spdr_smart()
        macro['spdr'] = {'v': v, 'c': c}
        
        # Lưu vào bộ nhớ
        DATA_CACHE['data'] = macro
        DATA_CACHE['last_update'] = current_time
    
    # Trộn dữ liệu Vàng mới + Dữ liệu Vĩ mô (từ Cache)
    d.update(DATA_CACHE['data'])
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V21 - Smart Cache Mode"

@app.route('/run_check')
def run_check():
    # Bọc try-except để không sập server
    try:
        d = get_data_combined()
        alerts = []
        now = time.time()
        
        # CẢNH BÁO (Dùng dữ liệu Vàng mới nhất)
        if d['gold']['rsi'] > CONFIG['RSI_HIGH'] and d['gold']['h1'] > 20:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['gold']['rsi']:.0f} + H1 chạy {d['gold']['h1']:.1f}$")
                last_alert_times['RSI'] = now
                
        if d['gold']['rsi'] < CONFIG['RSI_LOW'] and d['gold']['h1'] > 20:
            if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['gold']['rsi']:.0f} + H1 sập {d['gold']['h1']:.1f}$")
                last_alert_times['RSI'] = now

        if d['gold']['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {d['gold']['h1']:.1f} giá")
                last_alert_times['H1'] = now

        # Cảnh báo Vĩ mô (Dựa trên dữ liệu Cache - chậm hơn chút nhưng an toàn)
        if abs(d['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
            if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(d['be10']['c']):.3f} điểm")
                last_alert_times['BE'] = now

        if d['vix']['p'] > CONFIG['VIX_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX CAO:</b> {d['vix']['p']:.2f}")
                last_alert_times['VIX'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # BÁO CÁO
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        if vn_now.minute in [0, 1, 2, 30, 31, 32]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            # Xử lý hiển thị
            spdr_str = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""
            
            vix_str = f"{d['vix']['p']:.2f}" if d['vix']['p'] > 0 else "N/A"
            be10_str = f"{d['be10']['p']:.2f}%" if d['be10']['p'] > 0 else "N/A"
            be05_str = f"{d['be05']['p']:.2f}%" if d['be05']['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"Nguồn Vàng: {d['gold']['src']}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {d['gold']['p']:.1f}\n"
                f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {d['gold']['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_str} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{d['be_name']}:</b>\n"
                f"   • 10Y: {be10_str} (Chg: {s(d['be10']['c'])}{d['be10']['c']:.3f})\n"
                f"   • 05Y: {be05_str} (Chg: {s(d['be05']['c'])}{d['be05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"📉 <b>VIX:</b> {vix_str} | 🌪 <b>GVZ:</b> {d['gvz']['p']:.2f}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e:
        print(f"Bot Error: {e}")
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
