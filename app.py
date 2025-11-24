import os
import telegram
import asyncio
import yfinance as yf
from flask import Flask
from threading import Thread

# --- CẤU HÌNH (Điền thông tin của bạn vào đây) ---
TOKEN = 'ĐIỀN_TOKEN_CỦA_BẠN_VÀO_ĐÂY'
CHAT_ID = 'ĐIỀN_CHAT_ID_CỦA_BẠN_VÀO_ĐÂY'
# Mẹo: Chat với @userinfobot để lấy CHAT_ID, chat với @BotFather để lấy TOKEN

app = Flask(__name__)
bot = telegram.Bot(token=TOKEN)

# Biến toàn cục để lưu ID tin nhắn Dashboard
dashboard_msg_id = None

THRESHOLDS = {
    'GVZ_LEVEL': 25, 'GVZ_CHANGE_PCT': 10.0,
    'VIX_LEVEL': 30, 'VIX_CHANGE_PCT': 8.0,
    'US10Y_CHANGE': 0.25
}

async def logic_check_market():
    global dashboard_msg_id
    try:
        # 1. Lấy dữ liệu
        tickers = ["^GVZ", "^VIX", "^TNX"]
        data = yf.download(tickers, period="2d", interval="1d", progress=False)
        
        alerts = []
        market_info = {}
        
        # 2. Xử lý dữ liệu
        for ticker in tickers:
            name = ticker.replace("^", "")
            try:
                # Lấy giá đóng cửa 2 ngày gần nhất
                closes = data['Close'][ticker].dropna().tail(2)
                if len(closes) < 2: continue
                
                curr = closes.iloc[-1]
                prev = closes.iloc[-2]
                change = curr - prev
                pct = (change / prev) * 100
                
                market_info[name] = {'val': round(curr, 2), 'pct': round(pct, 2), 'chg': round(change, 2)}
                
                # Logic Cảnh báo
                if name == "GVZ":
                    if curr > THRESHOLDS['GVZ_LEVEL']: alerts.append(f"⚠️ GVZ cao: {curr}")
                    if abs(pct) > THRESHOLDS['GVZ_CHANGE_PCT']: alerts.append(f"⚠️ GVZ biến động: {pct}%")
                elif name == "VIX":
                    if curr > THRESHOLDS['VIX_LEVEL']: alerts.append(f"⚠️ VIX cao: {curr}")
                    if abs(pct) > THRESHOLDS['VIX_CHANGE_PCT']: alerts.append(f"⚠️ VIX biến động: {pct}%")
                elif name == "TNX": # US10Y
                    if abs(change) >= THRESHOLDS['US10Y_CHANGE']: alerts.append(f"⚠️ US10Y đổi chiều: {change} điểm")
            except Exception as e:
                print(f"Lỗi ticker {ticker}: {e}")

        # 3. Gửi cảnh báo (nếu có)
        if alerts:
            await bot.send_message(chat_id=CHAT_ID, text="\n".join(alerts))

        # 4. Update Dashboard
        # Giả lập số liệu SPDR/FedWatch (cần logic scrape riêng nếu muốn chính xác)
        msg = f"""
📊 **MARKET WATCH**
--------------------
🔹 GVZ: {market_info.get('GVZ', {}).get('val')} ({market_info.get('GVZ', {}).get('pct')}%)
🔹 VIX: {market_info.get('VIX', {}).get('val')} ({market_info.get('VIX', {}).get('pct')}%)
🔹 US10Y: {market_info.get('TNX', {}).get('val')}%
--------------------
_Check lúc: {import_time_string()}_
        """
        
        if dashboard_msg_id:
            try:
                await bot.edit_message_text(chat_id=CHAT_ID, message_id=dashboard_msg_id, text=msg, parse_mode='Markdown')
            except:
                # Nếu không edit được (do cũ quá), gửi mới
                m = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                dashboard_msg_id = m.message_id
        else:
            m = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            dashboard_msg_id = m.message_id
            try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=dashboard_msg_id)
            except: pass
            
    except Exception as e:
        print(f"Lỗi logic: {e}")

def import_time_string():
    from datetime import datetime
    import pytz
    vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    return datetime.now(vn_tz).strftime('%H:%M %d/%m')

# --- WEB SERVER ---
@app.route('/')
def home():
    return "Bot đang chạy!"

@app.route('/run_check')
def run_check():
    # Đây là link để Cron-job gọi vào mỗi phút
    asyncio.run(logic_check_market())
    return "Đã check market", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
