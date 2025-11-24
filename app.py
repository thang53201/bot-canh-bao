from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests

app = Flask(__name__)

# ==============================================================================
# CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAGxxxxxxxxxxxx",  # <-- Điền lại Token của bạn
    "TELEGRAM_CHAT_ID": "546450726x",               # <-- Điền lại Chat ID của bạn
    
    # Ngưỡng cảnh báo
    "VIX_VALUE_LIMIT": 30,
    "VIX_PCT_CHANGE_LIMIT": 15,
    "GVZ_VALUE_LIMIT": 25,
    "GVZ_PCT_CHANGE_LIMIT": 10,
    "T10YIE_CHANGE_LIMIT": 0.25,
    "FEDWATCH_CHANGE_LIMIT": 20.0,
    "GOLD_H1_RANGE_LIMIT": 40.0,
    "SPDR_TONS_LIMIT": 5.0,
    
    # CHỐNG SPAM: Thời gian chờ giữa 2 lần báo khẩn cấp (giây)
    "ALERT_COOLDOWN": 3600  # 3600s = 60 phút. Báo xong 1 lần sẽ im 1 tiếng.
}

# Bộ nhớ tạm để lưu thời gian đã báo động gần nhất
# Cấu trúc: {'VIX': timestamp, 'GOLD': timestamp, ...}
last_alert_times = {}

# ==============================================================================
# HÀM XỬ LÝ
# ==============================================================================
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
        print(f"Lỗi gửi Telegram: {e}")

def get_market_data():
    data = {}
    try:
        # Lấy dữ liệu D1 (Ngày) để tính % thay đổi chuẩn TradingView
        tickers = ["GC=F", "^VIX", "^GVZ", "^TNX"]
        df = yf.download(tickers, period="2d", interval="1d", progress=False)
        
        # Lấy dữ liệu H1 (Giờ) chỉ riêng cho Vàng để check nến sốc
        gold_h1 = yf.download("GC=F", period="1d", interval="1h", progress=False)
        
        try:
            # 1. VIX (So D1)
            vix_cur = float(df['Close']['^VIX'].iloc[-1])
            vix_prev = float(df['Close']['^VIX'].iloc[-2])
            data['vix'] = vix_cur
            data['vix_pct'] = ((vix_cur - vix_prev) / vix_prev) * 100

            # 2. GVZ (So D1)
            gvz_cur = float(df['Close']['^GVZ'].iloc[-1])
            gvz_prev = float(df['Close']['^GVZ'].iloc[-2])
            data['gvz'] = gvz_cur
            data['gvz_pct'] = ((gvz_cur - gvz_prev) / gvz_prev) * 100

            # 3. US10Y / TNX (So D1)
            t10_cur = float(df['Close']['^TNX'].iloc[-1])
            t10_prev = float(df['Close']['^TNX'].iloc[-2])
            data['t10_change'] = t10_cur - t10_prev

            # 4. Vàng H1 (So High/Low nến hiện tại)
            if not gold_h1.empty:
                last_candle = gold_h1.iloc[-1]
                data['gold_price'] = float(last_candle['Close'])
                data['gold_h1_range'] = float(last_candle['High'] - last_candle['Low'])
            else:
                data['gold_price'] = 0
                data['gold_h1_range'] = 0
            
            # 5. Dữ liệu placeholder
            data['fed_change'] = 0.0
            data['spdr_flow'] = 0.0

        except IndexError:
            return None

    except Exception as e:
        print(f"Lỗi data: {e}")
        return None
    
    return data

# ==============================================================================
# LOGIC CHECK ALERT (CÓ CHỐNG SPAM)
# ==============================================================================
def check_for_alerts(data):
    alerts = []
    current_time = time.time()
    
    # Hàm con để check logic cooldown
    def should_alert(key):
        last_time = last_alert_times.get(key, 0)
        # Nếu chưa báo bao giờ HOẶC đã quá thời gian cooldown
        if current_time - last_time > CONFIG["ALERT_COOLDOWN"]:
            return True
        return False

    # 1. Check VIX
    if (data['vix'] > CONFIG["VIX_VALUE_LIMIT"] or data['vix_pct'] > CONFIG["VIX_PCT_CHANGE_LIMIT"]):
        if should_alert('VIX'):
            alerts.append(f"⚠️ <b>VIX ALERT:</b> {data['vix']:.2f} (Tăng {data['vix_pct']:.1f}%)")
            last_alert_times['VIX'] = current_time # Ghi nhớ thời gian báo

    # 2. Check GVZ
    if (data['gvz'] > CONFIG["GVZ_VALUE_LIMIT"] or data['gvz_pct'] > CONFIG["GVZ_PCT_CHANGE_LIMIT"]):
        if should_alert('GVZ'):
            alerts.append(f"⚠️ <b>GVZ ALERT:</b> {data['gvz']:.2f} (Bão Vàng)")
            last_alert_times['GVZ'] = current_time

    # 3. Check Yield
    if abs(data['t10_change']) > CONFIG["T10YIE_CHANGE_LIMIT"]:
        if should_alert('YIELD'):
            alerts.append(f"⚠️ <b>Yield Change:</b> {data['t10_change']:.3f} điểm")
            last_alert_times['YIELD'] = current_time

    # 4. Check Gold H1 (Quan trọng)
    if data['gold_h1_range'] >= CONFIG["GOLD_H1_RANGE_LIMIT"]:
        if should_alert('GOLD_H1'):
            alerts.append(f"🚨 <b>GOLD H1 SỐC:</b> Chạy {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    return alerts

# ==============================================================================
# ROUTE FLASK
# ==============================================================================
@app.route('/')
def home():
    return "Bot Anti-Spam Active"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    if not data:
        return "Error data", 500

    # 1. Xử lý Báo Động (Priority 1)
    alerts = check_for_alerts(data)
    if alerts:
        msg = "\n".join(alerts)
        full_msg = f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n(Đã kích hoạt chế độ im lặng 60p)\n\n{msg}\n\n👉 CHECK EA NGAY!"
        send_telegram_msg(full_msg)

    # 2. Xử lý Update định kỳ (Priority 2)
    # Gửi vào phút 00 và 30 hàng giờ
    current_minute = datetime.now().minute
    
    # Logic: Chỉ gửi update nếu PHÚT là 0 hoặc 30
    if current_minute == 0 or current_minute == 30:
        # Thêm logic nhỏ: Nếu vừa gửi alert xong thì thôi không gửi update cho đỡ rối?
        # Hoặc cứ gửi. Ở đây tôi để cứ gửi cho chắc ăn.
        status_msg = (
            f"📊 <b>MARKET UPDATE (D1 Logic)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"--------------------------\n"
            f"🥇 Gold Spot: {data['gold_price']:.1f}\n"
            f"🕯 Gold H1 Range: {data['gold_h1_range']:.1f} $\n"
            f"--------------------------\n"
            f"📉 VIX: {data['vix']:.1f} ({data['vix_pct']:+.1f}%)\n"
            f"🌪 GVZ: {data['gvz']:.1f} ({data['gvz_pct']:+.1f}%)\n"
            f"🇺🇸 US10Y Chg: {data['t10_change']:+.3f}"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    if alerts:
        return "Alert Sent"
    
    return "Checked. No Alert.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
