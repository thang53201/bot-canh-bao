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
    
    # --- NGƯỠNG CẢNH BÁO KHẨN CẤP (Emergency) ---
    "VIX_LIMIT": 30,             # VIX > 30
    "GVZ_LIMIT": 25,             # GVZ > 25
    "GOLD_H1_LIMIT": 40.0,       # Nến H1 Vàng chạy > 40 giá
    "BE_CHANGE_LIMIT": 0.25,     # Lạm phát kỳ vọng đổi > 0.25 điểm
    
    "ALERT_COOLDOWN": 3600       # Thời gian chờ giữa 2 lần báo (60 phút)
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY DỮ LIỆU THÔNG MINH (FIX LỖI 0.00)
# ==============================================================================
def get_safe_d1_data(ticker_symbol):
    """
    Lấy dữ liệu D1. Tự động quét lùi 1 tháng để tìm ngày có dữ liệu gần nhất.
    Khắc phục triệt để lỗi Yahoo trả về 0.00 hoặc NaN.
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        # Lấy history 1 tháng
        hist = ticker.history(period="1mo")
        
        # Xóa các hàng rỗng (NaN)
        hist = hist.dropna(subset=['Close'])
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        # Lấy dòng cuối cùng (Hiện tại) và dòng sát cuối (Hôm qua)
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except Exception as e:
        print(f"Lỗi lấy data {ticker_symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_h1_range():
    """Lấy biên độ nến H1 hiện tại của Vàng để cảnh báo sốc"""
    try:
        data = yf.download("GC=F", period="1d", interval="1h", progress=False)
        if not data.empty:
            # Xử lý format mới của yfinance (tránh lỗi array)
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
    
    # 1. GOLD FUTURES (D1)
    cur, chg, pct = get_safe_d1_data("GC=F")
    data['gold_price'] = cur
    data['gold_change'] = chg
    data['gold_pct'] = pct
    
    # 2. US BREAKEVEN (Lạm phát kỳ vọng D1)
    # 10 Year
    cur, chg, pct = get_safe_d1_data("^T10YIE")
    data['be10_val'] = cur
    data['be10_chg'] = chg

    # 5 Year (Thay cho 2Y)
    cur, chg, pct = get_safe_d1_data("^T5YIE")
    data['be05_val'] = cur
    data['be05_chg'] = chg
    
    # 3. VIX & GVZ (D1)
    cur, chg, pct = get_safe_d1_data("^VIX")
    data['vix'] = cur
    data['vix_pct'] = pct
    
    cur, chg, pct = get_safe_d1_data("^GVZ")
    data['gvz'] = cur
    data['gvz_pct'] = pct

    # 4. GOLD H1 (Chỉ để check cảnh báo)
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
# 3. ROUTING & LOGIC CHÍNH
# ==============================================================================
@app.route('/')
def home():
    return "Bot V5 - Clean & Stable"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- A. KIỂM TRA CẢNH BÁO KHẨN CẤP (EMERGENCY) ---
    
    # 1. Vàng H1 Sốc (>40 giá)
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 CHẠY ĐIÊN:</b> {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    # 2. VIX Sốc (>30)
    if data['vix'] > CONFIG["VIX_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG ĐỎ:</b> {data['vix']:.2f}")
            last_alert_times['VIX'] = current_time
            
    # 3. GVZ Sốc (>25)
    if data['gvz'] > CONFIG["GVZ_LIMIT"]:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f}")
            last_alert_times['GVZ'] = current_time

    # 4. Lạm phát 10Y đảo chiều (>0.25 điểm)
    if abs(data['be10_chg']) > CONFIG["BE_CHANGE_LIMIT"]:
        if current_time - last_alert_times.get('BE10', 0) > CONFIG["ALERT_COOLDOWN"]:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y {tag} SỐC:</b> {abs(data['be10_chg']):.3f} điểm")
            last_alert_times['BE10'] = current_time

    # Gửi cảnh báo NGAY LẬP TỨC nếu có
    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO KHẨN CẤP</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- B. BẢNG TIN D1 ĐỊNH KỲ (Mỗi 30 phút) ---
    current_minute = datetime.now().minute
    # Khung giờ: Phút 00-02 và 30-32
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {data['gold_price']:.1f}\n"
            f"   Chg: {sign(data['gold_change'])}{data['gold_change']:.1f}$ ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>Lạm phát Kỳ vọng (Breakeven):</b>\n"
            f"   • 10Y: {data['be10_val']:.2f}% (Chg: {sign(data['be10_chg'])}{data['be10_chg']:.3f})\n"
            f"   • 05Y: {data['be05_val']:.2f}% (Chg: {sign(data['be05_chg'])}{data['be05_chg']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']:.2f} ({sign(data['vix_pct'])}{data['vix_pct']:.1f}%)\n"
            f"🌪 <b>GVZ:</b> {data['gvz']:.2f} ({sign(data['gvz_pct'])}{data['gvz_pct']:.1f}%)\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
