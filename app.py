from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG) - ĐIỀN API CỦA BẠN
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",  # <-- NHỚ ĐIỀN LẠI TOKEN
    "TELEGRAM_CHAT_ID": "5464507208",               # <-- NHỚ ĐIỀN LẠI CHAT ID
    
    # --- CẤU HÌNH NGƯỠNG BÁO ĐỘNG ---
    "VIX_VALUE_LIMIT": 30,          # VIX > 30
    "VIX_PCT_CHANGE_LIMIT": 15,     # VIX tăng > 15% (so với hôm qua)
    "GVZ_VALUE_LIMIT": 25,          # GVZ > 25
    "GVZ_PCT_CHANGE_LIMIT": 10,     # GVZ tăng > 10%
    "T10YIE_CHANGE_LIMIT": 0.25,    # Yield thay đổi > 0.25 điểm
    "FEDWATCH_CHANGE_LIMIT": 20.0,  # FedWatch đổi > 20%
    "GOLD_H1_RANGE_LIMIT": 40.0,    # Nến H1 Vàng chạy > 40 giá
    
    # --- CẤU HÌNH CHỐNG SPAM ---
    "ALERT_COOLDOWN": 3600  # 60 phút. Báo lỗi xong sẽ im 1 tiếng mới báo lại lỗi đó.
}

# Bộ nhớ tạm để lưu thời gian đã báo động (reset mỗi khi redeploy)
last_alert_times = {}

# ==============================================================================
# 2. HÀM HỖ TRỢ (GỬI TIN, LẤY DATA)
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
        print(f"Lỗi gửi Tele: {e}")

def get_market_data():
    data = {}
    try:
        # Lấy dữ liệu D1 (Ngày) cho các chỉ số vĩ mô
        tickers = ["GC=F", "^VIX", "^GVZ", "^TNX"]
        df = yf.download(tickers, period="2d", interval="1d", progress=False)
        
        # Lấy riêng Vàng H1 để soi nến giờ
        gold_h1 = yf.download("GC=F", period="1d", interval="1h", progress=False)
        
        try:
            # VIX
            vix_cur = float(df['Close']['^VIX'].iloc[-1])
            vix_prev = float(df['Close']['^VIX'].iloc[-2])
            data['vix'] = vix_cur
            data['vix_pct'] = ((vix_cur - vix_prev) / vix_prev) * 100

            # GVZ
            gvz_cur = float(df['Close']['^GVZ'].iloc[-1])
            gvz_prev = float(df['Close']['^GVZ'].iloc[-2])
            data['gvz'] = gvz_cur
            data['gvz_pct'] = ((gvz_cur - gvz_prev) / gvz_prev) * 100

            # Yield (TNX)
            t10_cur = float(df['Close']['^TNX'].iloc[-1])
            t10_prev = float(df['Close']['^TNX'].iloc[-2])
            data['t10_change'] = t10_cur - t10_prev

            # Gold H1
            if not gold_h1.empty:
                last_candle = gold_h1.iloc[-1]
                data['gold_price'] = float(last_candle['Close'])
                data['gold_h1_range'] = float(last_candle['High'] - last_candle['Low'])
            else:
                data['gold_price'] = 0
                data['gold_h1_range'] = 0
                
        except IndexError:
            return None # Không đủ dữ liệu để so sánh

    except Exception as e:
        print(f"Lỗi lấy data: {e}")
        return None
    
    return data

# ==============================================================================
# 3. LOGIC KIỂM TRA BÁO ĐỘNG (CHECK TRIGGERS)
# ==============================================================================
def check_for_alerts(data):
    alerts = []
    current_time = time.time()
    
    def should_alert(key):
        # Nếu chưa báo bao giờ HOẶC đã quá 60 phút từ lần báo trước
        if current_time - last_alert_times.get(key, 0) > CONFIG["ALERT_COOLDOWN"]:
            return True
        return False

    # 1. VIX
    if (data['vix'] > CONFIG["VIX_VALUE_LIMIT"] or data['vix_pct'] > CONFIG["VIX_PCT_CHANGE_LIMIT"]):
        if should_alert('VIX'):
            alerts.append(f"⚠️ <b>VIX ALERT:</b> {data['vix']:.2f} (Tăng {data['vix_pct']:.1f}%)")
            last_alert_times['VIX'] = current_time

    # 2. GVZ
    if (data['gvz'] > CONFIG["GVZ_VALUE_LIMIT"] or data['gvz_pct'] > CONFIG["GVZ_PCT_CHANGE_LIMIT"]):
        if should_alert('GVZ'):
            alerts.append(f"⚠️ <b>GVZ ALERT:</b> {data['gvz']:.2f} (Bão Vàng)")
            last_alert_times['GVZ'] = current_time

    # 3. Yield
    if abs(data['t10_change']) > CONFIG["T10YIE_CHANGE_LIMIT"]:
        if should_alert('YIELD'):
            alerts.append(f"⚠️ <b>Yield Biến động:</b> {data['t10_change']:+.3f} điểm")
            last_alert_times['YIELD'] = current_time

    # 4. Gold H1
    if data['gold_h1_range'] >= CONFIG["GOLD_H1_RANGE_LIMIT"]:
        if should_alert('GOLD_H1'):
            alerts.append(f"🚨 <b>GOLD H1 SỐC:</b> Nến chạy {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time

    return alerts

# ==============================================================================
# 4. MAIN ROUTE (CRON-JOB GỌI VÀO ĐÂY MỖI PHÚT)
# ==============================================================================
@app.route('/')
def home():
    return "Bot is Running..."

@app.route('/run_check')
def run_check():
    print("--- Cronjob Checking ---")
    data = get_market_data()
    
    if not data:
        return "Data Error", 500

    # BƯỚC 1: KIỂM TRA BÁO ĐỘNG KHẨN CẤP (Ưu tiên số 1)
    alerts = check_for_alerts(data)
    if alerts:
        msg = "\n".join(alerts)
        full_msg = f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n(Bot sẽ im lặng 60p)\n\n{msg}\n\n👉 KIỂM TRA EA NGAY!"
        send_telegram_msg(full_msg)
        return "Alert Sent"

    # BƯỚC 2: KIỂM TRA BÁO CÁO ĐỊNH KỲ (Mỗi 30 phút)
    current_minute = datetime.now().minute
    
    # SỬA LỖI: Cho phép trễ 2 phút (0-2 và 30-32) phòng trường hợp Render khởi động chậm
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        # Kiểm tra xem vừa mới gửi chưa để tránh gửi đúp trong khung giờ 2 phút này
        # (Logic đơn giản: Nếu giây < 10 thì gửi, để đảm bảo chỉ gửi 1 lần đầu tiên)
        # Tuy nhiên với Cron 1 phút/lần thì không sợ spam lắm.
        
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

    return "Checked. No Alert.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
