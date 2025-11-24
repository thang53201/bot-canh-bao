from flask import Flask
import yfinance as yf
from datetime import datetime
import pandas as pd
import requests

app = Flask(__name__)

# ==============================================================================
# CẤU HÌNH (CONFIG) - ĐIỀN API CỦA BẠN VÀO ĐÂY
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",  # <-- ID từ ảnh của bạn, nhớ điền nốt phần che
    "TELEGRAM_CHAT_ID": "5464507208",               # <-- ID từ ảnh của bạn
    
    # Ngưỡng cảnh báo
    "VIX_VALUE_LIMIT": 30,
    "VIX_PCT_CHANGE_LIMIT": 15,
    "GVZ_VALUE_LIMIT": 25,
    "GVZ_PCT_CHANGE_LIMIT": 10,
    "T10YIE_CHANGE_LIMIT": 0.25,
    "FEDWATCH_CHANGE_LIMIT": 20.0,
    "GOLD_H1_RANGE_LIMIT": 40.0,
    "SPDR_TONS_LIMIT": 5.0,
}

# ==============================================================================
# HÀM XỬ LÝ (HELPER FUNCTIONS)
# ==============================================================================
def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
        payload = {
            "chat_id": CONFIG['TELEGRAM_CHAT_ID'],
            "text": message,
            "parse_mode": "HTML" # Để bôi đậm chữ nếu cần
        }
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

def get_market_data():
    data = {}
    try:
        # Tải dữ liệu: Vàng (GC=F), VIX (^VIX), GVZ (^GVZ), TNX (^TNX)
        tickers = ["GC=F", "^VIX", "^GVZ", "^TNX"]
        df = yf.download(tickers, period="2d", interval="1d", progress=False)
        gold_h1 = yf.download("GC=F", period="1d", interval="1h", progress=False)
        
        # Lấy dữ liệu an toàn (tránh lỗi nếu thiếu data)
        try:
            # VIX
            vix_cur = df['Close']['^VIX'].iloc[-1]
            vix_prev = df['Close']['^VIX'].iloc[-2]
            data['vix'] = float(vix_cur)
            data['vix_pct'] = ((vix_cur - vix_prev) / vix_prev) * 100

            # GVZ
            gvz_cur = df['Close']['^GVZ'].iloc[-1]
            gvz_prev = df['Close']['^GVZ'].iloc[-2]
            data['gvz'] = float(gvz_cur)
            data['gvz_pct'] = ((gvz_cur - gvz_prev) / gvz_prev) * 100

            # US10Y (TNX) - Demo cho T10YIE
            t10_cur = df['Close']['^TNX'].iloc[-1]
            t10_prev = df['Close']['^TNX'].iloc[-2]
            data['t10_change'] = float(t10_cur - t10_prev)

            # Vàng H1
            if not gold_h1.empty:
                last_candle = gold_h1.iloc[-1]
                data['gold_price'] = float(last_candle['Close'])
                data['gold_h1_range'] = float(last_candle['High'] - last_candle['Low'])
            else:
                data['gold_price'] = 0
                data['gold_h1_range'] = 0
            
            # Placeholder cho FedWatch/SPDR (Giá trị giả lập 0 để code chạy)
            data['fed_change'] = 0.0
            data['spdr_flow'] = 0.0

        except IndexError:
            return None # Không đủ dữ liệu 2 ngày

    except Exception as e:
        print(f"Lỗi yfinance: {e}")
        return None
    
    return data

# ==============================================================================
# ROUTE FLASK (ĐỊA CHỈ ĐỂ CRON-JOB GỌI VÀO)
# ==============================================================================
@app.route('/')
def home():
    return "Bot is Alive!"

@app.route('/run_check')
def run_check():
    """Hàm này sẽ được Cron-job gọi mỗi 1 phút"""
    print("Checking market...")
    data = get_market_data()
    
    if not data:
        return "Error fetching data", 500

    alerts = []
    
    # 1. Logic kiểm tra Báo Động (Alerts)
    if data['vix'] > CONFIG["VIX_VALUE_LIMIT"] or data['vix_pct'] > CONFIG["VIX_PCT_CHANGE_LIMIT"]:
        alerts.append(f"⚠️ <b>VIX ALERT:</b> {data['vix']:.2f} (Tăng {data['vix_pct']:.1f}%)")
        
    if data['gvz'] > CONFIG["GVZ_VALUE_LIMIT"] or data['gvz_pct'] > CONFIG["GVZ_PCT_CHANGE_LIMIT"]:
        alerts.append(f"⚠️ <b>GVZ ALERT:</b> {data['gvz']:.2f} (Bão Vàng)")
        
    if abs(data['t10_change']) > CONFIG["T10YIE_CHANGE_LIMIT"]:
        alerts.append(f"⚠️ <b>Yield Change:</b> {data['t10_change']:.3f} điểm")
        
    if data['gold_h1_range'] >= CONFIG["GOLD_H1_RANGE_LIMIT"]:
        alerts.append(f"🚨 <b>GOLD H1 SỐC:</b> {data['gold_h1_range']:.1f} giá")

    # Gửi tin nhắn KHẨN nếu có biến
    if alerts:
        msg = "\n".join(alerts)
        full_msg = f"🔥🔥 <b>CẢNH BÁO RỦI RO</b> 🔥🔥\n\n{msg}\n\n👉 KIỂM TRA EA NGAY!"
        send_telegram_msg(full_msg)
        return "Alert Sent!"

    # 2. Logic Báo cáo định kỳ (Update mỗi 30 phút)
    # Vì Cron gọi mỗi phút, ta check phút hiện tại. Nếu phút là 0 hoặc 30 thì gửi.
    current_minute = datetime.now().minute
    if current_minute == 0 or current_minute == 30:
        status_msg = (
            f"📊 <b>MARKET UPDATE 30M</b>\n"
            f"Gold: {data['gold_price']:.1f} | H1: {data['gold_h1_range']:.1f}\n"
            f"VIX: {data['vix']:.1f} | GVZ: {data['gvz']:.1f}\n"
            f"US10Y Change: {data['t10_change']:.3f}"
        )
        send_telegram_msg(status_msg)
        return "Update Sent!"

    return "No Alert", 200

if __name__ == '__main__':
    # Chạy cục bộ để test
    app.run(host='0.0.0.0', port=5000)
