from flask import Flask
import requests
import pandas as pd
import io
import time
from datetime import datetime
import pytz
import json

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO
    "GOLD_H1_LIMIT": 30.0,
    "RSI_HIGH": 80,
    "RSI_LOW": 20,
    "VIX_LIMIT": 30,
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM TẠO HEADER GIẢ LẬP TRÌNH DUYỆT (CHỐNG CHẶN)
# ==============================================================================
def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/"
    }

# ==============================================================================
# 3. HÀM LẤY DATA TRỰC TIẾP TỪ API (BỎ QUA THƯ VIỆN YFINANCE)
# ==============================================================================
def get_yahoo_direct(symbol):
    """
    Gọi trực tiếp vào API JSON của Yahoo để tránh bị thư viện làm lỗi.
    """
    try:
        # URL API ngầm của Yahoo
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        
        response = requests.get(url, headers=get_headers(), timeout=10)
        data = response.json()
        
        # Phân tích JSON
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = quote['close']
        
        # Lọc bỏ giá trị None/Null
        clean_closes = [c for c in closes if c is not None]
        
        if len(clean_closes) < 2:
            return 0.0, 0.0, 0.0
            
        current = float(clean_closes[-1])
        prev = float(clean_closes[-2])
        
        change = current - prev
        pct = (change / prev * 100) if prev != 0 else 0
        
        return current, change, pct
    except Exception as e:
        print(f"Lỗi lấy {symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_h1_direct():
    """Lấy dữ liệu H1 và RSI trực tiếp"""
    try:
        # Lấy range 5 ngày, interval 60m (1h)
        url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=60m&range=5d"
        response = requests.get(url, headers=get_headers(), timeout=10)
        data = response.json()
        
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        
        closes = quote['close']
        highs = quote['high']
        lows = quote['low']
        
        # Làm sạch dữ liệu
        clean_data = []
        for i in range(len(closes)):
            if closes[i] is not None:
                clean_data.append({
                    'close': closes[i],
                    'high': highs[i],
                    'low': lows[i]
                })
        
        if len(clean_data) < 15: return 0.0, 50.0
        
        # 1. Tính H1 Range (Nến cuối cùng)
        last_candle = clean_data[-1]
        h1_range = last_candle['high'] - last_candle['low']
        
        # 2. Tính RSI (Thủ công)
        prices = pd.Series([x['close'] for x in clean_data])
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        return h1_range, current_rsi
        
    except: return 0.0, 50.0

# ==============================================================================
# 4. HÀM LẤY SPDR (CÀO WEB GỐC)
# ==============================================================================
def get_spdr_real():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers=get_headers(), timeout=15, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    curr = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return curr, curr - prev
        return 0.0, 0.0
    except: return 0.0, 0.0

# ==============================================================================
# 5. TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_data():
    d = {}
    
    # 1. Gold Futures (GC=F)
    p, c, pct = get_yahoo_direct("GC=F")
    d['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # 2. Tech (RSI, H1)
    h1, rsi = get_gold_h1_direct()
    d['h1'] = h1; d['rsi'] = rsi
    
    # 3. Lạm phát (Breakeven)
    # Lấy trực tiếp mã Lạm phát, nếu lỗi (0.0) thì lấy mã Yield
    p10, c10, _ = get_yahoo_direct("^T10YIE")
    p05, c05, _ = get_yahoo_direct("^T5YIE")
    
    if p10 == 0:
        d['be_name'] = "US Yields (Lợi suất - Backup)"
        p10, c10, _ = get_yahoo_direct("^TNX") # 10Y Yield
        p05, c05, _ = get_yahoo_direct("^FVX") # 5Y Yield
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # 4. Risk
    p, _, pct = get_yahoo_direct("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    
    p, _, pct = get_yahoo_direct("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    # 5. SPDR
    v, c = get_spdr_real()
    d['spdr'] = {'v': v, 'c': c}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 6. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V16 - Direct API Mode"

@app.route('/run_check')
def run_check():
    d = get_data()
    alerts = []
    now = time.time()
    
    # --- CẢNH BÁO ---
    if d['rsi'] > CONFIG['RSI_HIGH'] and d['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['rsi']:.0f} + H1 chạy {d['h1']:.1f}$")
            last_alert_times['RSI'] = now
            
    if d['rsi'] < CONFIG['RSI_LOW'] and d['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['rsi']:.0f} + H1 sập {d['h1']:.1f}$")
            last_alert_times['RSI'] = now

    if d['h1'] > CONFIG['GOLD_H1_LIMIT']:
        if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {d['h1']:.1f} giá")
            last_alert_times['H1'] = now

    if d['vix']['p'] > CONFIG['VIX_LIMIT']:
         if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {d['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if abs(d['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
        if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(d['be10']['c']):.3f} điểm")
            last_alert_times['BE'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert"

    # --- BÁO CÁO 30 PHÚT ---
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        # Xử lý chuỗi hiển thị
        spdr_display = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chờ cập nhật"
        spdr_chg_display = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""
        
        be10_val = f"{d['be10']['p']:.2f}%" if d['be10']['p'] > 0 else "0.00%"
        be05_val = f"{d['be05']['p']:.2f}%" if d['be05']['p'] > 0 else "0.00%"

        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {d['gold']['p']:.1f}\n"
            f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {d['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {spdr_display} {spdr_chg_display}\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>{d['be_name']}:</b>\n"
            f"   • 10Y: {be10_val} (Chg: {s(d['be10']['c'])}{d['be10']['c']:.3f})\n"
            f"   • 05Y: {be05_val} (Chg: {s(d['be05']['c'])}{d['be05']['c']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {d['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {d['gvz']['p']:.2f}\n"
        )
        send_tele(msg)
        return "Report"

    return "Ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
