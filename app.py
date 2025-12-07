from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz
import json

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # CẢNH BÁO VÀNG
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # CẢNH BÁO VĨ MÔ
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "MOVE_PCT_LIMIT": 5.0,
    
    "ALERT_COOLDOWN": 3600,
    
    # CẤU HÌNH TIN TỨC
    "NEWS_CACHE_TIME": 14400 # 4 Tiếng cập nhật lịch tin 1 lần (Siêu an toàn)
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'move': {'p': 0, 'c': 0, 'pct': 0},
    'news': [], # Cache tin tức
    'last_success_time': 0,
    'last_news_time': 0, # Thời gian cập nhật tin cuối cùng
    'last_dashboard_time': 0
}

last_alert_times = {}

def get_vn_time(): return datetime.utcnow() + timedelta(hours=7)

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================================
# 2. HÀM LẤY TIN TỨC FOREXFACTORY (JSON BACKDOOR)
# ==============================================================================
def get_forex_news():
    """
    Lấy lịch tin từ nguồn JSON của FairEconomy (ForexFactory).
    Lọc: USD + High Impact + Sắp diễn ra.
    """
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.forexfactory.com/"
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        
        upcoming = []
        # Lấy thời gian hiện tại theo UTC (để so sánh với file json)
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        
        for item in data:
            # 1. Lọc USD và Tin Đỏ (High)
            if item['country'] == 'USD' and item['impact'] == 'High':
                try:
                    # 2. Xử lý thời gian (Format: 2025-11-26T10:00:00-05:00)
                    # Dùng cách cắt chuỗi cơ bản để không cần thư viện dateutil nặng nề
                    raw_date = item['date']
                    # Cắt lấy phần ngày giờ cơ bản: 2025-11-26T10:00:00
                    dt_str = raw_date.rsplit('-', 1)[0] if '-' in raw_date[-6:] else raw_date.rsplit('+', 1)[0]
                    news_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
                    
                    # Giả định offset từ chuỗi gốc (thường là -04:00 hoặc -05:00)
                    # Để đơn giản và an toàn, ta convert sang VN luôn
                    # FF JSON thường là giờ New York. Ta cứ +12 tiếng là ra giờ VN xấp xỉ, 
                    # hoặc chuẩn hơn là parse timezone.
                    # Cách chuẩn nhất không cần thư viện ngoài:
                    # Lấy offset từ chuỗi cuối (ví dụ -05:00)
                    offset_str = raw_date[-6:]
                    sign = 1 if offset_str[0] == '+' else -1
                    hours = int(offset_str[1:3])
                    minutes = int(offset_str[4:6])
                    offset_delta = timedelta(hours=hours, minutes=minutes) * sign
                    
                    # Convert về UTC
                    news_utc = news_dt - offset_delta
                    news_utc = news_utc.replace(tzinfo=pytz.utc)
                    
                    # 3. Chỉ lấy tin TƯƠNG LAI (Trong vòng 24h tới)
                    # Hoặc tin vừa ra cách đây 1 tiếng (để biết lý do chạy)
                    time_diff = (news_utc - now_utc).total_seconds()
                    
                    if -3600 < time_diff < 86400: # Từ -1h đến +24h
                        # Convert ra giờ Việt Nam (UTC+7)
                        news_vn = news_utc + timedelta(hours=7)
                        time_str = news_vn.strftime('%H:%M') # Chỉ lấy giờ phút
                        title = item['title']
                        upcoming.append(f"• <b>{time_str}:</b> {title}")
                        
                except: continue
                
        # Sắp xếp và lấy 3 tin gần nhất
        return upcoming[:5]
    except Exception as e:
        print(f"News Error: {e}")
        return []

# ==============================================================================
# 3. HÀM LẤY VÀNG (TWELVE DATA)
# ==============================================================================
def calculate_rsi(prices, periods=14):
    if len(prices) < periods + 1: return 50
    delta = pd.Series(prices).diff()
    gain = (delta.where(delta > 0, 0)).rolling(periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def get_gold_api():
    try:
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        d = r.json()
        if 'close' in d:
            url2 = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1h&outputsize=20&apikey={CONFIG['TWELVE_DATA_KEY']}"
            r2 = requests.get(url2, timeout=10)
            d2 = r2.json()
            h1_move = 0; rsi = 50
            if 'values' in d2:
                candles = d2['values']
                closes = [float(c['close']) for c in candles][::-1]
                rsi = calculate_rsi(closes)
                current = candles[0]
                h1_move = float(current['high']) - float(current['low'])

            return {'p': float(d['close']), 'c': float(d['change']), 'pct': float(d['percent_change']), 'h1': h1_move, 'rsi': rsi, 'src': 'API Forex'}
    except: pass
    
    if GLOBAL_CACHE['gold']['p'] > 0:
        old = GLOBAL_CACHE['gold'].copy()
        old['src'] = "Mất kết nối (Giá cũ)"
        return old
    return {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Lỗi API'}

# ==============================================================================
# 4. MACRO (YAHOO)
# ==============================================================================
def get_yahoo_data(symbol):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            cur = closes[-1]; prev = closes[-2]
            return cur, cur - prev, (cur - prev)/prev*100
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 1. CẬP NHẬT TIN TỨC (4 TIẾNG/LẦN)
    if current_time - GLOBAL_CACHE['last_news_time'] > CONFIG['NEWS_CACHE_TIME']:
        news_list = get_forex_news()
        if news_list: GLOBAL_CACHE['news'] = news_list
        GLOBAL_CACHE['last_news_time'] = current_time

    # 2. CẬP NHẬT CHỈ SỐ (5 PHÚT/LẦN)
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^MOVE")
    if res: GLOBAL_CACHE['move'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    gold = get_gold_api()
    GLOBAL_CACHE['gold'] = gold
    try: update_macro_data()
    except: pass
    return gold, GLOBAL_CACHE

# ==============================================================================
# 5. ROUTING & RUN
# ==============================================================================
@app.route('/')
def home(): return "Bot V97 - News Alert"

@app.route('/test')
def run_test():
    gold, _ = get_data_final()
    send_tele(f"🔔 TEST OK. Gold: {gold['p']} ({gold['src']})")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO ---
        if gold['p'] > 0:
            if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {gold['rsi']:.0f} + H1 chạy {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {gold['rsi']:.0f} + H1 sập {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if gold['h1'] > CONFIG['GOLD_H1_LIMIT']:
                if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 biến động {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now

        if macro['move']['pct'] > CONFIG['MOVE_PCT_LIMIT']:
             if now - last_alert_times.get('MOVE', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌋 <b>MOVE SỐC:</b> +{macro['move']['pct']:.2f}%")
                last_alert_times['MOVE'] = now
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG:</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD ---
        vn_now = get_vn_time()
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            gold_p = f"{gold['p']:.1f}" if gold['p'] > 0 else "N/A"
            
            # Format tin tức (Nếu có)
            news_section = ""
            if macro['news']:
                news_txt = "\n".join(macro['news'])
                news_section = f"📰 <b>TIN ĐỎ USD (24H):</b>\n{news_txt}\n-------------------------------\n"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"{news_section}"
                f"🥇 <b>GOLD (XAU/USD):</b> {gold_p}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk Sentiment:</b>\n"
                f"   • VIX: {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ: {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
                f"   • MOVE: {fmt(macro['move']['p'], macro['move']['c'], macro['move']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
