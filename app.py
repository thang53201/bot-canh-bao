from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",  # <-- ĐIỀN LẠI TOKEN
    "TELEGRAM_CHAT_ID": "5464507208",               # <-- ĐIỀN LẠI CHAT ID
    
    # NGƯỠNG CẢNH BÁO KHẨN (Emergency)
    "VIX_LIMIT": 30,
    "GVZ_LIMIT": 25,
    "GOLD_H1_LIMIT": 40.0,
    "YIELD_CHANGE_LIMIT": 3.0,   # Yield biến động > 3% (tương đối)
    
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY DỮ LIỆU (REALTIME SPOT & YIELD)
# ==============================================================================
def get_realtime_data(ticker_symbol):
    """Lấy dữ liệu Realtime D1"""
    try:
        # Lấy dữ liệu 5 ngày gần nhất
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        # Current là giá đóng cửa nến gần nhất (hoặc giá hiện tại nếu đang chạy)
        current = float(hist['Close'].iloc[-1])
        # Prev là giá đóng cửa ngày hôm trước
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except Exception:
        return 0.0, 0.0, 0.0

def get_gold_h1_range():
    """Biên độ H1 Vàng Spot"""
    try:
        data = yf.download("XAUUSD=X", period="1d", interval="1h", progress=False)
        if not data.empty:
            try:
                high = float(data['High'].iloc[-1].item())
                low = float(data['Low'].iloc[-1].item())
            except:
                high = float(data['High'].iloc[-1])
                low = float(data['Low'].iloc[-1])
            return high - low
        return 0.0
    except:
        return 0.0

def get_market_data():
    data = {}
    
    # 1. GOLD SPOT (XAUUSD=X) - Chuẩn Exness/Investing
    cur, chg, pct = get_realtime_data("XAUUSD=X")
    data['gold_price'] = cur
    data['gold_change'] = chg
    data['gold_pct'] = pct
    
    # 2. US 10Y YIELD (^TNX) - Thay thế Breakeven bị lỗi
    cur, chg, pct = get_realtime_data("^TNX")
    data['us10y'] = cur
    data['us10y_change'] = chg
    data['us10y_pct'] = pct

    # 3. US 05Y YIELD (^FVX) - Thay thế Breakeven bị lỗi
    cur, chg, pct = get_realtime_data("^FVX")
    data['us05y'] = cur
    data['us05y_change'] = chg
    data['us05y_pct'] = pct
    
    # 4. VIX (^VIX)
    cur, chg, pct = get_realtime_data("^VIX")
    data['vix'] = cur
    data['vix_pct'] = pct
    
    # 5. GVZ (^GVZ)
    cur, chg, pct = get_realtime_data("^GVZ")
    data['gvz'] = cur
    data['gvz_pct'] = pct

    # 6. GOLD H1 Range
    data['gold_h1_range'] = get_gold_h1_range()
    
    return data

def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
        payload = {
            "chat_id": CONFIG['TELEGRAM_CHAT_ID'],
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Lỗi Tele: {e}")

# ==============================================================================
# 3. ROUTING
# ==============================================================================
@app.route('/')
def home():
    return "Bot Realtime Active"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- A. CẢNH BÁO KHẨN CẤP ---
    
    # 1. Vàng H1
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 BIẾN ĐỘNG:</b> {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    # 2. VIX
    if data['vix'] > CONFIG["VIX_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG ĐỎ:</b> {data['vix']:.2f}")
            last_alert_times['VIX'] = current_time

    # Gửi cảnh báo
    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- B. BÁO CÁO 30 PHÚT (STYLE INVESTING) ---
    current_minute = datetime.now().minute
    # Chấp nhận phút 00-02 và 30-32
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""

        # Logic icon: Tăng dùng 🟢, Giảm dùng 🔴
        def icon(val): return "🟢" if val >= 0 else "🔴"

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (Realtime)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>XAU/USD (Spot):</b> {data['gold_price']:.2f}\n"
            f"   {icon(data['gold_change'])} {sign(data['gold_change'])}{data['gold_change']:.2f} ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>US Yields (Lợi suất):</b>\n"
            f"   • 10Y: {data['us10y']:.3f}% ({sign(data['us10y_change'])}{data['us10y_change']:.3f})\n"
            f"   • 05Y: {data['us05y']:.3f}% ({sign(data['us05y_change'])}{data['us05y_change']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>S&P 500 VIX:</b> {data['vix']:.2f}\n"
            f"   {icon(data['vix_pct'])} {sign(data['vix_pct'])}{data['vix_pct']:.2f}%\n"
            f"\n"
            f"🌪 <b>CBOE Gold Vol (GVZ):</b> {data['gvz']:.2f}\n"
            f"   {icon(data['gvz_pct'])} {sign(data['gvz_pct'])}{data['gvz_pct']:.2f}%\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
