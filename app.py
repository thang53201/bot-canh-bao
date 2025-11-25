from flask import Flask
import requests
import re
import time
import pandas as pd
import io
from datetime import datetime
import pytz

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    "GOLD_H1_LIMIT": 30.0,
    "RSI_HIGH": 80, 
    "RSI_LOW": 20,
    "VIX_LIMIT": 30,
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM CÀO HTML (WEB SCRAPING) - CHỐNG CHẶN API
# ==============================================================================
def get_html_price(symbol):
    """
    Tải trang web Yahoo và dùng Regex tìm giá.
    Bỏ qua API bị chặn.
    """
    url = f"https://finance.yahoo.com/quote/{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        
        # 1. Tìm giá hiện tại (regularMarketPrice)
        # Mẫu regex tìm chuỗi: value="1234.56"
        price_match = re.search(r'regularMarketPrice.*?value="([0-9\.]+)"', r.text)
        
        # 2. Tìm giá hôm qua (regularMarketPreviousClose)
        prev_match = re.search(r'regularMarketPreviousClose.*?value="([0-9\.]+)"', r.text)
        
        if price_match and prev_match:
            current = float(price_match.group(1))
            prev = float(prev_match.group(1))
            
            change = current - prev
            pct = (change / prev * 100) if prev != 0 else 0
            
            return current, change, pct
            
        return 0.0, 0.0, 0.0
    except Exception as e:
        print(f"Scrape Error {symbol}: {e}")
        return 0.0, 0.0, 0.0

# ==============================================================================
# 3. HÀM DỰ PHÒNG: LẤY VÀNG TỪ BINANCE (NẾU YAHOO CHẾT)
# ==============================================================================
def get_binance_gold():
    """Lấy giá PAXG/USDT (Vàng số) từ Binance nếu Yahoo tịt"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT"
        r = requests.get(url, timeout=5)
        data = r.json()
        
        current = float(data['lastPrice'])
        prev = float(data['prevClosePrice'])
        change = float(data['priceChange'])
        pct = float(data['priceChangePercent'])
        
        return current, change, pct
    except:
        return 0.0, 0.0, 0.0

# ==============================================================================
# 4. HÀM LẤY SPDR (CÀO CSV)
# ==============================================================================
def get_spdr():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        
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
# 5. TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_data():
    d = {}
    
    # 1. GOLD: Thử Yahoo HTML trước, nếu 0 thì lấy Binance
    p, c, pct = get_html_price("GC=F")
    if p == 0:
        p, c, pct = get_binance_gold() # Fallback Binance
        d['gold_src'] = "Binance (PAXG)"
    else:
        d['gold_src'] = "Yahoo Futures"
        
    d['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # 2. RSI & H1 (Tính sơ bộ từ giá hiện tại)
    # Vì cào HTML ko lấy được nến lịch sử, ta tạm gán RSI = 50 
    # (Để tránh báo ảo khi không có data lịch sử)
    d['rsi'] = 50.0 
    d['h1'] = 0.0
    
    # 3. LẠM PHÁT (Cào HTML)
    p10, c10, _ = get_html_price("^T10YIE")
    p05, c05, _ = get_html_price("^T5YIE")
    
    # Nếu bị chặn tiếp, chuyển sang Yield
    if p10 == 0:
        d['be_name'] = "US Yields (Lợi suất)"
        p10, c10, _ = get_html_price("^TNX")
        p05, c05, _ = get_html_price("^FVX")
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # 4. RISK
    p, _, pct = get_html_price("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    
    p, _, pct = get_html_price("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    # 5. SPDR
    v, c = get_spdr()
    d['spdr'] = {'v': v, 'c': c}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

@app.route('/')
def home(): return "Bot V19 - HTML Scraping Mode"

@app.route('/run_check')
def run_check():
    data = get_data()
    alerts = []
    now = time.time()
    
    # CẢNH BÁO (Bỏ qua RSI/H1 vì scraping ko lấy được nến lịch sử chuẩn)
    # Chỉ cảnh báo các chỉ số giá trị thực
    
    if data['vix']['p'] > CONFIG['VIX_LIMIT']:
         if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {data['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if abs(data['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
        if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>VĨ MÔ BIẾN ĐỘNG:</b> Đổi {abs(data['be10']['c']):.3f} điểm")
            last_alert_times['BE'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert"

    # DASHBOARD
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        spdr_str = f"{data['spdr']['v']:.2f} tấn" if data['spdr']['v'] > 0 else "Chưa cập nhật"
        spdr_chg = f"({s(data['spdr']['c'])}{data['spdr']['c']:.2f})" if data['spdr']['v'] > 0 else ""
        
        # Xử lý hiển thị nếu vẫn bị chặn hết
        gold_price = f"{data['gold']['p']:.1f}" if data['gold']['p'] > 0 else "N/A (Bị chặn)"
        
        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"Nguồn Vàng: {data['gold_src']}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold:</b> {gold_price}\n"
            f"   {i(data['gold']['c'])} {s(data['gold']['c'])}{data['gold']['c']:.1f}$ ({s(data['gold']['pct'])}{data['gold']['pct']:.2f}%)\n"
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
