import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
import json
import os
from flask import Flask
from datetime import datetime, timedelta
import pytz

# --- CẤU HÌNH (ĐIỀN LẠI THÔNG TIN CỦA BẠN) ---
TOKEN = '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo' 
CHAT_ID = '5464507208'                    

app = Flask(__name__)

# --- CẤU HÌNH NGƯỠNG CẢNH BÁO ---
THRESHOLDS = {
    'VIX_HIGH': 30,             # VIX trên 30
    'VIX_CHANGE_PCT': 15.0,     # VIX tăng 15% trong ngày
    'GVZ_HIGH': 25,             # GVZ trên 25
    'GVZ_CHANGE_PCT': 10.0,     # GVZ tăng 10% trong ngày
    'US10Y_CHANGE': 0.25,       # Yield 10Y biến động 0.25 điểm
    'US02Y_CHANGE': 0.20,       # Yield 02Y biến động 0.20 điểm
    'SPDR_CHANGE_TONS': 5.0,    # SPDR mua/bán 5 tấn
    'GOLD_H1_MOVE': 40.0,       # Nến H1 chạy 40 giá (400 pips)
}

# Từ khóa tin tức nhạy cảm (Cảnh báo biến động mạnh)
NEWS_KEYWORDS = ["war", "nuclear", "attack", "cpi", "nfp", "fed rate", "powell", "inflation", "escalation"]

# Tên file lưu trạng thái
STATE_FILE = "bot_state.json"

# --- QUẢN LÝ TRẠNG THÁI (TRÁNH SPAM) ---
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except: pass
    return {
        "msg_id": None,
        "last_dash_time": 0,
        "date_str": "",
        "alerts_triggered": {
            "vix_high": False, "vix_jump": False,
            "gvz_high": False, "gvz_jump": False,
            "us10y": False, "us02y": False,
            "spdr": False, "news": [] # Lưu các tin đã báo để ko báo lại
        }
    }

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Lỗi lưu state: {e}")

# --- HÀM HỖ TRỢ LẤY DATA ---
def get_gold_spot_price():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?region=US&lang=en-US&interval=1m&range=1h"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        result = data['chart']['result'][0]['indicators']['quote'][0]['close']
        price = next((x for x in reversed(result) if x is not None), 0)
        return round(price, 2)
    except: return 0.0

def get_spdr_data():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        s = requests.get(url, timeout=5).content
        df = pd.read_csv(io.StringIO(s.decode('utf-8')), skiprows=6)
        df = df[['Date', 'Total Net Asset Value Tonnes in the Trust']].dropna().tail(5)
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        tonnes = float(last['Total Net Asset Value Tonnes in the Trust'])
        change = tonnes - float(prev['Total Net Asset Value Tonnes in the Trust'])
        
        # Check streak (3 ngày cùng chiều)
        diffs = df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3)
        is_buy_streak = all(x > 0 for x in diffs.dropna())
        is_sell_streak = all(x < 0 for x in diffs.dropna())
        
        return {'tonnes': tonnes, 'change': change, 'streak_buy': is_buy_streak, 'streak_sell': is_sell_streak}
    except:
        return {'tonnes': 0, 'change': 0, 'streak_buy': False, 'streak_sell': False}

def check_sensitive_news(triggered_news_list):
    """Kiểm tra tin tức từ Yahoo Finance xem có tin sốc không"""
    alerts = []
    new_triggered = triggered_news_list.copy()
    try:
        ticker = yf.Ticker("GC=F")
        news = ticker.news
        for item in news:
            title = item.get('title', '').lower()
            link = item.get('link', '')
            uuid = item.get('uuid', title) # Dùng title làm ID nếu ko có uuid
            
            if uuid in triggered_triggered: continue # Tin này đã báo rồi

            for kw in NEWS_KEYWORDS:
                if kw in title:
                    alerts.append(f"📰 **TIN NÓNG:** {item['title']} \n(Nguy cơ biến động mạnh!)")
                    new_triggered.append(uuid)
                    break
    except: pass
    return alerts, new_triggered[-20:] # Chỉ giữ lại 20 tin gần nhất để tiết kiệm bộ nhớ

