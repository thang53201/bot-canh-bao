import yfinance as yf
import time
from datetime import datetime, timedelta
import pandas as pd

# ==============================================================================
# CẤU HÌNH NGƯỠNG BÁO ĐỘNG (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",  # Điền Token Bot Telegram của bạn
    "TELEGRAM_CHAT_ID": "5464507208",   # Điền Chat ID của bạn
    
    # 1. Cấu hình VIX & GVZ
    "VIX_VALUE_LIMIT": 30,          # Giá trị tuyệt đối > 30
    "VIX_PCT_CHANGE_LIMIT": 15,     # Tăng > 15% trong ngày
    "GVZ_VALUE_LIMIT": 25,          # Giá trị tuyệt đối > 25
    "GVZ_PCT_CHANGE_LIMIT": 10,     # Tăng > 10% trong ngày

    # 2. Cấu hình Kỳ vọng Lạm phát (T10YIE / Breakeven)
    "T10YIE_CHANGE_LIMIT": 0.25,    # Biến động +/- 0.25 điểm
    
    # 3. Cấu hình FED WATCH (Lãi suất)
    "FEDWATCH_CHANGE_LIMIT": 20.0,  # Thay đổi > 20% (Mức cực đoan cho EA 100 giá)

    # 4. Cấu hình Vàng (XAUUSD)
    "GOLD_H1_RANGE_LIMIT": 40.0,    # Nến H1 chạy > 40 giá ($400 pips)
    
    # 5. Cấu hình Quỹ SPDR
    "SPDR_TONS_LIMIT": 5.0,         # Mua/Bán > 5 tấn/ngày
    "SPDR_CONSECUTIVE_DAYS": 3,     # Số ngày mua/bán ròng liên tiếp
}

# ==============================================================================
# HÀM GIẢ LẬP / XỬ LÝ DỮ LIỆU KHÓ (SPDR & FEDWATCH)
# ==============================================================================
# Lưu ý: FedWatch và Tonnage SPDR không có API miễn phí trực tiếp qua yfinance.
# Bạn cần nhập tay hoặc dùng API trả phí. Ở đây tôi để hàm chờ (Placeholder).

def get_fedwatch_change():
    """
    Giả lập lấy thay đổi FedWatch. 
    Thực tế cần crawl từ web CME hoặc nhập tay nếu thấy tin mạnh.
    Hiện tại trả về 0.0 để code chạy không lỗi.
    """
    return 0.0 

def get_spdr_status():
    """
    Giả lập check SPDR.
    Logic: Trả về số tấn mua/bán hôm nay và list lịch sử 3 ngày.
    Ví dụ: return -6.0, [-2.0, -3.0, -6.0] (Bán 6 tấn, 3 ngày bán liên tiếp)
    """
    # Demo dữ liệu: Hôm nay không mua bán, lịch sử bình thường
    current_flow = 0.0
    history_flows = [0.0, 0.0, 0.0] 
    return current_flow, history_flows

# ==============================================================================
# HÀM LẤY DỮ LIỆU THỊ TRƯỜNG (CORE)
# ==============================================================================
def get_market_data():
    data = {}
    try:
        # Tải dữ liệu: Vàng (GC=F), VIX (^VIX), GVZ (^GVZ), 10Y Yield (^TNX - Proxy cho T10YIE nếu thiếu)
        # Lưu ý: T10YIE trên Yahoo đôi khi bị ẩn, dùng ^TNX (Lợi suất 10Y) để test code, 
        # Nếu bạn có mã chính xác trên Yahoo cho Breakeven thì thay thế vào.
        tickers = ["GC=F", "^VIX", "^GVZ", "^TNX"] 
        
        # Lấy dữ liệu 2 ngày để tính % thay đổi so với đóng cửa hôm qua (D1 Logic)
        df = yf.download(tickers, period="2d", interval="1d", progress=False)
        
        # Lấy dữ liệu nến H1 cho Vàng để check biến động giờ
        gold_h1 = yf.download("GC=F", period="1d", interval="1h", progress=False)
        
        # 1. Xử lý VIX
        vix_cur = df['Close']['^VIX'].iloc[-1]
        vix_prev = df['Close']['^VIX'].iloc[-2]
        data['vix'] = vix_cur
        data['vix_pct'] = ((vix_cur - vix_prev) / vix_prev) * 100

        # 2. Xử lý GVZ
        gvz_cur = df['Close']['^GVZ'].iloc[-1]
        gvz_prev = df['Close']['^GVZ'].iloc[-2]
        data['gvz'] = gvz_cur
        data['gvz_pct'] = ((gvz_cur - gvz_prev) / gvz_prev) * 100

        # 3. Xử lý T10YIE (Dùng tạm ^TNX để demo logic tính toán điểm)
        t10_cur = df['Close']['^TNX'].iloc[-1]
        t10_prev = df['Close']['^TNX'].iloc[-2]
        data['t10_val'] = t10_cur
        data['t10_change'] = t10_cur - t10_prev # Tính thay đổi tuyệt đối (điểm)

        # 4. Xử lý Vàng H1 (Giá hiện tại & Biên độ nến H1)
        if not gold_h1.empty:
            last_candle = gold_h1.iloc[-1]
            data['gold_price'] = last_candle['Close']
            data['gold_h1_range'] = last_candle['High'] - last_candle['Low']
        else:
            data['gold_price'] = 0
            data['gold_h1_range'] = 0

        # 5. Dữ liệu ngoài (Fed & SPDR)
        data['fed_change'] = get_fedwatch_change()
        spdr_cur, spdr_hist = get_spdr_status()
        data['spdr_flow'] = spdr_cur
        data['spdr_hist'] = spdr_hist

    except Exception as e:
        print(f"Lỗi lấy dữ liệu: {e}")
        return None
    
    return data

