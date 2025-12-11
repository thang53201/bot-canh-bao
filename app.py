from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz
from dateutil import parser

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (V105 - FULL ALERT + NEWS)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    "TWELVE_DATA_KEY": "3d1252ab61b947bda28b0e532947ae34", 
    
    # CẢNH BÁO VÀNG
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    
    # CẢNH BÁO BIẾN ĐỘNG (1 NGÀY)
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "MOVE_PCT_LIMIT": 5.0,
    
    # CẢNH BÁO BIẾN ĐỘNG (ĐA NGÀY - TỪ V104)
    "MOVE_3D_LIMIT": 10.0, 
    "GVZ_2D_LIMIT": 10.0,
    
    "ALERT_COOLDOWN": 3600,
    "NEWS_CACHE_TIME": 3600, # 1 Tiếng
    "GOLD_CACHE_TIME": 120   # 2 Phút
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0, 'pct_2d': 0, 'pct_3d': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0, 'pct_2d': 0, 'pct_3d': 0},
    'move': {'p': 0, 'c': 0, 'pct': 0, 'pct_2d': 0, 'pct_3d': 0},
    'news': [],
    'last_success_time': 0,
    'last_gold_time': 0,
    'last_news_time': 0,
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
# 2. HÀM LẤY VÀNG (2 PHÚT/LẦN)
# ==============================================================================
def calculate_rsi_safe(prices, period=14):
    clean_prices = [p for p in prices if p > 0]
    if len(clean_prices) < period + 1: return 50.0
    series = pd.Series(clean_prices)
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    if avg_loss.iloc[-1] == 0: return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    result = float(rsi.iloc[-1])
    if result > 99 or result < 1: return 50.0
    return result

