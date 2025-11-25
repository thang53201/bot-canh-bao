from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO
    "GOLD_H1_LIMIT": 30.0,
    "RSI_HIGH": 80, 
    "RSI_LOW": 20,
    "VIX_LIMIT": 30,
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM TẠO "MẶT NẠ" (RANDOM HEADERS) - ĐỂ TRÁNH BỊ YAHOO CHẶN
# ==============================================================================
def get_random_header():
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
    ]
    return {
        "User-Agent": random.choice(user_agents),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

# ==============================================================================
# 3. NGUỒN VÀNG BẤT TỬ: BINANCE API (PAXG/USDT)
# ==============================================================================
def get_gold_binance():
    """
    Lấy giá Vàng từ Binance. Nguồn này KHÔNG BAO GIỜ BỊ CHẶN.
    PAX Gold (PAXG) là token neo giá vàng thật, biến động y hệt XAUUSD.
    """
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT"
        r = requests.get(url, timeout=5)
        data = r.json()
        
        current = float(data['lastPrice'])
        change = float(data['priceChange'])
        pct = float(data['priceChangePercent'])
        
        # Tính RSI & H1 sơ bộ từ Klines (Nến)
        # Lấy 15 cây nến H1 gần nhất
        k_url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=15"
        k_r = requests.get(k_url, timeout=5)
        k_data = k_r.json()
        
        # k_data: [time, open, high, low, close, ...]
        closes = [float(x[4]) for x in k_data]
        
        # Tính H1 Range (Cây nến đang chạy - cây cuối cùng)
        last_candle = k_data[-1]
        h1_high = float(last_candle[2])
        h1_low = float(last_candle[3])
        h1_range = h1_high - h1_low
        
        # Tính RSI 14
        if len(closes) >= 15:
            prices = pd.Series(closes)
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1])
        else:
            current_rsi = 50.0

        return {
            'p': current, 'c': change, 'pct': pct,
            'h1': h1_range, 'rsi': current_rsi,
            'src': 'Binance (Ổn định)'
        }
    except Exception as e:
        print(f"Binance Error: {e}")
        return None

# ==============================================================================
# 4. NGUỒN YAHOO (CHO VIX, YIELD) - CÓ CƠ CHẾ THỬ LẠI
# ==============================================================================
def get_yahoo_simple(symbol):
    """Lấy giá hiện tại từ Yahoo API JSON"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
        r = requests.get(url, headers=get_random_header(), timeout=5)
        data = r.json()
        meta = data['chart']['result'][0]['meta']
        
        current = meta['regularMarketPrice']
        prev = meta['chartPreviousClose']
        
        change = current - prev
        pct = (change / prev * 100)
        
        return current, change, pct
    except:
        return 0.0, 0.0, 0.0

# ==============================================================================
# 5. SPDR (CÀO CSV GỐC)
# ==============================================================================
def get_spdr():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers=get_random_header(), timeout=10, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    curr = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return curr, curr - prev
        return 0.0, 0.0
    except: return 0.0, 0.0

# ==============================================================================
# 6. LOGIC TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_data_final():
    d = {}
    
    # 1. GOLD: Ưu tiên Binance (Vì Yahoo hay chặn)
    gold_data = get_gold_binance()
    if gold_data:
        d['gold'] = gold_data
    else:
        # Dự phòng nếu Binance sập (hiếm), thử quay lại Yahoo
        p, c, pct = get_yahoo_simple("GC=F")
        d['gold'] = {'p': p, 'c': c, 'pct': pct, 'h1': 0, 'rsi': 50, 'src': 'Yahoo (Backup)'}

    # 2. VIX & GVZ (Lấy Yahoo)
    p, _, pct = get_yahoo_simple("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    
    p, _, pct = get_yahoo_simple("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    # 3. LẠM PHÁT / YIELD
    # Thử lấy Breakeven trước
    p10, c10, _ = get_yahoo_simple("^T10YIE")
    p05, c05, _ = get_yahoo_simple("^T5YIE")
    
    if p10 == 0: # Nếu Yahoo chặn Breakeven
        d['be_name'] = "US Yields (Lợi suất)"
        p10, c10, _ = get_yahoo_simple("^TNX")
        p05, c05, _ = get_yahoo_simple("^FVX")
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # 4. SPDR
    v, c = get_spdr()
    d['spdr'] = {'v': v, 'c': c}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 7. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V20 - Binance Gold Core"

@app.route('/run_check')
def run_check():
    d = get_data_final()
    alerts = []
    now = time.time()
    
    # --- CẢNH BÁO ---
    # 1. Vàng (Từ Binance)
    if d['gold']['rsi'] > CONFIG['RSI_HIGH'] and d['gold']['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['gold']['rsi']:.0f} + H1 chạy {d['gold']['h1']:.1f}$")
            last_alert_times['RSI'] = now
            
    if d['gold']['rsi'] < CONFIG['RSI_LOW'] and d['gold']['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['gold']['rsi']:.0f} + H1 sập {d['gold']['h1']:.1f}$")
            last_alert_times['RSI'] = now

    if d['gold']['h1'] > CONFIG['GOLD_H1_LIMIT']:
        if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {d['gold']['h1']:.1f} giá")
            last_alert_times['H1'] = now

    # 2. Vĩ mô
    if d['vix']['p'] > CONFIG['VIX_LIMIT']:
         if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {d['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if abs(d['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
        if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(d['be10']['c']):.3f} điểm")
            last_alert_times['BE'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert Sent"

    # --- DASHBOARD 30 PHÚT ---
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        spdr_str = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chờ cập nhật"
        spdr_chg = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""
        
        # Xử lý hiển thị VIX/Yield nếu bị chặn (về 0)
        vix_str = f"{d['vix']['p']:.2f}" if d['vix']['p'] > 0 else "N/A"
        be10_str = f"{d['be10']['p']:.2f}%" if d['be10']['p'] > 0 else "N/A"
        be05_str = f"{d['be05']['p']:.2f}%" if d['be05']['p'] > 0 else "N/A"

        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"Nguồn Vàng: {d['gold']['src']}\n"
            f"-------------------------------\n"
            f"🥇 <b>GOLD (PAXG):</b> {d['gold']['p']:.1f}\n"
            f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {d['gold']['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {spdr_str} {spdr_chg}\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>{d['be_name']}:</b>\n"
            f"   • 10Y: {be10_str} (Chg: {s(d['be10']['c'])}{d['be10']['c']:.3f})\n"
            f"   • 05Y: {be05_str} (Chg: {s(d['be05']['c'])}{d['be05']['c']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {vix_str} | 🌪 <b>GVZ:</b> {d['gvz']['p']:.2f}\n"
        )
        send_tele(msg)
        return "Report Sent"

    return "Checked", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
