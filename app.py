import os
import requests
import yfinance as yf
import pandas as pd
import pytz
from datetime import datetime
from flask import Flask

app = Flask(__name__)

# --- CẤU HÌNH BOT ---
TELEGRAM_TOKEN = "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo"
CHAT_ID = "5464507208"

# Ký hiệu mã
# ^TNX: Lợi suất trái phiếu Mỹ 10 năm
TICKERS = {
    'GOLD': 'GC=F',
    'VIX': '^VIX',
    'GVZ': '^GVZ',
    'US10Y': '^TNX'
}

# --- HÀM GỬI TIN NHẮN ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

# --- HÀM LẤY DỮ LIỆU ---
def get_market_data():
    data = {}
    tickers_list = " ".join(TICKERS.values())
    try:
        # Lấy dữ liệu
        df = yf.download(tickers_list, period="5d", interval="1d", progress=False)
        
        for key, symbol in TICKERS.items():
            try:
                # Lấy giá đóng cửa gần nhất và giá hôm trước
                last_price = df['Close'][symbol].iloc[-1]
                prev_close = df['Close'][symbol].iloc[-2]
                
                change_point = last_price - prev_close # Số điểm thay đổi
                change_pct = (change_point / prev_close) * 100 # Phần trăm
                
                data[key] = {
                    'price': last_price,
                    'change_p': change_point, 
                    'change_pct': change_pct
                }
            except Exception as e:
                data[key] = {'price': 0, 'change_p': 0, 'change_pct': 0}

    except Exception as e:
        return None

    # Lấy Vàng H1 để check nến giật
    try:
        gold_h1 = yf.download(TICKERS['GOLD'], period="1d", interval="1h", progress=False)
        if not gold_h1.empty:
            current_candle = gold_h1.iloc[-1]
            data['GOLD_H1'] = {
                'close': current_candle['Close'].item(),
                'range': current_candle['High'].item() - current_candle['Low'].item()
            }
    except:
        pass

    return data

# --- LOGIC CHECK ---
@app.route('/run-check')
def run_check():
    vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(vn_tz)
    current_minute = now_vn.minute
    
    market_data = get_market_data()
    if not market_data:
        return "Lỗi data", 500

    alerts = []
    
    # --- 1. LOGIC CẢNH BÁO KHẨN CẤP (Ưu tiên cao) ---

    # GVZ: Chỉ cảnh báo khi TĂNG > 15% (Mới) hoặc Giá > 25
    gvz = market_data['GVZ']
    if gvz['price'] > 25:
        alerts.append(f"🔥 **GVZ CAO:** {gvz['price']:.2f}")
    if gvz['change_pct'] > 15: # Đã sửa thành 15%
        alerts.append(f"⚡ **GVZ TĂNG SỐC:** +{gvz['change_pct']:.2f}%")

    # VIX: Chỉ cảnh báo khi TĂNG > 10% (Mới) hoặc Giá > 30
    vix = market_data['VIX']
    if vix['price'] > 30:
        alerts.append(f"☠️ **VIX KHỦNG HOẢNG:** {vix['price']:.2f}")
    if vix['change_pct'] > 10: # Đã sửa thành 10%
        alerts.append(f"🎢 **VIX TĂNG SỐC:** +{vix['change_pct']:.2f}%")

    # US10Y: Cảnh báo theo ĐIỂM (Points)
    us10y = market_data['US10Y']
    # Nếu biến động quá 0.2 điểm (tăng hoặc giảm đều báo)
    if abs(us10y['change_p']) >= 0.2:
        icon = "📈" if us10y['change_p'] > 0 else "📉"
        alerts.append(f"{icon} **US10Y BIẾN ĐỘNG:** {us10y['change_p']:+.3f} điểm")

    # VÀNG H1: Quét 40 giá
    if 'GOLD_H1' in market_data:
        gold_h1 = market_data['GOLD_H1']
        if gold_h1['range'] >= 40:
            alerts.append(f"🚀 **VÀNG H1 QUÉT:** {gold_h1['range']:.1f} giá")

    # Gửi cảnh báo ngay lập tức nếu có
    if alerts:
        send_telegram("🚨 **CẢNH BÁO NÓNG** 🚨\n" + "\n".join(alerts))
        return "Sent Alert", 200

    # --- 2. BÁO CÁO ĐỊNH KỲ (Phút 00 và 30) ---
    if current_minute in [0, 1, 30, 31]:
        gold = market_data['GOLD']
        
        # Format báo cáo: US10Y ghi điểm, VIX/GVZ ghi %
        report = f"""
✅ **MARKET UPDATE {now_vn.strftime('%H:%M')}**
---------------------------
🥇 **GOLD:** {gold['price']:.1f} ({gold['change_p']:+.1f} giá)

🇺🇸 **US10Y (Yield):**
• Mức: {us10y['price']:.3f}%
• Thay đổi: **{us10y['change_p']:+.3f} điểm**

📊 **RISK (Chỉ số rủi ro):**
• VIX: {vix['price']:.2f} ({vix['change_pct']:+.2f}%)
• GVZ: {gvz['price']:.2f} ({gvz['change_pct']:+.2f}%)
"""
        send_telegram(report)
        return "Sent Report", 200

    return "No Alert", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
