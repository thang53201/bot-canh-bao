from flask import Flask
import yfinance as yf
from datetime import datetime, timedelta
import time
import requests
import pandas as pd

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG) - ĐÃ CẬP NHẬT ID & KEY MỚI
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO KHẨN CẤP
    "VIX_LIMIT": 30,             # VIX > 30
    "GVZ_LIMIT": 25,             # GVZ > 25
    "GOLD_H1_LIMIT": 40.0,       # H1 Vàng > 40 giá
    "BE_CHANGE_LIMIT": 0.25,     # Lạm phát đổi > 0.25 điểm
    
    "ALERT_COOLDOWN": 3600       # Im lặng 60 phút sau khi báo
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY DỮ LIỆU THÔNG MINH (SMART FETCH)
# ==============================================================================
def get_safe_d1_data(ticker_symbol):
    """
    Tự động quét lùi 1 tháng để tìm ngày có dữ liệu gần nhất.
    Khắc phục triệt để lỗi Yahoo trả về 0.00 hoặc NaN cho mã Breakeven.
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        # Lấy lịch sử 1 tháng để chắc chắn có data
        hist = ticker.history(period="1mo")
        
        # Xóa các hàng bị rỗng (NaN)
        hist = hist.dropna(subset=['Close'])
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        # Lấy giá trị mới nhất (Current) và liền trước (Prev)
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except Exception:
        return 0.0, 0.0, 0.0

def get_gold_h1_range():
    """Biên độ H1 của Gold Futures"""
    try:
        data = yf.download("GC=F", period="1d", interval="1h", progress=False)
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
    
    # 1. GOLD FUTURES (GC=F) - Giữ nguyên theo ý bạn
    cur, chg, pct = get_safe_d1_data("GC=F")
    data['gold_price'] = cur
    data['gold_change'] = chg
    data['gold_pct'] = pct
    
    # 2. US BREAKEVEN (Lạm phát kỳ vọng) - Giữ nguyên
    # 10 Year (^T10YIE)
    cur, chg, pct = get_safe_d1_data("^T10YIE")
    data['be10_val'] = cur
    data['be10_chg'] = chg

    # 5 Year (^T5YIE) - Thay cho 2Y bị lỗi API
    cur, chg, pct = get_safe_d1_data("^T5YIE")
    data['be05_val'] = cur
    data['be05_chg'] = chg
    
    # 3. VIX & GVZ
    cur, chg, pct = get_safe_d1_data("^VIX")
    data['vix'] = cur
    data['vix_pct'] = pct
    
    cur, chg, pct = get_safe_d1_data("^GVZ")
    data['gvz'] = cur
    data['gvz_pct'] = pct

    # 4. GOLD H1 RANGE (Cho cảnh báo)
    data['gold_h1_range'] = get_gold_h1_range()
    
    # 5. SPDR & FED (Giữ hiển thị nhưng giá trị mặc định vì ko có API)
    data['spdr_val'] = 0 
    data['fed_val'] = 0
    
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
# 3. ROUTING & LOGIC
# ==============================================================================
@app.route('/')
def home():
    return "Bot V7 Active - Full Features"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- A. CẢNH BÁO KHẨN CẤP ---
    
    # 1. Vàng H1 Sốc
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 CHẠY ĐIÊN:</b> {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    # 2. VIX Sốc
    if data['vix'] > CONFIG["VIX_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG ĐỎ:</b> {data['vix']:.2f}")
            last_alert_times['VIX'] = current_time
            
    # 3. GVZ Sốc
    if data['gvz'] > CONFIG["GVZ_LIMIT"]:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f}")
            last_alert_times['GVZ'] = current_time

    # 4. Lạm phát đảo chiều
    if abs(data['be10_chg']) > CONFIG["BE_CHANGE_LIMIT"]:
        if current_time - last_alert_times.get('BE10', 0) > CONFIG["ALERT_COOLDOWN"]:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y {tag} SỐC:</b> {abs(data['be10_chg']):.3f} điểm")
            last_alert_times['BE10'] = current_time

    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- B. BÁO CÁO 30 PHÚT (D1 - ĐẦY ĐỦ MỤC) ---
    current_minute = datetime.now().minute
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""
        def icon(val): return "🟢" if val >= 0 else "🔴"

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {data['gold_price']:.1f}\n"
            f"   {icon(data['gold_change'])} {sign(data['gold_change'])}{data['gold_change']:.1f}$ ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>Lạm phát Kỳ vọng (Breakeven):</b>\n"
            f"   • 10Y: {data['be10_val']:.2f}% (Chg: {sign(data['be10_chg'])}{data['be10_chg']:.3f})\n"
            f"   • 05Y: {data['be05_val']:.2f}% (Chg: {sign(data['be05_chg'])}{data['be05_chg']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']:.2f} ({sign(data['vix_pct'])}{data['vix_pct']:.1f}%)\n"
            f"🌪 <b>GVZ:</b> {data['gvz']:.2f} ({sign(data['gvz_pct'])}{data['gvz_pct']:.1f}%)\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR:</b> {data['spdr_val']} tấn (N/A)\n"
            f"⚖️ <b>FedWatch:</b> {data['fed_val']}% (N/A)\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
