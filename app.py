from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "INF_10Y_LIMIT": 0.25, 
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600
}

# Cache
GLOBAL_CACHE = {
    'gold': {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'},
    'vix': {'p': 0, 'c': 0, 'pct': 0},
    'gvz': {'p': 0, 'c': 0, 'pct': 0},
    'inf10': {'p': 0, 'c': 0}, 
    'inf05': {'p': 0, 'c': 0}, 
    'fed': {'p': 0, 'pct': 0, 'name': 'Yield 13W'},
    'spdr': {'v': 0, 'c': 0},
    'be_source': 'Chờ...',
    'last_success_time': 0,
    'last_dashboard_time': 0
}

last_alert_times = {}

def get_vn_time(): return datetime.utcnow() + timedelta(hours=7)

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ==============================================================================
# 2. HÀM LẤY DỮ LIỆU TỪ CNBC (NGUỒN MỚI - NHẸ HƠN YAHOO)
# ==============================================================================
def get_cnbc_quote(symbols):
    """
    Lấy dữ liệu từ API ẩn của CNBC. Trả về danh sách JSON.
    XAU= : Gold Spot
    .VIX : VIX Index
    US10Y: Yield 10 Năm
    """
    try:
        # URL API cực nhanh của CNBC
        url = f"https://quote.cnbc.com/quote-html-webservice/quote.htm?partnerId=2&requestMethod=quick&exthrs=1&noform=1&fund=1&output=json&symbols={symbols}"
        r = requests.get(url, timeout=5)
        if r.status_code != 200: return None
        
        data = r.json()
        if 'QuickQuoteResult' not in data: return None
        
        return data['QuickQuoteResult']['QuickQuote']
    except: return None

# ==============================================================================
# 3. LẤY VÀNG (ƯU TIÊN CNBC SPOT -> BACKUP BINANCE)
# ==============================================================================
def get_gold_final():
    # 1. Thử CNBC (Spot Gold - Giá sát Exness)
    try:
        data = get_cnbc_quote("XAU=") # XAU= là mã Vàng Spot trên CNBC
        if data:
            item = data[0] if isinstance(data, list) else data
            price = float(item['last'])
            change = float(item['change'])
            pct = float(item['change_pct'].replace('%',''))
            
            # CNBC không có nến H1, ta dùng Binance để lấy RSI/H1 (vì Binance có nến chuẩn)
            # Lấy RSI từ Binance để ghép vào
            binance_tech = get_gold_binance_tech_only()
            
            return {
                'p': price, 'c': change, 'pct': pct,
                'h1': binance_tech['h1'], 'rsi': binance_tech['rsi'],
                'src': 'CNBC (Spot)'
            }
    except: pass

    # 2. Nếu CNBC lỗi -> Dùng Binance (Giá lệch nhưng sống dai)
    return get_gold_binance_full()

def get_gold_binance_tech_only():
    """Chỉ lấy RSI và H1 từ Binance"""
    try:
        k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=5).json()
        closes = [float(x[4]) for x in k]
        if len(closes) >= 15:
            delta = pd.Series(closes).diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            last = k[-1]
            h1 = float(last[2]) - float(last[3])
            return {'h1': h1, 'rsi': float(rsi.iloc[-1])}
    except: return {'h1': 0, 'rsi': 50}

def get_gold_binance_full():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=5).json()
        tech = get_gold_binance_tech_only()
        return {'p': float(r['lastPrice']), 'c': float(r['priceChange']), 'pct': float(r['priceChangePercent']), 'h1': tech['h1'], 'rsi': tech['rsi'], 'src': 'Binance (Backup)'}
    except: return None

# ==============================================================================
# 4. VĨ MÔ (CNBC + FRED)
# ==============================================================================
def get_fred_data(sid):
    try:
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        df = pd.read_csv(io.StringIO(r.text))
        df = df[df[sid] != '.']
        df[sid] = pd.to_numeric(df[sid])
        if len(df) >= 2: return float(df.iloc[-1][sid]), float(df.iloc[-1][sid]) - float(df.iloc[-2][sid])
    except: return None

