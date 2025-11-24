import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
import time
from flask import Flask
from datetime import datetime, timedelta
import pytz

# --- CẤU HÌNH (ĐIỀN LẠI THÔNG TIN CỦA BẠN VÀO ĐÂY) ---
TOKEN = '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo'  # <-- NHỚ ĐIỀN LẠI TOKEN CỦA BẠN
CHAT_ID = '5464507208'                    # <-- NHỚ ĐIỀN LẠI CHAT ID CỦA BẠN

app = Flask(__name__)

# --- CẤU HÌNH NGƯỠNG CẢNH BÁO ---
THRESHOLDS = {
    'VIX_DANGER': 30,           
    'VIX_CHANGE_PCT': 10.0,     # Chỉ báo khi VIX TĂNG >= 10%
    'GVZ_DANGER': 25,           
    'GVZ_CHANGE_PCT': 15.0,     # GVZ Tăng >= 15%
    'US10Y_CHANGE': 0.25,       
    'US02Y_CHANGE': 0.2,        # US02Y biến động 0.2 điểm
    'GOLD_MOVE_DOLLARS': 50.0,  # Vàng chạy 50$ (500 pips)
    'GOLD_H1_MOVE_DOLLARS': 40.0, # Vàng nến H1 > 40$ (400 pips)
    'SPDR_CHANGE_TONS': 5.0,    
}

# File tạm để lưu trạng thái
MSG_ID_FILE = "msg_id.txt"
LAST_DASHBOARD_TIME_FILE = "last_dash_time.txt"

# --- HÀM HỖ TRỢ LẤY GIÁ VÀNG SPOT (MỚI) ---
def get_gold_spot_price():
    """Lấy giá Vàng Spot (XAUUSD) từ nguồn bên ngoài để đảm bảo độ chính xác"""
    try:
        # Sử dụng API của Yahoo Finance cho XAUUSD=X (Spot Gold)
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?region=US&lang=en-US&interval=1m&range=1h"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status() # Báo lỗi nếu status code không phải 200
        
        data = response.json()
        
        if 'result' in data['chart'] and data['chart']['result']:
            price_list = data['chart']['result'][0]['indicators']['quote'][0]['close']
            if price_list and price_list[-1] is not None:
                return round(price_list[-1], 2)
        return 0.0
    except Exception as e:
        print(f"Lỗi khi lấy giá Gold Spot từ API: {e}")
        return 0.0

# --- HÀM HỖ TRỢ THỜI GIAN ---
def get_last_dash_time():
    """Lấy thời điểm gửi dashboard lần cuối (dùng cho logic 30 phút)"""
    try:
        with open(LAST_DASHBOARD_TIME_FILE, "r") as f:
            timestamp = float(f.read().strip())
            return datetime.fromtimestamp(timestamp, tz=pytz.utc)
    except:
        return datetime.min.replace(tzinfo=pytz.utc)

def save_last_dash_time(dt_obj):
    """Lưu thời điểm gửi dashboard mới nhất"""
    with open(LAST_DASHBOARD_TIME_FILE, "w") as f:
        f.write(str(dt_obj.timestamp()))

# --- HÀM LẤY DỮ LIỆU ---
def get_spdr_data():
    """Đọc dữ liệu trực tiếp từ file CSV của quỹ SPDR"""
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        s = requests.get(url, timeout=5).content
        df = pd.read_csv(io.StringIO(s.decode('utf-8')), skiprows=6)
        df = df[['Date', 'Total Net Asset Value Tonnes in the Trust']].dropna().tail(5)
        
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        tonnes_now = float(last_row['Total Net Asset Value Tonnes in the Trust'])
        change = tonnes_now - float(prev_row['Total Net Asset Value Tonnes in the Trust'])
        
        diffs = df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        streak_buy = all(diffs == 1)
        streak_sell = all(diffs == -1)
        
        return {'tonnes': tonnes_now, 'change': change, 'streak_buy': streak_buy, 'streak_sell': streak_sell}
    except Exception as e:
        print(f"Lỗi SPDR: {e}")
        return {'tonnes': 0, 'change': 0, 'streak_buy': False, 'streak_sell': False}

