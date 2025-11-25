from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd
import io
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
    "GVZ_LIMIT": 23,
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM TẠO KẾT NỐI ẨN DANH (Fake Browser)
# ==============================================================================
def create_session():
    """Tạo session giả lập trình duyệt Chrome để không bị chặn"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
    })
    return session

# ==============================================================================
# 3. HÀM LẤY DATA YAHOO (CHỐNG LỖI 0.00)
# ==============================================================================
def get_safe_data(ticker_symbol):
    try:
        session = create_session()
        ticker = yf.Ticker(ticker_symbol, session=session)
        hist = ticker.history(period="1mo")
        
        # Lọc dữ liệu rác
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] > 0.0001]
        
        if len(hist) < 2: return 0.0, 0.0, 0.0
        
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        return current, current - prev, (current - prev) / prev * 100
    except: return 0.0, 0.0, 0.0

# ==============================================================================
# 4. HÀM LẤY SPDR (NGUỒN GỐC - SOURCE OF TRUTH)
# ==============================================================================
def get_spdr_real():
    """
    Lấy trực tiếp từ file CSV báo cáo của Quỹ SPDR.
    Đây là nguồn mà kgold và các trang khác đều phải lấy theo.
    """
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        # Header bắt buộc để không bị chặn
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=10, verify=False) # verify=False để bỏ qua lỗi SSL nếu có
        
        if r.status_code == 200:
            # Đọc CSV thông minh: Tìm dòng chứa chữ "Tonnes"
            lines = r.text.split('\n')
            header_row = 0
            for i, line in enumerate(lines[:20]): # Quét 20 dòng đầu
                if "Tonnes" in line:
                    header_row = i
                    break
            
            # Đọc lại với đúng header
            df = pd.read_csv(io.StringIO(r.text), skiprows=header_row)
            
            # Tìm cột chứa Tonnes
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                # Lấy dữ liệu dòng cuối cùng (bỏ qua NaN)
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    curr = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return curr, curr - prev
                    
        return 0.0, 0.0
    except Exception as e:
        print(f"SPDR Error: {e}")
        return 0.0, 0.0

# ==============================================================================
# 5. CÁC HÀM HỖ TRỢ KHÁC (RSI, H1)
# ==============================================================================
def get_gold_tech():
    try:
        session = create_session()
        data = yf.download("GC=F", period="5d", interval="1h", progress=False, session=session)
        if len(data) < 15: return 0.0, 50.0 
        
        # RSI
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        # H1 Range
        last = data.iloc[-1]
        try: h, l = float(last['High'].item()), float(last['Low'].item())
        except: h, l = float(last['High']), float(last['Low'])
            
        return h - l, float(rsi.iloc[-1])
    except: return 0.0, 50.0

# ==============================================================================
# 6. TỔNG HỢP & LOGIC
# ==============================================================================
def get_data():
    d = {}
    
    # Gold
    p, c, pct = get_safe_data("GC=F")
    d['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # Tech
    h1, rsi = get_gold_tech()
    d['h1'] = h1; d['rsi'] = rsi
    
    # SPDR
    v, c = get_spdr_real()
    d['spdr'] = {'v': v, 'c': c}
    
    # Lạm phát (Breakeven -> Yield Fallback)
    p10, c10, _ = get_safe_data("^T10YIE")
    p05, c05, _ = get_safe_data("^T5YIE")
    
    if p10 == 0: # Nếu Yahoo chặn Breakeven
        d['be_name'] = "US Yields (Lợi suất)"
        p10, c10, _ = get_safe_data("^TNX") # 10Y Yield
        p05, c05, _ = get_safe_data("^FVX") # 5Y Yield
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # VIX/GVZ
    p, _, pct = get_safe_data("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    p, _, pct = get_safe_data("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

@app.route('/')
def home(): return "Bot V14 - Source of Truth"

@app.route('/run_check')
def run_check():
    d = get_data()
    alerts = []
    now = time.time()
    
    # CẢNH BÁO
    if d['rsi'] > CONFIG['RSI_HIGH'] and d['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['rsi']:.0f} + H1 chạy {d['h1']:.1f}$")
            last_alert_times['RSI'] = now
            
    if d['rsi'] < CONFIG['RSI_LOW'] and d['h1'] > 20:
        if now - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['rsi']:.0f} + H1 sập {d['h1']:.1f}$")
            last_alert_times['RSI'] = now

    if d['h1'] > CONFIG['GOLD_H1_LIMIT']:
        if now - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {d['h1']:.1f} giá")
            last_alert_times['H1'] = now
            
    if d['vix']['p'] > CONFIG['VIX_LIMIT']:
        if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {d['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert"

    # DASHBOARD 30P
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        # Xử lý hiển thị SPDR
        spdr_str = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chưa cập nhật"
        spdr_chg_str = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""

        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {d['gold']['p']:.1f}\n"
            f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {d['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {spdr_str} {spdr_chg_str}\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>{d['be_name']}:</b>\n"
            f"   • 10Y: {d['be10']['p']:.2f}% (Chg: {s(d['be10']['c'])}{d['be10']['c']:.3f})\n"
            f"   • 05Y: {d['be05']['p']:.2f}% (Chg: {s(d['be05']['c'])}{d['be05']['c']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {d['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {d['gvz']['p']:.2f}\n"
        )
        send_tele(msg)
        return "Report"

    return "Ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
