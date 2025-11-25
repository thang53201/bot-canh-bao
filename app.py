from flask import Flask
import requests
import pandas as pd
import io
import time
from datetime import datetime
import pytz
import json

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
# 2. HÀM GỌI API TRỰC TIẾP (CORE FUNCTION)
# ==============================================================================
def get_headers():
    """Giả lập header của Chrome để Yahoo tưởng là người dùng thật"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

def get_yahoo_json(symbol):
    """
    Lấy dữ liệu JSON trực tiếp từ Yahoo (Bỏ qua thư viện yfinance).
    Đây là cách duy nhất để không bị chặn IP trên Render.
    """
    try:
        # URL API nội bộ của Yahoo
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        
        # Gửi request trực tiếp
        response = requests.get(url, headers=get_headers(), timeout=10)
        data = response.json()
        
        # Bóc tách dữ liệu JSON
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = quote['close']
        
        # Lọc bỏ giá trị None (null)
        clean_closes = [c for c in closes if c is not None]
        
        if len(clean_closes) < 2:
            return 0.0, 0.0, 0.0
            
        current = float(clean_closes[-1])
        prev = float(clean_closes[-2])
        
        change = current - prev
        pct = (change / prev * 100) if prev != 0 else 0
        
        return current, change, pct
    except Exception as e:
        print(f"Lỗi JSON {symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_h1_json():
    """Lấy RSI và H1 Range qua JSON"""
    try:
        # Lấy dữ liệu 1 giờ (60m)
        url = "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=60m&range=5d"
        response = requests.get(url, headers=get_headers(), timeout=10)
        data = response.json()
        
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        closes = quote['close']
        highs = quote['high']
        lows = quote['low']
        
        # Làm sạch data
        clean_data = []
        for i in range(len(closes)):
            if closes[i] is not None and highs[i] is not None and lows[i] is not None:
                clean_data.append({
                    'close': closes[i],
                    'high': highs[i],
                    'low': lows[i]
                })
        
        if len(clean_data) < 15: return 0.0, 50.0
        
        # 1. H1 Range (Nến cuối)
        last = clean_data[-1]
        h1_range = last['high'] - last['low']
        
        # 2. RSI Thủ công
        prices = pd.Series([x['close'] for x in clean_data])
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        return h1_range, current_rsi
    except:
        return 0.0, 50.0

# ==============================================================================
# 3. SPDR (Vẫn giữ nguyên vì đã hoạt động tốt)
# ==============================================================================
def get_spdr_real():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers=get_headers(), timeout=15, verify=False)
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
# 4. TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_market_data():
    data = {}
    
    # 1. Gold (Dùng hàm JSON mới)
    p, c, pct = get_yahoo_json("GC=F")
    data['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # 2. Tech
    h1, rsi = get_gold_h1_json()
    d['h1'] = h1; d['rsi'] = rsi
    
    # 3. Lạm phát (Breakeven)
    # Lấy trực tiếp JSON, nếu 0 thì lấy Yield
    p10, c10, _ = get_yahoo_json("^T10YIE")
    p05, c05, _ = get_yahoo_json("^T5YIE")
    
    if p10 == 0:
        d['be_name'] = "US Yields (Lợi suất)"
        p10, c10, _ = get_yahoo_json("^TNX")
        p05, c05, _ = get_yahoo_json("^FVX")
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # 4. Risk
    p, _, pct = get_yahoo_json("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    
    p, _, pct = get_yahoo_json("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    # 5. SPDR
    v, c = get_spdr_real()
    d['spdr'] = {'v': v, 'c': c}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 5. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V17 - Direct JSON"

@app.route('/run_check')
def run_check():
    d = get_market_data() # Gọi hàm mới đã sửa lỗi
    
    # Đoạn này giữ nguyên logic lấy từ d['...'] như cũ, nhưng lưu ý d['h1'] và d['rsi']
    # Tôi sẽ map lại biến cho khớp
    data = d # Alias cho tiện
    
    alerts = []
    now = time.time()
    
    # CẢNH BÁO
    if data['rsi'] > CONFIG['RSI_HIGH'] and data['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {data['rsi']:.0f} + H1 chạy {data['h1']:.1f}$")
            last_alert_times['RSI'] = now
            
    if data['rsi'] < CONFIG['RSI_LOW'] and data['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {data['rsi']:.0f} + H1 sập {data['h1']:.1f}$")
            last_alert_times['RSI'] = now

    if data['h1'] > CONFIG['GOLD_H1_LIMIT']:
        if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {data['h1']:.1f} giá")
            last_alert_times['H1'] = now

    if abs(data['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
        if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(data['be10']['c']):.3f} điểm")
            last_alert_times['BE'] = now
    
    if data['vix']['p'] > CONFIG['VIX_LIMIT']:
         if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {data['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert"

    # DASHBOARD
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        spdr_str = f"{data['spdr']['v']:.2f} tấn" if data['spdr']['v'] > 0 else "Chờ cập nhật"
        spdr_chg = f"({s(data['spdr']['c'])}{data['spdr']['c']:.2f})" if data['spdr']['v'] > 0 else ""
        
        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {data['gold']['p']:.1f}\n"
            f"   {i(data['gold']['c'])} {s(data['gold']['c'])}{data['gold']['c']:.1f}$ ({s(data['gold']['pct'])}{data['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {data['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {spdr_str} {spdr_chg}\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>{data['be_name']}:</b>\n"
            f"   • 10Y: {data['be10']['p']:.2f}% (Chg: {s(data['be10']['c'])}{data['be10']['c']:.3f})\n"
            f"   • 05Y: {data['be05']['p']:.2f}% (Chg: {s(data['be05']['c'])}{data['be05']['c']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {data['gvz']['p']:.2f}\n"
        )
        send_tele(msg)
        return "Report"

    return "Ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