# ==============================================================================
# HÀM GỬI CẢNH BÁO (LOGIC CHÍNH)
# ==============================================================================
def check_triggers(data):
    alerts = []
    
    # 1. Check VIX
    if data['vix'] > CONFIG["VIX_VALUE_LIMIT"] or data['vix_pct'] > CONFIG["VIX_PCT_CHANGE_LIMIT"]:
        alerts.append(f"⚠️ VIX BÁO ĐỘNG: {data['vix']:.2f} (Tăng {data['vix_pct']:.2f}%)")

    # 2. Check GVZ
    if data['gvz'] > CONFIG["GVZ_VALUE_LIMIT"] or data['gvz_pct'] > CONFIG["GVZ_PCT_CHANGE_LIMIT"]:
        alerts.append(f"⚠️ GVZ (Bão Vàng): {data['gvz']:.2f} (Tăng {data['gvz_pct']:.2f}%)")

    # 3. Check T10YIE / Yield
    if abs(data['t10_change']) > CONFIG["T10YIE_CHANGE_LIMIT"]:
        tag = "TĂNG" if data['t10_change'] > 0 else "GIẢM"
        alerts.append(f"⚠️ Lợi suất/Kỳ vọng {tag} mạnh: {abs(data['t10_change']):.3f} điểm")

    # 4. Check FedWatch
    if abs(data['fed_change']) >= CONFIG["FEDWATCH_CHANGE_LIMIT"]:
        alerts.append(f"🚨 FEDWATCH ĐẢO CHIỀU: {data['fed_change']}% (Cực nguy hiểm)")

    # 5. Check SPDR
    # - Điều kiện 1: Mua bán > 5 tấn
    if abs(data['spdr_flow']) >= CONFIG["SPDR_TONS_LIMIT"]:
         tag = "MUA" if data['spdr_flow'] > 0 else "XẢ"
         alerts.append(f"🐋 CÁ MẬP SPDR {tag}: {abs(data['spdr_flow'])} tấn")
    # - Điều kiện 2: 3 ngày liên tiếp cùng chiều
    # Logic: Nếu cả 3 ngày đều dương (mua) hoặc đều âm (bán) và khác 0
    if all(x > 0 for x in data['spdr_hist']) or all(x < 0 for x in data['spdr_hist']):
        alerts.append(f"⚠️ SPDR hành động 3 ngày liên tiếp!")

    # 6. Check Gold H1 Range
    if data['gold_h1_range'] >= CONFIG["GOLD_H1_RANGE_LIMIT"]:
        alerts.append(f"🚨 VÀNG H1 BIẾN ĐỘNG MẠNH: {data['gold_h1_range']:.2f} giá ($)")

    return alerts

def send_telegram_msg(message):
    # Code gửi telegram thật (Placeholder)
    print("\n" + "="*40)
    print(f"📩 SENDING TELEGRAM:\n{message}")
    print("="*40 + "\n")
    # Để kích hoạt gửi thật, bỏ comment dòng dưới và cài thư viện requests
    # import requests
    # url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage?chat_id={CONFIG['TELEGRAM_CHAT_ID']}&text={message}"
    # requests.get(url)

# ==============================================================================
# MAIN LOOP
# ==============================================================================
def main():
    print("🤖 BOT MONITOR STARTED - MODE: EA RISK 100 PRICES")
    last_update_time = datetime.now() - timedelta(minutes=31) # Để trigger update ngay lần đầu

    while True:
        current_time = datetime.now()
        data = get_market_data()
        
        if data:
            # 1. Kiểm tra điều kiện báo động (Alert)
            alerts = check_triggers(data)
            
            if alerts:
                # Nếu có biến => Gửi ngay lập tức
                msg_content = "\n".join(alerts)
                full_msg = f"🔥🔥 CẢNH BÁO RỦI RO 🔥🔥\nThời gian: {current_time.strftime('%H:%M')}\n\n{msg_content}\n\n👉 KIỂM TRA EA NGAY!"
                send_telegram_msg(full_msg)
            
            # 2. Kiểm tra điều kiện báo cáo định kỳ (Update)
            # Chỉ gửi nếu ko có báo động và đã qua 30 phút
            elif (current_time - last_update_time).total_seconds() >= 1800: # 1800s = 30p
                status_msg = (
                    f"📊 MARKET UPDATE 30M\n"
                    f"Gold: {data['gold_price']:.1f} | H1 Range: {data['gold_h1_range']:.1f}\n"
                    f"VIX: {data['vix']:.1f} ({data['vix_pct']:.1f}%)\n"
                    f"GVZ: {data['gvz']:.1f} ({data['gvz_pct']:.1f}%)\n"
                    f"US10Y/T10 Change: {data['t10_change']:.3f}\n"
                    f"FedWatch Change: {data['fed_change']}%\n"
                    f"SPDR Today: {data['spdr_flow']} tấn"
                )
                send_telegram_msg(status_msg)
                last_update_time = current_time
            
            else:
                print(f"[{current_time.strftime('%H:%M:%S')}] Monitoring... Gold: {data['gold_price']:.1f}, H1: {data['gold_h1_range']:.1f}")

        # Nghỉ 60 giây trước khi quét lại
        time.sleep(60)

if __name__ == "__main__":
    main()