def get_gold_api():
    try:
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        d = r.json()
        if 'close' in d:
            url2 = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1h&outputsize=50&apikey={CONFIG['TWELVE_DATA_KEY']}"
            r2 = requests.get(url2, timeout=10)
            d2 = r2.json()
            h1_move = 0; rsi = 50
            if 'values' in d2:
                candles = d2['values']
                closes = [float(c['close']) for c in candles][::-1]
                rsi = calculate_rsi_safe(closes)
                current = candles[0]
                h1_move = float(current['high']) - float(current['low'])
            return {'p': float(d['close']), 'c': float(d['change']), 'pct': float(d['percent_change']), 'h1': h1_move, 'rsi': rsi, 'src': 'API Forex'}
    except: pass
    
    if GLOBAL_CACHE['gold']['p'] > 0:
        old = GLOBAL_CACHE['gold'].copy()
        old['src'] = "Mất kết nối (Dữ liệu cũ)"
        return old
    return {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Lỗi API'}

def update_gold_data():
    global GLOBAL_CACHE
    current_time = time.time()
    if current_time - GLOBAL_CACHE['last_gold_time'] < CONFIG['GOLD_CACHE_TIME']: return
    new_gold = get_gold_api()
    if new_gold['src'] != 'Lỗi API':
        GLOBAL_CACHE['gold'] = new_gold
        GLOBAL_CACHE['last_gold_time'] = current_time

# ==============================================================================
# 3. MACRO & TIN TỨC (ĐÃ SỬA LỖI HIỂN THỊ TIN TỨC)
# ==============================================================================
def get_ff_news():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.forexfactory.com/"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        upcoming = []
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        for item in data:
            if item['country'] == 'USD' and item['impact'] == 'High':
                try:
                    raw_date = item['date']
                    dt_str = raw_date.rsplit('-', 1)[0] if '-' in raw_date[-6:] else raw_date.rsplit('+', 1)[0]
                    news_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
                    offset_str = raw_date[-6:]
                    sign = 1 if offset_str[0] == '+' else -1
                    hours = int(offset_str[1:3]); minutes = int(offset_str[4:6])
                    offset_delta = timedelta(hours=hours, minutes=minutes) * sign
                    news_utc = (news_dt - offset_delta).replace(tzinfo=pytz.utc)
                    time_diff = (news_utc - now_utc).total_seconds()
                    
                    # Lấy tin trong 36h tới (1.5 ngày)
                    if -3600 < time_diff < 129600:
                        news_vn = news_utc + timedelta(hours=7)
                        day_str = news_vn.strftime('%d/%m')
                        time_str = news_vn.strftime('%H:%M')
                        upcoming.append(f"• {day_str} <b>{time_str}:</b> {item['title']}")
                except: continue
        return upcoming[:5]
    except: return []

def get_yahoo_data(symbol):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        
        if len(closes) < 2: return None
        
        cur = closes[-1]; prev = closes[-2]
        change = cur - prev
        pct = (change / prev) * 100
        
        pct_2d = ((cur - closes[-3]) / closes[-3]) * 100 if len(closes) >= 3 else 0.0
        pct_3d = ((cur - closes[-4]) / closes[-4]) * 100 if len(closes) >= 4 else 0.0
            
        return {'p': cur, 'c': change, 'pct': pct, 'pct_2d': pct_2d, 'pct_3d': pct_3d}
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # Tin tức: Cập nhật mỗi 1 tiếng
    if current_time - GLOBAL_CACHE['last_news_time'] > CONFIG['NEWS_CACHE_TIME']:
        news = get_ff_news()
        if news: GLOBAL_CACHE['news'] = news
        GLOBAL_CACHE['last_news_time'] = current_time

    # Vĩ mô: Cập nhật mỗi 5 phút
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = res
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = res
    res = get_yahoo_data("^MOVE")
    if res: GLOBAL_CACHE['move'] = res
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    update_gold_data()
    try: update_macro_data()
    except: pass
    return GLOBAL_CACHE['gold'], GLOBAL_CACHE

# ==============================================================================
# 4. ROUTING & RUN
# ==============================================================================
@app.route('/')
def home(): return "Bot V105 - News Restored"

@app.route('/test')
def run_test():
    gold, _ = get_data_final()
    send_tele(f"🔔 TEST OK. Gold: {gold['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        gold, macro = get_data_final()
        alerts = []
        now = time.time()
        
        # 1. CẢNH BÁO VÀNG
        if gold['p'] > 0:
            if gold['rsi'] > CONFIG['RSI_HIGH'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {gold['rsi']:.1f} + H1 chạy {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if gold['rsi'] < CONFIG['RSI_LOW'] and gold['h1'] > CONFIG['RSI_PRICE_MOVE']:
                if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {gold['rsi']:.1f} + H1 sập {gold['h1']:.1f}$")
                    last_alert_times['RSI'] = now
            if abs(gold['h1']) > CONFIG['GOLD_H1_LIMIT']:
                if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 biến động {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now

        # 2. CẢNH BÁO BIẾN ĐỘNG (1 NGÀY)
        if macro['move']['pct'] > CONFIG['MOVE_PCT_LIMIT']:
             if now - last_alert_times.get('MOVE_1D', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌋 <b>MOVE SỐC (1D):</b> +{macro['move']['pct']:.2f}%")
                last_alert_times['MOVE_1D'] = now
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or macro['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if macro['gvz']['p'] > CONFIG['GVZ_VAL_LIMIT'] or macro['gvz']['pct'] > CONFIG['GVZ_PCT_LIMIT']:
             if now - last_alert_times.get('GVZ_1D', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BÁO ĐỘNG (1D):</b> {macro['gvz']['p']:.2f}")
                last_alert_times['GVZ_1D'] = now

        # 3. CẢNH BÁO BIẾN ĐỘNG MỚI (ĐA NGÀY - TỪ V104)
        if macro['move']['pct_3d'] > CONFIG['MOVE_3D_LIMIT']:
             if now - last_alert_times.get('MOVE_3D', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌋 <b>MOVE BÃO LỚN (3D):</b> Tăng {macro['move']['pct_3d']:.2f}% trong 3 ngày qua!")
                last_alert_times['MOVE_3D'] = now
        
        if macro['gvz']['pct_2d'] > CONFIG['GVZ_2D_LIMIT']:
             if now - last_alert_times.get('GVZ_2D', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🌪 <b>GVZ BẤT ỔN (2D):</b> Tăng {macro['gvz']['pct_2d']:.2f}% trong 2 ngày qua!")
                last_alert_times['GVZ_2D'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # DASHBOARD
        vn_now = get_vn_time()
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        last_sent = GLOBAL_CACHE.get('last_dashboard_time', 0)
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            gold_p = f"{gold['p']:.1f}" if gold['p'] > 0 else "N/A"
            rsi_val = f"{gold['rsi']:.1f}" if gold['rsi'] > 0 else "N/A"
            
            # --- PHẦN HIỂN THỊ TIN TỨC (ĐÃ SỬA LỖI) ---
            news_section = ""
            if macro['news']:
                news_txt = "\n".join(macro['news'])
                news_section = f"📰 <b>TIN ĐỎ USD (36H):</b>\n{news_txt}\n-------------------------------\n"
            # ------------------------------------------

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"{news_section}"
                f"🥇 <b>GOLD ({gold['src']}):</b> {gold_p}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {rsi_val}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk Sentiment (Nỗi sợ):</b>\n"
                f"   • VIX (CK): {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ (Vàng): {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
                f"   • MOVE (Bond): {fmt(macro['move']['p'], macro['move']['c'], macro['move']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
