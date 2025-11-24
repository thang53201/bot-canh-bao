from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd
import io
import numpy as np

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG) - DCA PROTECTOR MODE
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # --- NGƯỠNG CẢNH BÁO KHẨN CẤP ---
    # 1. Lực nến: Hạ xuống 30 giá để cảnh báo sớm trướcc khi đi 100 giá
    "GOLD_H1_LIMIT": 30.0,
    
    # 2. RSI (Bẫy giá): RSI > 80 mà giá vẫn chạy là siêu trend
    "RSI_HIGH": 80,
    "RSI_LOW": 20,
    
    # 3. Tâm lý & Vĩ mô
    "VIX_LIMIT": 30,             
    "GVZ_LIMIT": 23,        # Hạ chút để nhạy hơn với bão vàng
    "BE_CHANGE_LIMIT": 0.15, # Lạm phát đổi 0.15 là trend dài
    
    "ALERT_COOLDOWN": 3600  # Im lặng 60 phút sau khi báo
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM TÍNH TOÁN KỸ THUẬT (RSI & H1)
# ==============================================================================
def calculate_rsi(series, period=14):
    """Tính RSI thủ công không cần thư viện ngoài"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def get_gold_technical():
    """
    Lấy RSI và H1 Range của Vàng Spot.
    Mục đích: Phát hiện trend một chiều.
    """
    try:
        # Lấy dữ liệu H1 trong 5 ngày để đủ nến tính RSI
        data = yf.download("XAUUSD=X", period="5d", interval="1h", progress=False)
        
        if len(data) < 15: return 0.0, 50.0 # Không đủ dữ liệu
        
        # 1. Tính RSI 14
        data['RSI'] = calculate_rsi(data['Close'], period=14)
        current_rsi = float(data['RSI'].iloc[-1])
        
        # 2. Tính Biên độ H1 hiện tại
        try:
            high = float(data['High'].iloc[-1].item())
            low = float(data['Low'].iloc[-1].item())
        except:
            high = float(data['High'].iloc[-1])
            low = float(data['Low'].iloc[-1])
        
        h1_range = high - low
        
        return h1_range, current_rsi
    except Exception as e:
        print(f"Lỗi Tech: {e}")
        return 0.0, 50.0

# ==============================================================================
# 3. HÀM LẤY DATA D1 & KHÁC
# ==============================================================================
def get_safe_d1_data(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1mo")
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] != 0] # Lọc số 0
        
        if len(hist) < 2: return 0.0, 0.0, 0.0
        
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        return current, change_val, change_pct
    except: return 0.0, 0.0, 0.0

def get_spdr_holdings():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
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

def get_market_data():
    data = {}
    
    # 1. Gold & RSI
    cur, chg, pct = get_safe_d1_data("XAUUSD=X")
    data['gold_price'] = cur; data['gold_change'] = chg; data['gold_pct'] = pct
    
    h1_range, rsi = get_gold_technical()
    data['gold_h1'] = h1_range
    data['rsi'] = rsi
    
    # 2. Breakeven (5Y & 10Y)
    cur, chg, pct = get_safe_d1_data("^T5YIE")
    data['be05_val'] = cur; data['be05_chg'] = chg
    cur, chg, pct = get_safe_d1_data("^T10YIE")
    data['be10_val'] = cur; data['be10_chg'] = chg
    
    # 3. VIX & GVZ
    cur, chg, pct = get_safe_d1_data("^VIX")
    data['vix'] = cur; data['vix_pct'] = pct
    cur, chg, pct = get_safe_d1_data("^GVZ")
    data['gvz'] = cur; data['gvz_pct'] = pct
    
    # 4. SPDR
    val, chg = get_spdr_holdings()
    data['spdr_val'] = val; data['spdr_chg'] = chg
    
    return data

def send_telegram_msg(message):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": message, "parse_mode": "HTML"})
    except: pass

# ==============================================================================
# 4. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V9 - DCA Protector Ready"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- LOGIC CẢNH BÁO SÓNG THẦN (Check mỗi phút) ---
    
    # 1. COMBO TỬ THẦN: RSI Cực đoan + Giá vẫn chạy mạnh
    # Ý nghĩa: Đã quá mua mà giá vẫn tăng > 20$ --> Phe mua quá mạnh, Sell là chết.
    if data['rsi'] > CONFIG['RSI_HIGH'] and data['gold_h1'] > 20:
        if current_time - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {data['rsi']:.1f} (Quá mua) + H1 chạy {data['gold_h1']:.1f}$.\n👉 <b>Cấm Sell bắt đỉnh!</b>")
            last_alert_times['RSI'] = current_time
            
    if data['rsi'] < CONFIG['RSI_LOW'] and data['gold_h1'] > 20:
        if current_time - last_alert_times.get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {data['rsi']:.1f} (Quá bán) + H1 sập {data['gold_h1']:.1f}$.\n👉 <b>Cấm Buy bắt đáy!</b>")
            last_alert_times['RSI'] = current_time

    # 2. Vàng H1 Sốc (>30 giá)
    if data['gold_h1'] > CONFIG['GOLD_H1_LIMIT']:
        if current_time - last_alert_times.get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🚨 <b>VÀNG BIẾN ĐỘNG MẠNH:</b> H1 {data['gold_h1']:.1f} giá.")
            last_alert_times['H1'] = current_time

    # 3. VIX & GVZ
    if data['vix'] > CONFIG['VIX_LIMIT']:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {data['vix']:.2f}")
            last_alert_times['VIX'] = current_time
            
    if data['gvz'] > CONFIG['GVZ_LIMIT']:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG['ALERT_COOLDOWN']:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f}")
            last_alert_times['GVZ'] = current_time

    # 4. Lạm phát
    if abs(data['be10_chg']) > CONFIG['BE_CHANGE_LIMIT']:
        if current_time - last_alert_times.get('BE', 0) > CONFIG['ALERT_COOLDOWN']:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT {tag} SỐC:</b> {abs(data['be10_chg']):.3f} điểm")
            last_alert_times['BE'] = current_time

    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO RỦI RO</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- DASHBOARD D1 (Check mỗi 30 phút) ---
    current_minute = datetime.now().minute
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""
        def icon(val): return "🟢" if val >= 0 else "🔴"

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>XAU/USD:</b> {data['gold_price']:.1f}\n"
            f"   {icon(data['gold_change'])} {sign(data['gold_change'])}{data['gold_change']:.1f}$ ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"   🎯 <b>RSI (H1):</b> {data['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold:</b> {data['spdr_val']:.2f} tấn ({sign(data['spdr_chg'])}{data['spdr_chg']:.2f})\n"
            f"🇺🇸 <b>Breakeven (Lạm phát):</b>\n"
            f"   • 10Y: {data['be10_val']:.2f}% (Chg: {sign(data['be10_chg'])}{data['be10_chg']:.3f})\n"
            f"   • 05Y: {data['be05_val']:.2f}% (Chg: {sign(data['be05_chg'])}{data['be05_chg']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']:.2f} | 🌪 <b>GVZ:</b> {data['gvz']:.2f}\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