async def logic_check_market():
    # SỬ DỤNG asyncio.get_event_loop() ĐỂ TRÁNH LỖI 'RuntimeError: Event loop is closed'
    loop = asyncio.get_event_loop()
    bot = telegram.Bot(token=TOKEN)
    alerts = [] 
    now_utc = datetime.now(pytz.utc)

    try:
        # 1. Lấy dữ liệu Vàng mới nhất (Spot) VÀ VIX, GVZ, Yields (Daily)
        
        # --- FIX LỖI GIÁ VÀNG $0 ---
        gold_price_latest = get_gold_spot_price() 
        
        tickers_daily = ["GC=F", "^GVZ", "^VIX", "^TNX", "^IRX"] 
        data_daily = await loop.run_in_executor(None, lambda: yf.download(tickers_daily, period="2d", interval="1d", progress=False))
        
        # H1 Gold data
        gold_data_h1 = await loop.run_in_executor(None, lambda: yf.download("GC=F", period="2d", interval="60m", progress=False))
        
        # Lấy giá trị hiện tại (Latest) và Đóng cửa hôm qua (Prev)
        def get_val(ticker, data):
            try:
                closes = data['Close'][ticker].dropna()
                if len(closes) < 2: return 0, 0, 0
                curr = closes.iloc[-1]
                prev = closes.iloc[-2]
                chg = curr - prev
                pct = (chg / prev) * 100 if prev != 0 else 0
                return round(curr, 2), round(chg, 2), round(pct, 2)
            except: return 0, 0, 0

        # --- TÍNH TOÁN CÁC CHỈ SỐ ---
        
        # Giá đóng cửa Gold hôm qua và thay đổi (dùng GC=F daily)
        gold_close, gold_chg, gold_pct = get_val("GC=F", data_daily) 
        
        # SỬ DỤNG GIÁ TỨC THỜI MỚI (gold_price_latest) cho Dashboard
        gold_price = gold_price_latest if gold_price_latest != 0.0 else gold_close

        # 2. Các chỉ số khác
        gvz_val, gvz_chg, gvz_pct = get_val("^GVZ", data_daily)
        vix_val, vix_chg, vix_pct = get_val("^VIX", data_daily)
        us10y_val, us10y_chg, us10y_pct = get_val("^TNX", data_daily)
        us02y_val, us02y_chg, us02y_pct = get_val("^IRX", data_daily)

        # --- LOGIC CẢNH BÁO (RUNG CHUÔNG) ---
        
        # 1. VIX 
        if vix_val >= THRESHOLDS['VIX_DANGER']:
            alerts.append(f"🔴 **NGUY HIỂM:** VIX đạt {vix_val} (Rủi ro cao)")
        if vix_pct >= THRESHOLDS['VIX_CHANGE_PCT']: 
            alerts.append(f"⚠️ **VIX BÙNG NỔ:** Tăng +{vix_pct}%")

        # 2. GVZ 
        if gvz_val >= THRESHOLDS['GVZ_DANGER']:
            alerts.append(f"🌪 **BÃO VÀNG:** GVZ đạt {gvz_val}")
        if gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT']: 
            alerts.append(f"⚠️ **GVZ TĂNG MẠNH:** +{gvz_pct}% (Đạt 15%)")

        # 3. US10Y
        if abs(us10y_chg) >= THRESHOLDS['US10Y_CHANGE']:
            trend = "TĂNG" if us10y_chg > 0 else "GIẢM"
            alerts.append(f"🇺🇸 **US10Y:** {trend} {abs(us10y_chg)} điểm")

        # 4. US02Y 
        if abs(us02y_chg) >= THRESHOLDS['US02Y_CHANGE']:
            trend = "TĂNG" if us02y_chg > 0 else "GIẢM"
            alerts.append(f"🏦 **LÃI SUẤT US02Y:** {trend} {abs(us02y_chg)} điểm")

        # 5. SPDR GOLD TRUST
        spdr = get_spdr_data()
        if abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS']:
            action = "MUA GOM" if spdr['change'] > 0 else "XẢ HÀNG"
            alerts.append(f"🐋 **SPDR {action}:** {abs(round(spdr['change'], 2))} tấn")
        if spdr['streak_buy']: alerts.append("🐋 **SPDR:** Mua ròng 3 ngày liên tiếp")
        if spdr['streak_sell']: alerts.append("🐋 **SPDR:** Xả ròng 3 ngày liên tiếp")

        # 6. GOLD PRICE DAY CHANGE (500 pips)
        if abs(gold_chg) >= THRESHOLDS['GOLD_MOVE_DOLLARS']:
            pips = int(abs(gold_chg) * 10) 
            alerts.append(f"💰 **VÀNG BIẾN ĐỘNG:** {gold_chg}$ (~{pips} pips)")

        # 7. GOLD H1 CANDLE (400 pips)
        if not gold_data_h1.empty and len(gold_data_h1) >= 2:
            last_candle = gold_data_h1.iloc[-2] # Nến H1 hoàn thành gần nhất
            h1_range = round(last_candle['High'] - last_candle['Low'], 2)
            if h1_range >= THRESHOLDS['GOLD_H1_MOVE_DOLLARS']:
                pips_h1 = int(h1_range * 10)
                alerts.append(f"🔥 **H1 NẾN VÀNG:** {h1_range}$ ({pips_h1} pips). Tín hiệu hành động mạnh!")
            
        # 3. GỬI CẢNH BÁO TỨC THỜI (REALTIME)
        if alerts:
            msg_alert = "\n".join(alerts)
            await bot.send_message(chat_id=CHAT_ID, text=msg_alert, parse_mode='Markdown')


        # --- CẬP NHẬT DASHBOARD (LOGIC 30 PHÚT) ---
        
        last_dash_time = get_last_dash_time()
        needs_dash_update = (now_utc - last_dash_time).total_seconds() >= 1800 # 30 phút = 1800 giây

        if alerts or needs_dash_update:
            
            vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
            time_str = datetime.now(vn_tz).strftime('%H:%M %d/%m')
            
            # Icon trạng thái
            vix_icon = '🟢' if vix_pct < 0 else ('🔴' if vix_pct > 5 else '🟡')
            gold_icon = '📈' if gold_chg > 0 else '📉'
            
            dashboard_msg = f"""
📊 **MARKET MONITOR** ({time_str})
-----------------------------
🥇 **Gold:** {gold_price} ({gold_icon} {gold_chg}$)
🌊 **GVZ:** {gvz_val} ({gvz_pct}%)
{vix_icon} **VIX:** {vix_val} ({vix_pct}%)
🇺🇸 **US10Y:** {us10y_val}% (Var: {us10y_chg})
🏦 **US02Y:** {us02y_val}% (Var: {us02y_chg})
🐋 **SPDR:** {spdr['tonnes']} tấn ({spdr['change']:+.2f})
-----------------------------
_Cảnh báo chỉ rung chuông khi có biến động lớn_
            """
            
            # Cơ chế update tin nhắn cũ để không spam
            try:
                with open(MSG_ID_FILE, "r") as f:
                    saved_id = int(f.read().strip())
                await bot.edit_message_text(chat_id=CHAT_ID, message_id=saved_id, text=dashboard_msg, parse_mode='Markdown')
            except:
                m = await bot.send_message(chat_id=CHAT_ID, text=dashboard_msg, parse_mode='Markdown')
                with open(MSG_ID_FILE, "w") as f:
                    f.write(str(m.message_id))
                try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=m.message_id)
                except: pass
                
            save_last_dash_time(now_utc) # Lưu lại thời điểm update cuối

    except Exception as e:
        print(f"Lỗi hệ thống: {e}")

# --- SERVER ---
@app.route('/')
def home(): return "Bot OK", 200

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
