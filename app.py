from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd

app = Flask(__name__)

# ==============================================================================
# CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAGxxxxxxxxxxxx",  # <-- ĐIỀN TOKEN
    "TELEGRAM_CHAT_ID": "546450726x",               # <-- ĐIỀN CHAT ID
    
    # --- NGƯỠNG KÍCH HOẠT CẢNH BÁO KHẨN CẤP ---
    "VIX_LIMIT": 30,          # VIX > 30 là hoảng loạn
    "GVZ_LIMIT": 25,          # GVZ > 25 là bão to
    "GOLD_H1_LIMIT": 40.0,    # Nến H1 > 40 giá là sốc
    "BE_CHANGE_LIMIT": 0.25,  # Breakeven thay đổi > 0.25 điểm là đảo chiều lạm phát
    
    "ALERT_COOLDOWN": 3600    # Báo xong im 60 phút
}

last_alert_times = {}

# ==============================================================================
# HÀM LẤY DATA (D1 CHO DASHBOARD + H1 CHO CẢNH BÁO)
# ==============================================================================
def get_d1_data(ticker_symbol):
    """Lấy dữ liệu D1 (Ngày) để tính toán điểm số thay đổi"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        
        if len(hist) < 2:
            return 0, 0, 0
            
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev # Thay đổi tuyệt đối (Điểm hoặc $)
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except:
        return 0, 0, 0

def get_gold_h1_range():
    """Lấy biên độ nến H1 Gold Future để cảnh báo sốc"""
    try:
        data = yf.download("GC=F", period="1d", interval="1h", progress=False)
        if not data.empty:
            high = float(data['High'].iloc[-1].item()) if isinstance(data['High'].iloc[-1], pd.Series) else float(data['High'].iloc[-1])
            low = float(data['Low'].iloc[-1].item()) if isinstance(data['Low'].iloc[-1], pd.Series) else float(data['Low'].iloc[-1])
            return high - low
        return 0.0
    except:
        return 0.0

def get_market_data():
    data = {}
    
    # 1. GOLD FUTURE (D1)
    cur, chg, pct = get_d1_data("GC=F")
    data['gold_price'] = cur
    data['gold_change'] = chg
    data['gold_pct'] = pct
    
    # 2. VIX & GVZ (D1)
    cur, chg, pct = get_d1_data("^VIX")
    data['vix'] = cur
    data['vix_pct'] = pct
    
    cur, chg, pct = get_d1_data("^GVZ")
    data['gvz'] = cur
    data['gvz_pct'] = pct

    # 3. US BREAKEVEN RATES (D1 - Lạm phát kì vọng)
    # 10 Year (^T10YIE)
    cur, chg, pct = get_d1_data("^T10YIE")
    data['be10_val'] = cur
    data['be10_chg'] = chg # Điểm thay đổi

    # 5 Year (^T5YIE) - Thay cho 2Y
    cur, chg, pct = get_d1_data("^T5YIE")
    data['be05_val'] = cur
    data['be05_chg'] = chg

    # 4. GOLD H1 RANGE (Cho cảnh báo sốc)
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
# ROUTING & LOGIC CHÍNH
# ==============================================================================
@app.route('/')
def home():
    return "Bot Monitoring Active"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- PHẦN 1: KIỂM TRA 5 CẢNH BÁO KHẨN CẤP ---
    
    # 1. Check VIX
    if data['vix'] > CONFIG["VIX_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG ĐỎ:</b> {data['vix']:.2f}")
            last_alert_times['VIX'] = current_time

    # 2. Check GVZ
    if data['gvz'] > CONFIG["GVZ_LIMIT"]:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f} (Biên độ cực đại)")
            last_alert_times['GVZ'] = current_time

    # 3. Check Breakeven 10Y (Lạm phát)
    if abs(data['be10_chg']) > CONFIG["BE_CHANGE_LIMIT"]:
        if current_time - last_alert_times.get('BE10', 0) > CONFIG["ALERT_COOLDOWN"]:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT (10Y) {tag} SỐC:</b> {abs(data['be10_chg']):.3f} điểm")
            last_alert_times['BE10'] = current_time

    # 4. Check Gold H1 (Sốc giá)
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 CHẠY ĐIÊN:</b> {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    # Gửi cảnh báo ngay lập tức nếu có
    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO KHẨN CẤP</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- PHẦN 2: BÁO CÁO D1 (MỖI 30 PHÚT) ---
    current_minute = datetime.now().minute
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {data['gold_price']:.1f}\n"
            f"   Change: {sign(data['gold_change'])}{data['gold_change']:.1f}$ ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>US Breakeven (Lạm phát):</b>\n"
            f"   • 10Y: {data['be10_val']:.2f}% (Chg: {sign(data['be10_chg'])}{data['be10_chg']:.3f} điểm)\n"
            f"   • 05Y: {data['be05_val']:.2f}% (Chg: {sign(data['be05_chg'])}{data['be05_chg']:.3f} điểm)\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']:.2f} ({sign(data['vix_pct'])}{data['vix_pct']:.1f}%)\n"
            f"🌪 <b>GVZ:</b> {data['gvz']:.2f} ({sign(data['gvz_pct'])}{data['gvz_pct']:.1f}%)\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
