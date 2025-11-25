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
    
    # NGƯỠNG CẢNH BÁO (DCA PROTECTOR)
    "GOLD_H1_LIMIT": 30.0,       # Vàng H1 > 30 giá
    "RSI_HIGH": 80,              # RSI Quá mua
    "RSI_LOW": 20,               # RSI Quá bán
    
    "VIX_LIMIT": 30,             
    "GVZ_LIMIT": 23,
    "BE_CHANGE_LIMIT": 0.15,     # Lạm phát đổi > 0.15
    
    "ALERT_COOLDOWN": 3600
}

last_alert_times = {}

# ==============================================================================
# 2. KỸ THUẬT: TẠO SESSION NGỤY TRANG (ĐỂ LẤY DATA THẬT)
# ==============================================================================
def create_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
    })
    return session

# ==============================================================================
# 3. HÀM LẤY DATA CHÍNH XÁC (KHÔNG TRÁO ĐỔI)
# ==============================================================================
def get_real_data(ticker_symbol):
    """
    Lấy dữ liệu chính chủ. Tuyệt đối không thay thế bằng mã khác.
    Dùng session ngụy trang để tránh bị trả về 0.
    """
    try:
        session = create_session()
        ticker = yf.Ticker(ticker_symbol, session=session)
        
        # Lấy lịch sử 1 tháng để chắc chắn tìm được phiên giao dịch gần nhất
        hist = ticker.history(period="1mo")
        
        # 1. Lọc bỏ dữ liệu lỗi (NaN)
        hist = hist.dropna(subset=['Close'])
        # 2. Lọc bỏ số 0 (Yahoo lỗi trả về 0)
        hist = hist[hist['Close'] > 0.0001]
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        # Lấy giá trị thực tế của phiên gần nhất
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        chg = current - prev
        pct = (chg / prev * 100)
        
        return current, chg, pct
    except Exception as e:
        print(f"Lỗi lấy {ticker_symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_technical():
    """Lấy RSI và H1 Range từ Gold Futures (GC=F)"""
    try:
        session = create_session()
        # Dùng GC=F vì nó là dữ liệu thực, realtime nhất trên Yahoo
        data = yf.download("GC=F", period="5d", interval="1h", progress=False, session=session)
        
        if len(data) < 15: return 0.0, 50.0 
        
        # Tính RSI thủ công
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        # Tính H1 Range
        last = data.iloc[-1]
        # Fix lỗi format mới của yfinance
        try:
            h = float(last['High'].item())
            l = float(last['Low'].item())
        except:
            h = float(last['High'])
            l = float(last['Low'])
        
        return h - l, current_rsi
    except: return 0.0, 50.0

def get_spdr():
    """Cào dữ liệu SPDR (Chính chủ)"""
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                if len(df) >= 2:
                    cur = float(df.iloc[-1][col[0]])
                    prev = float(df.iloc[-2][col[0]])
                    return cur, cur - prev
        return 0.0, 0.0
    except: return 0.0, 0.0

# ==============================================================================
# 4. TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_market_data():
    data = {}
    
    # 1. GOLD (GC=F) - Realtime Futures
    cur, chg, pct = get_real_data("GC=F")
    data['gold'] = {'p': cur, 'chg': chg, 'pct': pct}
    
    h1, rsi = get_gold_technical()
    data['gold_h1'] = h1
    data['rsi'] = rsi
    
    # 2. LẠM PHÁT (ĐÚNG MÃ KHÁCH YÊU CẦU)
    # 10 Year Breakeven
    cur, chg, pct = get_real_data("^T10YIE")
    data['be10'] = {'p': cur, 'chg': chg}
    
    # 5 Year Breakeven (Dùng 5Y đại diện cho ngắn hạn vì Yahoo ko có 2Y)
    cur, chg, pct = get_real_data("^T5YIE")
    data['be05'] = {'p': cur, 'chg': chg}
    
    # 3. Risk (VIX, GVZ)
    cur, chg, pct = get_real_data("^VIX")
    data['vix'] = {'p': cur, 'pct': pct}
    
    cur, chg, pct = get_real_data("^GVZ")
    data['gvz'] = {'p': cur, 'pct': pct}
    
    # 4. SPDR
    val, chg = get_spdr()
    data['spdr'] = {'v': val, 'chg': chg}
    
    return data

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 5. ROUTING & LOGIC CHECK
# ==============================================================================
@app.route('/')
def home(): return "Bot V13 - Real Data Only"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- CHECK BIẾN CỐ (MỖI 1 PHÚT) ---
    
    # 1. RSI Quá mua/bán + Giá chạy
    if data['rsi'] > CONFIG['RSI_HIGH'] and data['gold_h1'] > 20:
        if current_time - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {data['rsi']:.1f} + H1 chạy {data['gold_h1']:.1f}$")
            last_alert_times['RSI'] = current_time
            
    if data['rsi'] < CONFIG['RSI_LOW'] and data['gold_h1'] > 20:
        if current_time - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {data['rsi']:.1f} + H1 sập {data['gold_h1']:.1f}$")
            last_alert_times['RSI'] = current_time

    # 2. Vàng H1 Sốc
    if data['gold_h1'] > CONFIG['GOLD_H1_LIMIT']:
        if current_time - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG:</b> H1 {data['gold_h1']:.1f} giá")
            last_alert_times['H1'] = current_time

    # 3. VIX
    if data['vix']['p'] > CONFIG['VIX_LIMIT']:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {data['vix']['p']:.2f}")
            last_alert_times['VIX'] = current_time

    # 4. Lạm phát (Breakeven 10Y) - Cảnh báo khi thay đổi mạnh
    if abs(data['be10']['chg']) > CONFIG['BE_CHANGE_LIMIT']:
        if current_time - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🇺🇸 <b>LẠM PHÁT BIẾN ĐỘNG:</b> Thay đổi {abs(data['be10']['chg']):.3f} điểm")
            last_alert_times['BE'] = current_time

    if alerts:
        send_telegram(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
        return "Alert Sent"

    # --- BÁO CÁO 30 PHÚT (D1) ---
    vn_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now = datetime.now(vn_tz)
    
    if now.minute in [0, 1, 2, 30, 31, 32]:
        def s(v): return "+" if v >= 0 else ""
        def i(v): return "🟢" if v >= 0 else "🔴"
        
        # Nếu Breakeven vẫn là 0 (do Yahoo chưa có dữ liệu hôm nay), hiển thị cảnh báo
        be10_display = f"{data['be10']['p']:.2f}%" if data['be10']['p'] > 0 else "Chờ cập nhật..."
        be05_display = f"{data['be05']['p']:.2f}%" if data['be05']['p'] > 0 else "Chờ cập nhật..."

        msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {now.strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>Gold Futures:</b> {data['gold']['p']:.1f}\n"
            f"   {i(data['gold']['chg'])} {s(data['gold']['chg'])}{data['gold']['chg']:.1f}$ ({s(data['gold']['pct'])}{data['gold']['pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {data['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {data['spdr']['v']:.2f} tấn ({s(data['spdr']['chg'])}{data['spdr']['chg']:.2f})\n"
            f"🇺🇸 <b>Lạm phát Kỳ vọng (Breakeven):</b>\n"
            f"   • 10Y: {be10_display} (Chg: {s(data['be10']['chg'])}{data['be10']['chg']:.3f})\n"
            f"   • 05Y: {be05_display} (Chg: {s(data['be05']['chg'])}{data['be05']['chg']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']['p']:.2f} | 🌪 <b>GVZ:</b> {data['gvz']['p']:.2f}\n"
        )
        send_telegram(msg)
        return "Report Sent"

    return "Checked", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
