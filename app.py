from flask import Flask
import yfinance as yf
import pandas as pd
import requests
import io
import time
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
# 2. HÀM TẠO SESSION (LỚP GIÁP CHỐNG CHẶN)
# ==============================================================================
def get_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    return session

# ==============================================================================
# 3. HÀM LẤY DATA (AN TOÀN TUYỆT ĐỐI)
# ==============================================================================
def get_safe_data(symbol):
    try:
        # Dùng session để lừa Yahoo
        session = get_session()
        ticker = yf.Ticker(symbol, session=session)
        
        # Lấy lịch sử 1 tháng
        hist = ticker.history(period="1mo")
        
        # Lọc bỏ dòng lỗi và dòng số 0
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] > 0.0001]
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        chg = current - prev
        pct = (chg / prev * 100)
        
        return current, chg, pct
    except Exception as e:
        print(f"Lỗi {symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_tech():
    try:
        session = get_session()
        # Lấy Gold Futures (GC=F)
        data = yf.download("GC=F", period="5d", interval="1h", progress=False, session=session)
        
        if len(data) < 15: return 0.0, 50.0
        
        # Tính RSI
        close = data['Close']
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        # Tính H1 Range
        last = data.iloc[-1]
        try:
            h = float(last['High'].item())
            l = float(last['Low'].item())
        except:
            h = float(last['High'])
            l = float(last['Low'])
            
        return h - l, current_rsi
    except: return 0.0, 50.0

def get_spdr():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        # Header giả lập bắt buộc
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=10, verify=False)
        
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
# 4. LOGIC CHÍNH
# ==============================================================================
def get_data():
    d = {}
    
    # Gold
    p, c, pct = get_safe_data("GC=F")
    d['gold'] = {'p': p, 'c': c, 'pct': pct}
    
    # Tech
    h1, rsi = get_gold_tech()
    d['h1'] = h1; d['rsi'] = rsi
    
    # Lạm phát (Breakeven -> Yield Fallback)
    p10, c10, _ = get_safe_data("^T10YIE")
    p05, c05, _ = get_safe_data("^T5YIE")
    
    # Nếu Yahoo trả về 0, chuyển sang Yield
    if p10 == 0:
        d['be_name'] = "US Yields (Lợi suất)"
        p10, c10, _ = get_safe_data("^TNX")
        p05, c05, _ = get_safe_data("^FVX")
    else:
        d['be_name'] = "Breakeven (Lạm phát)"
        
    d['be10'] = {'p': p10, 'c': c10}
    d['be05'] = {'p': p05, 'c': c05}
    
    # Risk
    p, _, pct = get_safe_data("^VIX")
    d['vix'] = {'p': p, 'pct': pct}
    p, _, pct = get_safe_data("^GVZ")
    d['gvz'] = {'p': p, 'pct': pct}
    
    # SPDR
    v, c = get_spdr()
    d['spdr'] = {'v': v, 'c': c}
    
    return d

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

@app.route('/')
def home(): return "Bot V18 - Tank Mode (Anti-Crash)"

@app.route('/run_check')
def run_check():
    # Bọc try-except toàn bộ để không bao giờ bị lỗi 500
    try:
        data = get_data()
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
            return "Alert Sent", 200

        # REPORT D1
        vn_now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        if vn_now.minute in [0, 1, 2, 30, 31, 32]:
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            # Format chuỗi hiển thị
            spdr_txt = f"{data['spdr']['v']:.2f} tấn" if data['spdr']['v'] > 0 else "Chưa cập nhật"
            spdr_chg = f"({s(data['spdr']['c'])}{data['spdr']['c']:.2f})" if data['spdr']['v'] > 0 else ""
            
            be10_txt = f"{data['be10']['p']:.2f}%" if data['be10']['p'] > 0 else "N/A"
            be05_txt = f"{data['be05']['p']:.2f}%" if data['be05']['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>Gold Futures:</b> {data['gold']['p']:.1f}\n"
                f"   {i(data['gold']['c'])} {s(data['gold']['c'])}{data['gold']['c']:.1f}$ ({s(data['gold']['pct'])}{data['gold']['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {data['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>{data['be_name']}:</b>\n"
                f"   • 10Y: {be10_txt} (Chg: {s(data['be10']['c'])}{data['be10']['c']:.3f})\n"
                f"   • 05Y: {be05_txt} (Chg: {s(data['be05']['c'])}{data['be05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"📉 <b>VIX:</b> {data['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {data['gvz']['p']:.2f}\n"
            )
            send_tele(msg)
            return "Report Sent", 200

        return "Checked", 200
        
    except Exception as e:
        print(f"System Error: {e}")
        return "Error handled", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
