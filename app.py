from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz
from dateutil import parser # Xử lý ngày tháng tin tức

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
    "INF_10Y_LIMIT": 0.25, "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600,
    "NEWS_CACHE_TIME": 14400 # 4 Tiếng mới cập nhật tin 1 lần (Siêu an toàn)
}

GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W'},
    'spdr': {'v': 0, 'c': 0},
    'news': [], # Lưu danh sách tin tức
    'be_source': 'Chờ...',
    'last_success_time': 0,
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
# 2. HÀM LẤY TIN TỨC (FOREXFACTORY JSON - 4 TIẾNG/LẦN)
# ==============================================================================
def get_ff_news():
    try:
        # Link JSON lịch tuần này (Nhẹ, không bị chặn)
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.forexfactory.com/"
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        
        upcoming_news = []
        now_utc = datetime.utcnow()
        
        for item in data:
            # Lọc: Chỉ lấy USD + High Impact (Tin đỏ)
            if item['country'] == 'USD' and item['impact'] == 'High':
                try:
                    # Parse thời gian tin ra
                    news_dt = parser.parse(item['date'])
                    news_utc = news_dt.astimezone(pytz.utc).replace(tzinfo=None)
                    
                    # Lấy tin trong vòng 24h tới (hoặc vừa qua 1h)
                    time_diff = (news_utc - now_utc).total_seconds()
                    if -3600 < time_diff < 86400:
                        # Chuyển sang giờ VN
                        news_vn = news_utc + timedelta(hours=7)
                        time_str = news_vn.strftime('%H:%M')
                        upcoming_news.append(f"• {time_str}: {item['title']}")
                except: continue
        
        return upcoming_news
    except Exception as e:
        print(f"News Err: {e}")
        return []

# ==============================================================================
# 3. HÀM LẤY VÀNG (TWELVE DATA + BINANCE)
# ==============================================================================
def calculate_rsi(prices, periods=14):
    if len(prices) < periods + 1: return 50
    delta = pd.Series(prices).diff()
    gain = (delta.where(delta > 0, 0)).rolling(periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def get_gold_api_full():
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1h&outputsize=20&apikey={CONFIG['TWELVE_DATA_KEY']}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if 'values' in data:
            candles = data['values']
            current = candles[0]
            price = float(current['close'])
            change = price - float(current['open'])
            percent = (change / float(current['open'])) * 100
            
            closes = [float(c['close']) for c in candles][::-1]
            rsi = calculate_rsi(closes)
            h1 = float(current['high']) - float(current['low'])
            return {'p': price, 'c': change, 'pct': percent, 'h1': h1, 'rsi': rsi, 'src': 'API Forex'}
    except: pass
    
    # Fallback Binance
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5).json()
        k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5).json()
        closes = [float(x[4]) for x in k]
        rsi = calculate_rsi(closes)
        last = k[-1]
        h1 = float(last[2]) - float(last[3])
        return {'p': float(r['lastPrice']), 'c': float(r['priceChange']), 'pct': float(r['priceChangePercent']), 'h1': h1, 'rsi': rsi, 'src': 'Binance (Backup)'}
    except: return None

# ==============================================================================
# 4. MACRO & SPDR
# ==============================================================================
def get_yahoo_data(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            return closes[-1], closes[-1]-closes[-2], (closes[-1]-closes[-2])/closes[-2]*100
    except: return None

def get_fred_data(sid):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[sid] != '.']
            df[sid] = pd.to_numeric(df[sid])
            if len(df) >= 2: return float(df.iloc[-1][sid]), float(df.iloc[-1][sid]) - float(df.iloc[-2][sid])
    except: return None

