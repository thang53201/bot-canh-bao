import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
from flask import Flask
from datetime import datetime
import pytz

# --- CẤU HÌNH (ĐIỀN LẠI THÔNG TIN CỦA BẠN) ---
TOKEN = '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo' # Điền Token của bạn
CHAT_ID = '5464507208'                   # Điền Chat ID của bạn

app = Flask(__name__)

# --- CẤU HÌNH NGƯỠNG CẢNH BÁO (LOGIC CỦA BẠN) ---
THRESHOLDS = {
    'VIX_DANGER': 30,           # VIX >= 30 là nguy hiểm
    'VIX_CHANGE_PCT': 10.0,     # Biến động >= 10%
    'GVZ_DANGER': 25,           # GVZ >= 25
    'GVZ_CHANGE_PCT': 10.0,     # GVZ tăng >= 10%
    'US10Y_CHANGE': 0.25,       # Thay đổi 0.25 điểm
    'GOLD_MOVE_DOLLARS': 15.0,  # Vàng chạy 15$ (~1500 pips)
    'SPDR_CHANGE_TONS': 5.0,    # Quỹ mua/bán > 5 tấn
    'RETRASE_TARGET': 0.2       # Hồi 20% là an toàn
}

# File tạm để lưu trạng thái Dashboard
MSG_ID_FILE = "msg_id.txt"

def get_spdr_data():
    """Đọc dữ liệu trực tiếp từ file CSV của quỹ SPDR"""
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        s = requests.get(url, timeout=5).content
        # Đọc CSV, bỏ qua header rác
        df = pd.read_csv(io.StringIO(s.decode('utf-8')), skiprows=6)
        df = df[['Date', 'Total Net Asset Value Tonnes in the Trust']].dropna().tail(5)
        
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        tonnes_now = float(last_row['Total Net Asset Value Tonnes in the Trust'])
        tonnes_prev = float(prev_row['Total Net Asset Value Tonnes in the Trust'])
        change = tonnes_now - tonnes_prev
        
        # Check logic chuỗi 3 ngày (Streak)
        streak_buy = all(df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3) > 0)
        streak_sell = all(df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3) < 0)
        
        return {'tonnes': tonnes_now, 'change': change, 'streak_buy': streak_buy, 'streak_sell': streak_sell}
    except Exception as e:
        print(f"Lỗi SPDR: {e}")
        return {'tonnes': 0, 'change': 0, 'streak_buy': False, 'streak_sell': False}