def get_spdr_smart():
    try:
        r = requests.get("https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv", headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        df = pd.read_csv(io.StringIO(r.text), skiprows=6)
        col = [c for c in df.columns if "Tonnes" in str(c)]
        if col:
            df = df.dropna(subset=[col[0]])
            return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
    except: return None

def update_macro_data():
    global GLOBAL_CACHE
    current_time = time.time()
    
    # 5 phút cập nhật 1 lần
    if current_time - GLOBAL_CACHE['last_success_time'] < 300: return

    # 1. Lấy CNBC (VIX, Yield)
    try:
        data = get_cnbc_quote(".VIX,US10Y,US5Y,US3M") # US3M là 3 tháng (Fed Proxy)
        
        for item in data:
            s = item['symbol']
            try:
                p = float(item['last'])
                c = float(item['change'])
                pct = float(item['change_pct'].replace('%',''))
                
                if s == '.VIX': GLOBAL_CACHE['vix'] = {'p': p, 'c': c, 'pct': pct}
                # Dùng Yield làm dự phòng cho Lạm phát nếu FRED lỗi
                if s == 'US10Y': 
                    # Chỉ lưu tạm, ưu tiên FRED bên dưới
                    pass 
                if s == 'US3M': # Fed Proxy chuẩn
                    GLOBAL_CACHE['fed'] = {'p': p, 'pct': pct, 'name': 'Yield 3M (Fed Proxy)'}
            except: pass
    except: pass
    
    # 2. GVZ (Yahoo - Chỉ lấy mỗi cái này từ Yahoo thôi nên khó bị chặn)
    # ... (Bỏ qua để code nhẹ, GVZ ít quan trọng hơn VIX)
    
    # 3. SPDR
    spdr = get_spdr_smart()
    if spdr: GLOBAL_CACHE['spdr'] = {'v': spdr[0], 'c': spdr[1]}
    
    # 4. LẠM PHÁT (FRED - Chuẩn nhất)
    inf10 = get_fred_data("T10YIE")
    if inf10:
        GLOBAL_CACHE['be_source'] = "Lạm phát (FRED)"
        GLOBAL_CACHE['inf10'] = {'p': inf10[0], 'c': inf10[1]}
    
    inf05 = get_fred_data("T5YIE")
    if inf05: GLOBAL_CACHE['inf05'] = {'p': inf05[0], 'c': inf05[1]}
    
    GLOBAL_CACHE['last_success_time'] = current_time

def get_data_final():
    # Vàng: Lấy mỗi phút
    gold = get_gold_final()
    if not gold: 
        if GLOBAL_CACHE['gold']['p'] > 0:
            gold = GLOBAL_CACHE['gold']
            gold['src'] = "Mất Net (Giá cũ)"
        else:
            gold = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50, 'src': 'Khởi động...'}
    else:
        GLOBAL_CACHE['gold'] = gold

    # Macro: Lấy mỗi 5 phút
    try: update_macro_data()
    except: pass
    
    return GLOBAL_CACHE['gold'], GLOBAL_CACHE

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V69 - CNBC Source"

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
        
        # CẢNH BÁO
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
                    alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {gold['h1']:.1f} giá")
                    last_alert_times['H1'] = now
        
        if macro['vix']['p'] > CONFIG['VIX_VAL_LIMIT']:
             if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {macro['vix']['p']:.2f}")
                last_alert_times['VIX'] = now

        if abs(macro['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - last_alert_times.get('INF10', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(macro['inf10']['c']):.3f} điểm")
                last_alert_times['INF10'] = now

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

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"Nguồn Vàng: {gold['src']}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (XAU/USD):</b> {gold_p}\n"
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
                f"📉 <b>VIX:</b> {fmt(macro['vix']['p'], macro['vix']['c'], macro['vix']['pct'])}\n"
            )
            send_tele(msg)
            GLOBAL_CACHE['last_dashboard_time'] = now
            return "Report Sent", 200

        return "Checked", 200
    except: return "Err", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