def get_spdr_smart():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 1. CẬP NHẬT TIN TỨC (4 TIẾNG/LẦN)
    if current_time - GLOBAL_CACHE['last_news_time'] > CONFIG['NEWS_CACHE_TIME']:
        news = get_ff_news()
        if news: GLOBAL_CACHE['news'] = news
        GLOBAL_CACHE['last_news_time'] = current_time

    # 2. CẬP NHẬT MACRO (5 PHÚT/LẦN)
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    res = get_yahoo_data("^VIX")
    if res: GLOBAL_CACHE['vix'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_yahoo_data("^GVZ")
    if res: GLOBAL_CACHE['gvz'] = {'p': res[0], 'c': res[1], 'pct': res[2]}
    res = get_spdr_smart()
    if res: GLOBAL_CACHE['spdr'] = {'v': res[0], 'c': res[1]}
    
    inf10 = get_fred_data("T10YIE")
    if inf10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
        GLOBAL_CACHE['inf10'] = {'p': inf10[0], 'c': inf10[1]}
    else:
        res10 = get_yahoo_data("^T10YIE")
        if res10:
            GLOBAL_CACHE['be_source'] = "Lạm phát (Yahoo)"
            GLOBAL_CACHE['inf10'] = {'p': res10[0], 'c': res10[1]}

    res05 = get_yahoo_data("^T5YIE")
    if res05: GLOBAL_CACHE['inf05'] = {'p': res05[0], 'c': res05[1]}
    else:
        fred05 = get_fred_data("T5YIE")
        if fred05: GLOBAL_CACHE['inf05'] = {'p': fred05[0], 'c': fred05[1]}

    res_fed = get_yahoo_data("^IRX")
    if res_fed: GLOBAL_CACHE['fed'] = {'p': res_fed[0], 'pct': res_fed[2], 'name': 'Yield 13W'}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    gold = get_gold_api_full() # Ưu tiên API Forex
    if not gold: 
        gold = get_gold_binance_full() # Fallback Binance
    
    if not gold:
        if GLOBAL_CACHE['gold']['p'] > 0: gold = GLOBAL_CACHE['gold']
        else: gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'}
    
    try: update_macro_data()
    except: pass
    
    GLOBAL_CACHE['gold'] = gold
    return gold, GLOBAL_CACHE

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V91 - News Hunter"

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
        
        # ALERT VÀNG
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
        
        # ALERT MACRO
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now
        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
             if now - last_alert_times.get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {macro['inf10']['c']:.3f} điểm")
                last_alert_times['INF'] = now
        if abs(macro['fed']['pct']) > CONFIG['FED_PCT_LIMIT']:
             if now - last_alert_times.get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED BIẾN ĐỘNG:</b> Đổi {macro['fed']['pct']:.1f}%")
                last_alert_times['FED'] = now

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
            
            spdr_txt = f"{macro['spdr']['v']:.2f} tấn" if macro['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(macro['spdr']['c'])}{macro['spdr']['c']:.2f})" if macro['spdr']['v'] > 0 else ""
            
            def fmt(val, chg, pct): return f"{val:.2f} ({s(pct)}{pct:.2f}%)" if val else "N/A"
            def fmt_pts(val, chg): return f"{val:.3f}% (Chg: {s(chg)}{chg:.3f})" if val else "N/A"

            gold_p = f"{gold['p']:.1f}" if gold['p'] > 0 else "N/A"
            
            # Format Tin tức
            news_section = ""
            if macro['news']:
                news_list = "\n".join(macro['news'])
                news_section = f"📰 <b>TIN ĐỎ USD (SẮP TỚI):</b>\n{news_list}\n-------------------------------\n"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"{news_section}"
                f"🥇 <b>GOLD ({gold['src']}):</b> {gold_p}\n"
                f"   {i(gold['c'])} {s(gold['c'])}{gold['c']:.1f}$ ({s(gold['pct'])}{gold['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {gold['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{macro['be_source']}:</b>\n"
                f"   • 10Y: {fmt_pts(macro['inf10']['p'], macro['inf10']['c'])}\n"
                f"   • 05Y: {fmt_pts(macro['inf05']['p'], macro['inf05']['c'])}\n"
                f"-------------------------------\n"
                f"🏦 <b>FedWatch ({macro['fed']['name']}):</b>\n"
                f"   • Mức: {fmt(macro['fed']['p'], 0, macro['fed']['pct'])}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
                f"   • GVZ: {fmt(macro['gvz']['p'], macro['gvz']['c'], macro['gvz']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e: 
        return f"Err: {e}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
