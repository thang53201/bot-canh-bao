import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
import json
import os
import traceback
from flask import Flask
from datetime import datetime
import pytz

# --- CẤU HÌNH (ĐIỀN CHÍNH XÁC THÔNG TIN CỦA BẠN) ---
TOKEN = '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo' 
CHAT_ID = '5464507208'                    

app = Flask(__name__)

# --- CẤU HÌNH NGƯỠNG CẢNH BÁO ---
THRESHOLDS = {
    'VIX_HIGH': 30,             
    'VIX_CHANGE_PCT': 15.0,     
    'GVZ_HIGH': 25,             
    'GVZ_CHANGE_PCT': 10.0,     
    'US10Y_CHANGE': 0.25,       
    'US02Y_CHANGE': 0.20,       
    'SPDR_CHANGE_TONS': 5.0,    
    'GOLD_H1_MOVE': 40.0,       
}

NEWS_KEYWORDS = ["war", "nuclear", "attack", "cpi", "nfp", "fed rate", "powell", "inflation", "escalation"]
STATE_FILE = "bot_state.json"

# --- HÀM QUẢN LÝ TRẠNG THÁI ---
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
            "spdr": False, "news": []
        }
    }

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Lỗi lưu state: {e}")

# --- HÀM LẤY DATA (ĐÃ FIX LỖI ÉP KIỂU FLOAT) ---
def get_gold_spot_price():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?region=US&lang=en-US&interval=1m&range=1h"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        result = data['chart']['result'][0]['indicators']['quote'][0]['close']
        price = next((x for x in reversed(result) if x is not None), 0)
        return float(price) # Ép kiểu số thực
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
        
        diffs = df['Total Net Asset Value Tonnes in the Trust'].diff().tail(3)
        is_buy_streak = all(x > 0 for x in diffs.dropna())
        is_sell_streak = all(x < 0 for x in diffs.dropna())
        
        return {'tonnes': tonnes, 'change': change, 'streak_buy': is_buy_streak, 'streak_sell': is_sell_streak}
    except:
        return {'tonnes': 0.0, 'change': 0.0, 'streak_buy': False, 'streak_sell': False}

def check_sensitive_news(triggered_news_list):
    alerts = []
    new_triggered = triggered_news_list.copy()
    try:
        ticker = yf.Ticker("GC=F")
        news = ticker.news
        for item in news:
            title = item.get('title', '').lower()
            uuid = item.get('uuid', title)
            if uuid in triggered_news_list: continue
            
            for kw in NEWS_KEYWORDS:
                if kw in title:
                    alerts.append(f"📰 **TIN NÓNG:** {item['title']}")
                    new_triggered.append(uuid)
                    break
    except: pass
    return alerts, new_triggered[-20:]