# --- LOGIC CHÍNH ---
async def logic_check_market():
    loop = asyncio.get_event_loop()
    bot = telegram.Bot(token=TOKEN)
    
    # 1. Load State & Reset flags nếu sang ngày mới
    state = load_state()
    vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(vn_tz)
    today_str = now_vn.strftime('%Y-%m-%d')
    
    if state['date_str'] != today_str:
        # Reset flags cho ngày mới
        state['date_str'] = today_str
        state['alerts_triggered'] = {k: False if k != 'news' else [] for k, v in state['alerts_triggered'].items()}
        state['alerts_triggered']['news'] = []

    alerts = []
    
    try:
        # 2. Lấy dữ liệu
        gold_now = get_gold_spot_price()
        
        # Lấy data Daily cho VIX, Yields
        tickers = ["^VIX", "^GVZ", "^TNX", "^IRX", "GC=F"]
        data = await loop.run_in_executor(None, lambda: yf.download(tickers, period="2d", interval="1d", progress=False))
        
        # Lấy data H1 cho Gold để check nến
        data_h1 = await loop.run_in_executor(None, lambda: yf.download("GC=F", period="1d", interval="60m", progress=False))

        def get_stat(ticker):
            try:
                s = data['Close'][ticker].dropna()
                if len(s) < 2: return 0, 0, 0
                curr, prev = s.iloc[-1], s.iloc[-2]
                chg = curr - prev
                pct = (chg/prev)*100 if prev else 0
                return round(curr, 2), round(chg, 2), round(pct, 2)
            except: return 0, 0, 0

        vix_val, vix_chg, vix_pct = get_stat("^VIX")
        gvz_val, gvz_chg, gvz_pct = get_stat("^GVZ")
        us10_val, us10_chg, us10_pct = get_stat("^TNX")
        us02_val, us02_chg, us02_pct = get_stat("^IRX")
        gold_d_val, gold_d_chg, gold_d_pct = get_stat("GC=F")

        if gold_now == 0: gold_now = gold_d_val # Fallback

        # --- KIỂM TRA ĐIỀU KIỆN CẢNH BÁO (ALERTS) ---
        
        # 1. VIX
        if vix_val > THRESHOLDS['VIX_HIGH'] and not state['alerts_triggered']['vix_high']:
            alerts.append(f"🔴 **VIX NGUY HIỂM:** Đã vượt mức {THRESHOLDS['VIX_HIGH']} (Hiện tại: {vix_val})")
            state['alerts_triggered']['vix_high'] = True
            
        if vix_pct >= THRESHOLDS['VIX_CHANGE_PCT'] and not state['alerts_triggered']['vix_jump']:
            alerts.append(f"⚠️ **VIX BÙNG NỔ:** Tăng +{vix_pct}% trong ngày")
            state['alerts_triggered']['vix_jump'] = True

        # 2. GVZ (Gold Volatility)
        if gvz_val > THRESHOLDS['GVZ_HIGH'] and not state['alerts_triggered']['gvz_high']:
            alerts.append(f"🌪 **BÃO VÀNG:** GVZ vượt {THRESHOLDS['GVZ_HIGH']} (Biến động cực mạnh)")
            state['alerts_triggered']['gvz_high'] = True
            
        if (gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT'] or gvz_val > 25) and not state['alerts_triggered']['gvz_jump']:
             # Logic gộp: Tăng 10% hoặc > 25 đều báo khẩn 1 lần
             if gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT']:
                 alerts.append(f"⚠️ **GVZ TĂNG SỐC:** +{gvz_pct}%")
             state['alerts_triggered']['gvz_jump'] = True

        # 3. Yields (US10Y, US02Y)
        if abs(us10_chg) >= THRESHOLDS['US10Y_CHANGE'] and not state['alerts_triggered']['us10y']:
            trend = "TĂNG" if us10_chg > 0 else "GIẢM"
            alerts.append(f"🇺🇸 **US10Y {trend} MẠNH:** {abs(us10_chg)} điểm (Nến D1)")
            state['alerts_triggered']['us10y'] = True

        if abs(us02_chg) >= THRESHOLDS['US02Y_CHANGE'] and not state['alerts_triggered']['us02y']:
            trend = "TĂNG" if us02_chg > 0 else "GIẢM"
            alerts.append(f"🏦 **US02Y {trend} MẠNH:** {abs(us02_chg)} điểm (Kỳ vọng lãi suất thay đổi)")
            state['alerts_triggered']['us02y'] = True

        # 4. SPDR (Cá mập)
        spdr = get_spdr_data()
        # Chỉ báo nếu có thay đổi mới so với lần check trước (hoặc dùng logic flag đơn giản trong ngày)
        # Ở đây dùng flag trong ngày: nếu hôm nay đã báo rồi thì thôi, trừ khi số lượng thay đổi
        if (abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS'] or spdr['streak_buy'] or spdr['streak_sell']) and not state['alerts_triggered']['spdr']:
            if abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS']:
                action = "MUA GOM" if spdr['change'] > 0 else "XẢ HÀNG"
                alerts.append(f"🐋 **SPDR {action}:** {abs(spdr['change'])} tấn")
            
            if spdr['streak_buy']: alerts.append("🐋 **SPDR:** Mua ròng 3 ngày liên tiếp!")
            if spdr['streak_sell']: alerts.append("🐋 **SPDR:** Bán ròng 3 ngày liên tiếp!")
            
            state['alerts_triggered']['spdr'] = True

        # 5. Gold H1 Candle (Realtime - Luôn cảnh báo nếu mới xảy ra)
        if not data_h1.empty:
            last_h1 = data_h1.iloc[-1]
            # Kiểm tra nến hiện tại (đang chạy) và nến trước đó
            h1_range = last_h1['High'] - last_h1['Low']
            if h1_range >= THRESHOLDS['GOLD_H1_MOVE']:
                # Lưu ý: check nến H1 cần thận trọng kẻo spam mỗi phút. 
                # Ta chỉ báo vào phút đóng nến hoặc chấp nhận báo lặp lại trong 1 tiếng đó nhưng có kèm thời gian
                # Ở đây mình chọn cách báo kèm thời gian check, user tự lọc
                alerts.append(f"🔥 **H1 BIẾN ĐỘNG:** Nến hiện tại chạy {h1_range:.1f}$ ({int(h1_range*10)} pips)")

        # 6. Tin tức & FedWatch (Thay thế bằng News Sentiment)
        # Logic: Check tin tức, nếu có từ khóa thì báo
        # Phần FedWatch > 15% rất khó lấy chính xác nếu ko có API, nên dùng tin tức để cover.
        news_alerts, updated_news_list = check_sensitive_news(state['alerts_triggered'].get('news', []))
        if news_alerts:
            alerts.extend(news_alerts)
            state['alerts_triggered']['news'] = updated_news_list

        # --- GỬI CẢNH BÁO NGAY LẬP TỨC ---
        if alerts:
            msg_text = "🚨 **CẢNH BÁO KHẨN CẤP** 🚨\n\n" + "\n".join(alerts)
            await bot.send_message(chat_id=CHAT_ID, text=msg_text, parse_mode='Markdown')

        # --- DASHBOARD ĐỊNH KỲ (30 PHÚT/LẦN HOẶC KHI CÓ ALERT) ---
        last_dash = datetime.fromtimestamp(state['last_dash_time'], tz=vn_tz)
        diff_mins = (now_vn - last_dash).total_seconds() / 60
        
        # Chỉ gửi Dashboard nếu có Alerts hoặc đã quá 30 phút
        if alerts or diff_mins >= 30:
            time_str = now_vn.strftime('%H:%M %d/%m')
            gold_icon = '📈' if gold_d_chg > 0 else '📉'
            vix_icon = '🟢' if vix_pct < 0 else ('🔴' if vix_val > 30 else '🟡')
            
            dashboard = f"""
📊 **MARKET MONITOR** ({time_str})
-----------------------------
🥇 **Gold:** {gold_now} ({gold_icon} {gold_d_chg}$)
🌊 **GVZ:** {gvz_val} ({gvz_pct}%)
{vix_icon} **VIX:** {vix_val} ({vix_pct}%)
🇺🇸 **US10Y:** {us10_val}% (Var: {us10_chg})
🏦 **US02Y:** {us02_val}% (Var: {us02_chg})
🐋 **SPDR:** {spdr['tonnes']} tấn ({spdr['change']:+.2f})
-----------------------------
_Cập nhật mỗi 30p hoặc khi có biến động mạnh_
            """
            
            # Logic xóa/sửa tin nhắn cũ
            try:
                if state['msg_id']:
                    await bot.delete_message(chat_id=CHAT_ID, message_id=state['msg_id'])
            except: pass # Bỏ qua nếu ko xóa được (do tin quá cũ hoặc đã bị xóa)
            
            # Gửi tin mới và Pin
            sent_msg = await bot.send_message(chat_id=CHAT_ID, text=dashboard, parse_mode='Markdown')
            try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=sent_msg.message_id)
            except: pass
            
            # Cập nhật State
            state['msg_id'] = sent_msg.message_id
            state['last_dash_time'] = now_vn.timestamp()

        # Lưu lại state cuối cùng
        save_state(state)

    except Exception as e:
        print(f"Error: {e}")

# --- SERVER ---
@app.route('/')
def home(): return "Bot Active", 200

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
