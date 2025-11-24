import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
from flask import Flask
from datetime import datetime
import pytz

# --- CẤU HÌNH (ĐIỀN LẠI TOKEN VÀ ID CỦA BẠN) ---
TOKEN = '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo'  # <-- NHỚ ĐIỀN LẠI TOKEN
CHAT_ID = '5464507208'                    # <-- NHỚ ĐIỀN LẠI CHAT ID

app = Flask(__name__)

# --- CẤU HÌNH NGƯỠNG CẢNH BÁO MỚI ---
THRESHOLDS = {
    'VIX_DANGER': 30,           # VIX >= 30 (Sợ hãi cực độ)
    'VIX_CHANGE_PCT': 10.0,     # Chỉ báo khi VIX TĂNG >= 10% (Giảm không báo)
    'GVZ_DANGER': 25,           # GVZ >= 25
    'GVZ_CHANGE_PCT': 10.0,     # GVZ Tăng >= 10%
    'US10Y_CHANGE': 0.25,       # Yield biến động 0.25 điểm
    'GOLD_MOVE_DOLLARS': 50.0,  # Vàng chạy 50$ = 500 pips (Mới sửa)
    'SPDR_CHANGE_TONS': 5.0,    # Quỹ mua/bán > 5 tấn
}

MSG_ID_FILE = "msg_id.txt"

# --- HÀM LẤY DỮ LIỆU ---
def get_spdr_data():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        s = requests.get(url, timeout=5).content
        df = pd.read_csv(io.StringIO(s.decode('utf-8')), skiprows=6)
        df = df[['Date', 'Total Net Asset Value Tonnes in the Trust']].dropna().tail(5)
        
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        tonnes_now = float(last_row['Total Net Asset Value Tonnes in the Trust'])
        change = tonnes_now - float(prev_row['Total Net Asset Value Tonnes in the Trust'])
        
        # Check chuỗi 3 ngày
        diffs = df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3)
        streak_buy = all(diffs > 0)
        streak_sell = all(diffs < 0)
        
        return {'tonnes': tonnes_now, 'change': change, 'streak_buy': streak_buy, 'streak_sell': streak_sell}
    except:
        return {'tonnes': 0, 'change': 0, 'streak_buy': False, 'streak_sell': False}

async def logic_check_market():
    bot = telegram.Bot(token=TOKEN)
    alerts = [] 
    
    try:
        tickers = ["GC=F", "^GVZ", "^VIX", "^TNX", "^IRX"] 
        data = yf.download(tickers, period="2d", interval="1d", progress=False)
        
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

        # --- LOGIC CẢNH BÁO (ĐÃ SỬA) ---
        
        # 1. VIX (Chỉ báo khi TĂNG > 10% hoặc Mức > 30)
        if vix_val >= THRESHOLDS['VIX_DANGER']:
            alerts.append(f"🔴 **NGUY HIỂM:** VIX đạt {vix_val} (Rủi ro cao)")
        if vix_pct >= THRESHOLDS['VIX_CHANGE_PCT']: # Bỏ abs(), chỉ lấy số dương
            alerts.append(f"⚠️ **VIX BÙNG NỔ:** Tăng +{vix_pct}%")

        # 2. GVZ (Chỉ báo khi TĂNG hoặc mức cao)
        if gvz_val >= THRESHOLDS['GVZ_DANGER']:
            alerts.append(f"🌪 **BÃO VÀNG:** GVZ đạt {gvz_val}")
        if gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT']:
            alerts.append(f"⚠️ **GVZ TĂNG MẠNH:** +{gvz_pct}%")

        # 3. US10Y (Giữ nguyên)
        if abs(us10y_chg) >= THRESHOLDS['US10Y_CHANGE']:
            trend = "TĂNG" if us10y_chg > 0 else "GIẢM"
            alerts.append(f"🇺🇸 **US10Y:** {trend} {abs(us10y_chg)} điểm")

        # 4. SPDR
        spdr = get_spdr_data()
        if abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS']:
            action = "MUA GOM" if spdr['change'] > 0 else "XẢ HÀNG"
            alerts.append(f"🐋 **SPDR {action}:** {abs(round(spdr['change'], 2))} tấn")

        # 5. VÀNG (Sửa thành 50$ = 500 pips)
        if abs(gold_chg) >= THRESHOLDS['GOLD_MOVE_DOLLARS']:
            pips = int(abs(gold_chg) * 10) # 1$ = 10 pips
            alerts.append(f"💰 **VÀNG BIẾN ĐỘNG:** {gold_chg}$ (~{pips} pips)")
            
        # 6. LÃI SUẤT 2 NĂM (US02Y)
        if abs(us02y_chg) >= 0.2:
            alerts.append(f"🏦 **LÃI SUẤT US02Y:** Biến động {us02y_chg}%")

        # --- GỬI CẢNH BÁO RIÊNG (NẾU CÓ) ---
        if alerts:
            await bot.send_message(chat_id=CHAT_ID, text="\n".join(alerts), parse_mode='Markdown')

        # --- CẬP NHẬT DASHBOARD (UPDATE IM LẶNG) ---
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
🐋 **SPDR:** {spdr['tonnes']} tấn ({spdr['change']:+.2f})
-----------------------------
_Vàng biến động >500 pips hoặc VIX tăng >10% mới báo_
        """
        
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

    except Exception as e:
        print(f"Error: {e}")

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
