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
    "BE_CHANGE_LIMIT": 0.15,
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM TẠO SESSION (GIẢ LẬP TRÌNH DUYỆT)
# ==============================================================================
def create_session():
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'})
    return s

# ==============================================================================
# 3. HÀM LẤY LẠM PHÁT (NGUỒN KÉP: YAHOO + FRED)
# ==============================================================================
def get_breakeven_real(years=10):
    """
    Lấy Lạm phát kỳ vọng. 
    Ưu tiên Yahoo. Nếu Yahoo = 0 thì lấy từ nguồn gốc FRED (Fed St. Louis).
    Tuyệt đối không thay thế bằng Yield.
    """
    symbol = "^T10YIE" if years == 10 else "^T5YIE"
    fred_id = "T10YIE" if years == 10 else "T5YIE"
    
    # CÁCH 1: THỬ YAHOO FINANCE (Realtime)
    try:
        session = create_session()
        ticker = yf.Ticker(symbol, session=session)
        hist = ticker.history(period="5d")
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] > 0.0001]
        
        if not hist.empty:
            cur = float(hist['Close'].iloc[-1])
            prev = float(hist['Close'].iloc[-2]) if len(hist) > 1 else cur
            return cur, cur - prev
    except:
        pass # Nếu lỗi, bỏ qua để xuống Cách 2

    # CÁCH 2: LẤY TỪ FRED (FEDERAL RESERVE) - NGUỒN DỰ PHÒNG
    try:
        # URL file CSV trực tiếp từ Fed
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            # FRED hay dùng dấu "." cho ngày nghỉ lễ, cần lọc bỏ
            df = df[df[fred_id] != '.']
            df[fred_id] = pd.to_numeric(df[fred_id])
            
            if len(df) >= 2:
                cur = float(df.iloc[-1][fred_id])
                prev = float(df.iloc[-2][fred_id])
                return cur, cur - prev
    except Exception as e:
        print(f"FRED Error: {e}")
        
    return 0.0, 0.0 # Chịu thua (Rất hiếm khi xảy ra)

# ==============================================================================
# 4. HÀM LẤY SPDR (NGUỒN GỐC)
# ==============================================================================
def get_spdr_real():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
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
# 5. CÁC HÀM CƠ BẢN KHÁC
# ==============================================================================
def get_safe_yahoo(symbol):
    try:
        session = create_session()
        ticker = yf.Ticker(symbol, session=session)
        hist = ticker.history(period="5d")
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] > 0.0001]
        if len(hist) < 2: return 0.0, 0.0, 0.0
        cur = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        return cur, cur - prev, (cur - prev)/prev*100
    except: return 0.0, 0.0, 0.0

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
# 6. LOGIC CHÍNH
# ==============================================================================
def get_data():
    d = {}
    
    # 1. Gold Futures
    p, c, pct = get_safe_yahoo("GC=F")
    d['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # 2. Tech (RSI, H1)
    h1, rsi = get_gold_tech()
    d['h1'] = h1; d['rsi'] = rsi
    
    # 3. SPDR (Nguồn gốc)
    v, c = get_spdr_real()
    d['spdr'] = {'v': v, 'c': c}
    
    # 4. Lạm phát (Nguồn kép: Yahoo -> FRED)
    p10, c10 = get_breakeven_real(10)
    p05, c05 = get_breakeven_real(5)
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # 5. Risk
    p, _, pct = get_safe_yahoo("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    p, _, pct = get_safe_yahoo("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

@app.route('/')
def home(): return "Bot V15 - No Substitution"

@app.route('/run_check')
def run_check():
    d = get_data()
    alerts = []
    now = time.time()
    
    # --- CẢNH BÁO ---
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

    if abs(d['be10']['c']) > CONFIG['BE_CHANGE_LIMIT']:
        if now - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(d['be10']['c']):.3f} điểm")
            last_alert_times['BE'] = now
            
    if d['vix']['p'] > 30:
         if now - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX CAO:</b> {d['vix']['p']:.2f}")
            last_alert_times['VIX'] = now

    if alerts:
        send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert"

    # --- BÁO CÁO ---
    vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    if vn_now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {vn_now.strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {d['gold']['p']:.1f}\n"
            f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {d['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {d['spdr']['v']:.2f} tấn ({s(d['spdr']['c'])}{d['spdr']['c']:.2f})\n"
            f"🇺🇸 <b>Breakeven (Lạm phát):</b>\n"
            f"   • 10Y: {d['be10']['p']:.3f}% ({s(d['be10']['c'])}{d['be10']['c']:.3f})\n"
            f"   • 05Y: {d['be05']['p']:.3f}% ({s(d['be05']['c'])}{d['be05']['c']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {d['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {d['gvz']['p']:.2f}\n"
        )
        send_tele(msg)
        return "Report"

    return "Ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