async def logic_check_market():
    bot = telegram.Bot(token=TOKEN)
    alerts = [] # Danh sách cảnh báo (Sẽ RUNG chuông)
    
    try:
        # 1. Lấy dữ liệu Yahoo Finance (Gold, GVZ, VIX, US10Y, US02Y)
        tickers = ["GC=F", "^GVZ", "^VIX", "^TNX", "^IRX"] 
        # GC=F: Vàng, ^TNX: 10 năm, ^IRX: 13 tuần (Proxy lãi suất Fed)
        
        data = yf.download(tickers, period="2d", interval="1d", progress=False)
        
        # Lấy giá trị hiện tại (Latest) và Đóng cửa hôm qua (Prev)
        def get_val(ticker):
            try:
                closes = data['Close'][ticker].dropna()
                if len(closes) < 2: return 0, 0, 0
                curr = closes.iloc[-1]
                prev = closes.iloc[-2]
                chg = curr - prev
                pct = (chg / prev) * 100 if prev != 0 else 0
                return round(curr, 2), round(chg, 2), round(pct, 2)
            except: return 0, 0, 0

        gold_price, gold_chg, gold_pct = get_val("GC=F")
        gvz_val, gvz_chg, gvz_pct = get_val("^GVZ")
        vix_val, vix_chg, vix_pct = get_val("^VIX")
        us10y_val, us10y_chg, us10y_pct = get_val("^TNX")
        us02y_val, us02y_chg, us02y_pct = get_val("^IRX")

        # 2. Xử lý Logic Cảnh báo (Chỉ RUNG khi chạm ngưỡng)
        
        # --- VIX ---
        if vix_val >= THRESHOLDS['VIX_DANGER']:
            alerts.append(f"🔴 **NGUY HIỂM:** VIX đạt {vix_val} (Mức rủi ro cực cao)")
        if vix_pct >= THRESHOLDS['VIX_CHANGE_PCT']:
            alerts.append(f"⚠️ **VIX BÙNG NỔ:** Tăng {vix_pct}% trong ngày")

        # --- GVZ ---
        if gvz_val >= THRESHOLDS['GVZ_DANGER']:
            alerts.append(f"🌪 **BÃO VÀNG:** GVZ đạt {gvz_val} (>25)")
        if gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT']:
            alerts.append(f"⚠️ **GVZ TĂNG MẠNH:** +{gvz_pct}%")

        # --- US10Y ---
        if abs(us10y_chg) >= THRESHOLDS['US10Y_CHANGE']:
            trend = "TĂNG" if us10y_chg > 0 else "GIẢM"
            alerts.append(f"🇺🇸 **US10Y BIẾN ĐỘNG:** {trend} {abs(us10y_chg)} điểm (Signal mạnh)")

        # --- SPDR GOLD TRUST ---
        spdr = get_spdr_data()
        if abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS']:
            action = "MUA GOM" if spdr['change'] > 0 else "XẢ HÀNG"
            alerts.append(f"🐋 **SPDR {action}:** {abs(spdr['change'])} tấn hôm nay")
        if spdr['streak_buy']: alerts.append("🐋 **SPDR:** Mua ròng 3 ngày liên tiếp")
        if spdr['streak_sell']: alerts.append("🐋 **SPDR:** Xả ròng 3 ngày liên tiếp")

        # --- GOLD PRICE & RETRACEMENT (DCA Logic) ---
        # Logic: Nếu giá chạy > 15$ (1500 pips)
        if abs(gold_chg) >= THRESHOLDS['GOLD_MOVE_DOLLARS']:
            # Tính mức hồi quy
            # Giả sử High/Low trong ngày (đơn giản hoá lấy Open-Current)
            # Thực tế để chính xác cần tick data, nhưng ở đây dùng biến động giá đóng cửa
            alerts.append(f"💰 **VÀNG CHẠY MẠNH:** Biến động {gold_chg}$ (~{int(abs(gold_chg)*100)} pips)")
            
            # Logic kiểm tra hồi (cơ bản)
            # Nếu giảm mạnh mà chưa hồi -> Bot cảnh báo
            # (Phần này cần database để lưu đỉnh/đáy chuẩn, đây là logic cảnh báo nhanh)
            
        # --- LÃI SUẤT (Thay FedWatch bằng US02Y) ---
        if abs(us02y_chg) >= 0.2:
            alerts.append(f"🏦 **LÃI SUẤT (US02Y):** Biến động mạnh {us02y_chg}%")

        # 3. GỬI CẢNH BÁO (RUNG CHUÔNG)
        if alerts:
            msg_alert = "\n".join(alerts)
            await bot.send_message(chat_id=CHAT_ID, text=msg_alert, parse_mode='Markdown')

        # 4. CẬP NHẬT DASHBOARD (KHÔNG RUNG, CHỈ HIỂN THỊ)
        vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
        time_str = datetime.now(vn_tz).strftime('%H:%M %d/%m')
        
        dashboard_msg = f"""
📊 **MARKET MONITOR (Realtime)**
⏱ _Update: {time_str}_
-----------------------------
🥇 **XAUUSD:** {gold_price} ({gold_chg}$)
🌊 **GVZ:** {gvz_val} ({gvz_pct}%) {'🔥' if gvz_val>20 else ''}
😱 **VIX:** {vix_val} ({vix_pct}%) {'☠️' if vix_val>25 else ''}
🇺🇸 **US10Y:** {us10y_val}% (Var: {us10y_chg})
🏦 **US02Y:** {us02y_val}% (Fed Proxy)
🐋 **SPDR:** {spdr['tonnes']} tấn (H.nay: {spdr['change']} tấn)
-----------------------------
_Bot tự động check rủi ro mỗi phút_
        """
        
        # Cơ chế update tin nhắn cũ để không spam
        try:
            with open(MSG_ID_FILE, "r") as f:
                saved_id = int(f.read().strip())
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=saved_id, text=dashboard_msg, parse_mode='Markdown')
        except:
            # Nếu không tìm thấy tin cũ hoặc lỗi, gửi tin mới và ghim lại
            m = await bot.send_message(chat_id=CHAT_ID, text=dashboard_msg, parse_mode='Markdown')
            with open(MSG_ID_FILE, "w") as f:
                f.write(str(m.message_id))
            try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=m.message_id)
            except: pass

    except Exception as e:
        print(f"Lỗi hệ thống: {e}")

# --- SERVER ---
@app.route('/')
def home(): return "Bot Market Watch đang chạy!", 200

@app.route('/run_check')
def run_check():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(logic_check_market())
        loop.close()
        return "Checked", 200
    except Exception as e: return str(e), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