# --- LOGIC CHÍNH ---
async def logic_check_market():
    loop = asyncio.get_event_loop()
    bot = telegram.Bot(token=TOKEN)
    
    state = load_state()
    vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(vn_tz)
    today_str = now_vn.strftime('%Y-%m-%d')
    
    # Reset ngày mới
    if state['date_str'] != today_str:
        state['date_str'] = today_str
        state['alerts_triggered'] = {k: False if k != 'news' else [] for k, v in state['alerts_triggered'].items()}
        state['alerts_triggered']['news'] = []

    alerts = []
    
    # Lấy data
    gold_now = get_gold_spot_price()
    tickers = ["^VIX", "^GVZ", "^TNX", "^IRX", "GC=F"]
    
    # Fix lỗi yfinance trả về MultiIndex hoặc Series
    data = await loop.run_in_executor(None, lambda: yf.download(tickers, period="2d", interval="1d", progress=False, auto_adjust=True))
    data_h1 = await loop.run_in_executor(None, lambda: yf.download("GC=F", period="1d", interval="60m", progress=False, auto_adjust=True))

    # Hàm lấy dữ liệu an toàn (Fix lỗi Ambiguous)
    def get_stat(ticker):
        try:
            # Lấy cột Close của ticker đó
            if isinstance(data.columns, pd.MultiIndex):
                s = data['Close'][ticker].dropna()
            else:
                s = data['Close'].dropna() # Trường hợp chỉ 1 ticker (ít xảy ra ở đây)

            if len(s) < 2: return 0.0, 0.0, 0.0
            
            # ÉP KIỂU SANG FLOAT ĐỂ TRÁNH LỖI SERIES
            curr = float(s.iloc[-1])
            prev = float(s.iloc[-2])
            
            chg = curr - prev
            pct = (chg/prev)*100 if prev != 0 else 0.0
            
            return round(curr, 2), round(chg, 2), round(pct, 2)
        except Exception as e:
            # print(f"Lỗi lấy stat {ticker}: {e}")
            return 0.0, 0.0, 0.0

    vix_val, vix_chg, vix_pct = get_stat("^VIX")
    gvz_val, gvz_chg, gvz_pct = get_stat("^GVZ")
    us10_val, us10_chg, us10_pct = get_stat("^TNX")
    us02_val, us02_chg, us02_pct = get_stat("^IRX")
    gold_d_val, gold_d_chg, gold_d_pct = get_stat("GC=F")
    
    if gold_now == 0: gold_now = gold_d_val

    # --- CHECK ALERT ---
    # So sánh số thực (float) sẽ không còn bị lỗi
    if vix_val > THRESHOLDS['VIX_HIGH'] and not state['alerts_triggered']['vix_high']:
        alerts.append(f"🔴 **VIX NGUY HIỂM:** {vix_val}")
        state['alerts_triggered']['vix_high'] = True
        
    if vix_pct >= THRESHOLDS['VIX_CHANGE_PCT'] and not state['alerts_triggered']['vix_jump']:
        alerts.append(f"⚠️ **VIX BÙNG NỔ:** +{vix_pct}%")
        state['alerts_triggered']['vix_jump'] = True

    if (gvz_pct >= THRESHOLDS['GVZ_CHANGE_PCT'] or gvz_val > THRESHOLDS['GVZ_HIGH']) and not state['alerts_triggered']['gvz_jump']:
         alerts.append(f"⚠️ **GVZ TĂNG MẠNH:** +{gvz_pct}% (Val: {gvz_val})")
         state['alerts_triggered']['gvz_jump'] = True

    if abs(us10_chg) >= THRESHOLDS['US10Y_CHANGE'] and not state['alerts_triggered']['us10y']:
        alerts.append(f"🇺🇸 **US10Y BIẾN ĐỘNG:** {us10_chg:+.2f} điểm")
        state['alerts_triggered']['us10y'] = True

    if abs(us02_chg) >= THRESHOLDS['US02Y_CHANGE'] and not state['alerts_triggered']['us02y']:
        alerts.append(f"🏦 **US02Y BIẾN ĐỘNG:** {us02_chg:+.2f} điểm")
        state['alerts_triggered']['us02y'] = True

    spdr = get_spdr_data()
    if (abs(spdr['change']) >= THRESHOLDS['SPDR_CHANGE_TONS'] or spdr['streak_buy'] or spdr['streak_sell']) and not state['alerts_triggered']['spdr']:
        alerts.append(f"🐋 **SPDR:** {spdr['change']:+.2f} tấn")
        state['alerts_triggered']['spdr'] = True

    if not data_h1.empty:
        try:
            # Xử lý an toàn cho Data H1
            if isinstance(data_h1.columns, pd.MultiIndex):
                # Nếu có multiindex (thường yfinance mới hay trả về dạng ('Close', 'GC=F'))
                try:
                    last_h1 = data_h1.iloc[-1]
                    high = float(last_h1['High']['GC=F']) if 'GC=F' in last_h1['High'] else float(last_h1['High'])
                    low = float(last_h1['Low']['GC=F']) if 'GC=F' in last_h1['Low'] else float(last_h1['Low'])
                except:
                    # Fallback đơn giản
                    high = float(data_h1['High'].iloc[-1])
                    low = float(data_h1['Low'].iloc[-1])
            else:
                high = float(data_h1['High'].iloc[-1])
                low = float(data_h1['Low'].iloc[-1])

            h1_range = high - low
            if h1_range >= THRESHOLDS['GOLD_H1_MOVE']:
                alerts.append(f"🔥 **H1 BIẾN ĐỘNG:** {h1_range:.1f}$")
        except Exception as e:
            print(f"Lỗi check H1: {e}")

    news_alerts, updated_news = check_sensitive_news(state['alerts_triggered']['news'])
    if news_alerts:
        alerts.extend(news_alerts)
        state['alerts_triggered']['news'] = updated_news

    # Gửi Alert ngay
    if alerts:
        await bot.send_message(chat_id=CHAT_ID, text="🚨 **CẢNH BÁO:**\n" + "\n".join(alerts), parse_mode='Markdown')

    # --- DASHBOARD ---
    last_dash = datetime.fromtimestamp(state['last_dash_time'], tz=vn_tz)
    diff_mins = (now_vn - last_dash).total_seconds() / 60
    
    if alerts or diff_mins >= 30:
        time_str = now_vn.strftime('%H:%M %d/%m')
        gold_icon = '📈' if gold_d_chg > 0 else '📉'
        
        dashboard = f"""
📊 **MARKET MONITOR** ({time_str})
-----------------------------
🥇 **Gold:** {gold_now} ({gold_icon} {gold_d_chg}$)
🌊 **GVZ:** {gvz_val} ({gvz_pct}%)
☢️ **VIX:** {vix_val} ({vix_pct}%)
🇺🇸 **US10Y:** {us10_val}% (Var: {us10_chg})
🏦 **US02Y:** {us02_val}% (Var: {us02_chg})
🐋 **SPDR:** {spdr['tonnes']} tấn ({spdr['change']:+.2f})
-----------------------------
_Auto check 1 min_
        """
        
        try:
            if state['msg_id']:
                await bot.delete_message(chat_id=CHAT_ID, message_id=state['msg_id'])
        except: pass
        
        sent = await bot.send_message(chat_id=CHAT_ID, text=dashboard, parse_mode='Markdown')
        try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=sent.message_id)
        except: pass
        
        state['msg_id'] = sent.message_id
        state['last_dash_time'] = now_vn.timestamp()

    save_state(state)

# --- SERVER & ROUTES ---
@app.route('/')
def home(): return "Bot Live", 200

@app.route('/test')
def test_bot():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot = telegram.Bot(token=TOKEN)
        m = loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text="✅ **TEST:** Bot kết nối thành công!"))
        loop.close()
        return f"OK! Đã gửi tin nhắn ID: {m.message_id}", 200
    except Exception as e:
        return f"❌ LỖI KẾT NỐI: {str(e)}", 500

@app.route('/run_check')
def run_check():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(logic_check_market())
        loop.close()
        return "Checked", 200
    except Exception as e:
        error_msg = str(e)
        print(f"ERROR in run_check: {traceback.format_exc()}")
        return f"Error: {error_msg}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
