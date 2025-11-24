from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd
import io
import pandas_ta as ta  # Cần cài thêm thư viện: pip install pandas_ta

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO KHẨN CẤP (Tinh chỉnh cho DCA)
    "VIX_LIMIT": 30,             
    "VIX_PCT_LIMIT": 15.0,       # Hạ xuống 15% cho nhạy
    "GVZ_LIMIT": 23,             # Hạ xuống 23 để bắt sớm bão
    "GOLD_H1_LIMIT": 30.0,       # Hạ xuống 30 giá để cảnh báo sớm hơn
    "RSI_LIMIT_HIGH": 80,        # RSI H4 quá mua cực đoan
    "RSI_LIMIT_LOW": 20,         # RSI H4 quá bán cực đoan
    "BE_CHANGE_LIMIT": 0.15,     # Hạ ngưỡng Lạm phát để bắt trend sớm
    
    "ALERT_COOLDOWN": 3600       
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY DATA & TÍNH TOÁN
# ==============================================================================
def get_safe_d1_data(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1mo")
        hist = hist.dropna(subset=['Close'])
        hist = hist[hist['Close'] != 0]
        
        if len(hist) < 2: return 0.0, 0.0, 0.0
        
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except: return 0.0, 0.0, 0.0

def get_gold_technical():
    """
    Lấy RSI H4 và Biên độ H1 của Vàng
    """
    try:
        # Lấy dữ liệu H1 để tính biên độ
        data_h1 = yf.download("XAUUSD=X", period="2d", interval="1h", progress=False)
        
        # Lấy dữ liệu H1 (giả lập H4 bằng cách resample hoặc dùng H1 RSI 14 kỳ tương đương)
        # Để đơn giản và chính xác trên data free, ta dùng RSI H1 chu kỳ 60 (tương đương H4 ngắn) 
        # hoặc lấy data H1 tính RSI 14 chuẩn.
        
        if data_h1.empty: return 0.0, 50.0 # Default RSI 50
        
        # 1. Tính Biên độ nến H1 cuối cùng
        try:
            high = float(data_h1['High'].iloc[-1].item())
            low = float(data_h1['Low'].iloc[-1].item())
        except:
            high = float(data_h1['High'].iloc[-1])
            low = float(data_h1['Low'].iloc[-1])
        h1_range = high - low

        # 2. Tính RSI (Dùng thư viện pandas_ta hoặc công thức tay)
        # Công thức RSI đơn giản để không cần cài nặng
        delta = data_h1['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        
        return h1_range, current_rsi
        
    except Exception as e:
        print(f"Lỗi Tech: {e}")
        return 0.0, 50.0

def get_spdr_holdings():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            df = pd.read_csv(io.StringIO(response.text), skiprows=6)
            col_name = [c for c in df.columns if "Tonnes" in str(c)]
            if col_name:
                df_clean = df.dropna(subset=[col_name[0]])
                if len(df_clean) >= 2:
                    current_ton = float(df_clean.iloc[-1][col_name[0]])
                    prev_ton = float(df_clean.iloc[-2][col_name[0]])
                    return current_ton, current_ton - prev_ton
        return 0.0, 0.0
    except: return 0.0, 0.0

def get_market_data():
    data = {}
    cur, chg, pct = get_safe_d1_data("XAUUSD=X")
    data['gold_price'] = cur; data['gold_change'] = chg; data['gold_pct'] = pct
    
    cur, chg, pct = get_safe_d1_data("^T5YIE") 
    data['be05_val'] = cur; data['be05_chg'] = chg
    cur, chg, pct = get_safe_d1_data("^T10YIE")
    data['be10_val'] = cur; data['be10_chg'] = chg
    
    cur, chg, pct = get_safe_d1_data("^VIX")
    data['vix'] = cur; data['vix_pct'] = pct
    cur, chg, pct = get_safe_d1_data("^GVZ")
    data['gvz'] = cur; data['gvz_pct'] = pct

    spdr_val, spdr_chg = get_spdr_holdings()
    data['spdr_val'] = spdr_val; data['spdr_chg'] = spdr_chg

    # Lấy thêm RSI và H1 Range
    h1_range, rsi = get_gold_technical()
    data['gold_h1_range'] = h1_range
    data['rsi'] = rsi
    
    return data

def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
        payload = {"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload)
    except: pass

@app.route('/')
def home(): return "Bot DCA Protector Active"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- LOGIC CẢNH BÁO SÓNG THẦN (DCA KILLER) ---
    
    # 1. RSI CỰC ĐOAN + BIẾN ĐỘNG MẠNH (Dấu hiệu sóng không hồi)
    # Nếu RSI > 80 (Quá mua) mà nến H1 vẫn chạy > 20 giá -> Bơm tiền đẩy giá tiếp -> Nguy hiểm cho lệnh Sell
    if (data['rsi'] > CONFIG["RSI_LIMIT_HIGH"] and data['gold_h1_range'] > 20):
         if current_time - last_alert_times.get('RSI_HIGH', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚀 <b>DANGER BUY TREND:</b> RSI {data['rsi']:.1f} (Quá mua) nhưng giá vẫn chạy mạnh! Cẩn thận Sell.")
            last_alert_times['RSI_HIGH'] = current_time
            
    if (data['rsi'] < CONFIG["RSI_LIMIT_LOW"] and data['gold_h1_range'] > 20):
         if current_time - last_alert_times.get('RSI_LOW', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🩸 <b>DANGER SELL TREND:</b> RSI {data['rsi']:.1f} (Quá bán) nhưng giá vẫn sập! Cẩn thận Buy.")
            last_alert_times['RSI_LOW'] = current_time

    # 2. Vàng H1 Sốc (Hạ ngưỡng xuống 30 để cảnh báo sớm)
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 SỐC:</b> {data['gold_h1_range']:.1f} giá (Dễ đi 1 chiều)")
            last_alert_times['GOLD_H1'] = current_time

    # 3. VIX & GVZ (Ngưỡng cũ)
    if data['vix'] > CONFIG["VIX_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {data['vix']:.2f} (Thị trường hoảng loạn)")
            last_alert_times['VIX'] = current_time
            
    if data['gvz'] > CONFIG["GVZ_LIMIT"]:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f} (Biên độ cực lớn)")
            last_alert_times['GVZ'] = current_time

    # 4. Lạm phát (Hạ ngưỡng xuống 0.15)
    if abs(data['be10_chg']) > CONFIG["BE_CHANGE_LIMIT"]:
        if current_time - last_alert_times.get('BE10', 0) > CONFIG["ALERT_COOLDOWN"]:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT {tag}:</b> {abs(data['be10_chg']):.3f} điểm (Thay đổi kỳ vọng)")
            last_alert_times['BE10'] = current_time

    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO RỦI RO DCA</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- DASHBOARD D1 (Mỗi 30p) ---
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
            f"   🎯 <b>RSI (H1/H4):</b> {data['rsi']:.1f}\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR:</b> {data['spdr_val']:.2f} ({sign(data['spdr_chg'])}{data['spdr_chg']:.2f})\n"
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
