from flask import Flask
import yfinance as yf
from datetime import datetime
import time
import requests
import pandas as pd
import io

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH (CONFIG)
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    # NGƯỠNG CẢNH BÁO KHẨN CẤP
    "VIX_LIMIT": 30,             
    "VIX_PCT_LIMIT": 20.0,
    "GVZ_LIMIT": 25,             
    "GVZ_PCT_LIMIT": 15.0,
    "GOLD_H1_LIMIT": 40.0,       
    "BE_CHANGE_LIMIT": 0.25,     
    
    "ALERT_COOLDOWN": 3600       
}

last_alert_times = {}

# ==============================================================================
# 2. HÀM LẤY DỮ LIỆU CHỨNG KHOÁN (FIX TRIỆT ĐỂ SỐ 0)
# ==============================================================================
def get_safe_d1_data(ticker_symbol):
    """
    Lấy dữ liệu D1. 
    Cơ chế: Quét 1 tháng -> Lọc NaN -> Lọc số 0 -> Lấy ngày gần nhất có số liệu thực.
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1mo")
        
        # BƯỚC 1: Xóa dữ liệu rỗng (NaN)
        hist = hist.dropna(subset=['Close'])
        
        # BƯỚC 2: Xóa dữ liệu bằng 0 (Fix lỗi Yahoo trả về 0.00)
        hist = hist[hist['Close'] != 0]
        
        if len(hist) < 2:
            return 0.0, 0.0, 0.0
            
        # Lấy giá trị hiện tại (dòng cuối) và hôm qua (dòng sát cuối)
        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        
        change_val = current - prev
        change_pct = (change_val / prev * 100) if prev != 0 else 0
        
        return current, change_val, change_pct
    except Exception as e:
        print(f"Lỗi {ticker_symbol}: {e}")
        return 0.0, 0.0, 0.0

def get_gold_h1_range():
    """Lấy biên độ H1 Vàng Spot (XAUUSD=X)"""
    try:
        data = yf.download("XAUUSD=X", period="1d", interval="1h", progress=False)
        if not data.empty:
            try:
                high = float(data['High'].iloc[-1].item())
                low = float(data['Low'].iloc[-1].item())
            except:
                high = float(data['High'].iloc[-1])
                low = float(data['Low'].iloc[-1])
            return high - low
        return 0.0
    except:
        return 0.0

# ==============================================================================
# 3. HÀM LẤY SPDR (CÀO FILE CSV)
# ==============================================================================
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
                    change_ton = current_ton - prev_ton
                    return current_ton, change_ton
        return 0.0, 0.0
    except:
        return 0.0, 0.0

# ==============================================================================
# 4. TỔNG HỢP & ROUTING
# ==============================================================================
def get_market_data():
    data = {}
    
    # Gold Spot
    cur, chg, pct = get_safe_d1_data("XAUUSD=X")
    data['gold_price'] = cur
    data['gold_change'] = chg
    data['gold_pct'] = pct
    
    # US Breakeven (Lạm phát)
    # Dùng mã 5Y (T5YIE) thay cho 2Y vì Yahoo ko có mã 2Y
    cur, chg, pct = get_safe_d1_data("^T5YIE") 
    data['be05_val'] = cur
    data['be05_chg'] = chg

    cur, chg, pct = get_safe_d1_data("^T10YIE")
    data['be10_val'] = cur
    data['be10_chg'] = chg
    
    # VIX & GVZ
    cur, chg, pct = get_safe_d1_data("^VIX")
    data['vix'] = cur
    data['vix_pct'] = pct
    
    cur, chg, pct = get_safe_d1_data("^GVZ")
    data['gvz'] = cur
    data['gvz_pct'] = pct

    # SPDR
    spdr_val, spdr_chg = get_spdr_holdings()
    data['spdr_val'] = spdr_val
    data['spdr_chg'] = spdr_chg

    # Gold H1
    data['gold_h1_range'] = get_gold_h1_range()
    
    return data

def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
        payload = {"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Lỗi Tele: {e}")

@app.route('/')
def home():
    return "Bot V8 - Final Fix 0.00"

@app.route('/run_check')
def run_check():
    data = get_market_data()
    alerts = []
    current_time = time.time()
    
    # --- ALERT LOGIC ---
    # 1. Vàng H1
    if data['gold_h1_range'] > CONFIG["GOLD_H1_LIMIT"]:
        if current_time - last_alert_times.get('GOLD_H1', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🚨 <b>VÀNG H1 CHẠY ĐIÊN:</b> {data['gold_h1_range']:.1f} giá")
            last_alert_times['GOLD_H1'] = current_time
    
    # 2. VIX
    if data['vix'] > CONFIG["VIX_LIMIT"] or data['vix_pct'] > CONFIG["VIX_PCT_LIMIT"]:
        if current_time - last_alert_times.get('VIX', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG ĐỎ:</b> {data['vix']:.2f} (Tăng {data['vix_pct']:.1f}%)")
            last_alert_times['VIX'] = current_time

    # 3. GVZ
    if data['gvz'] > CONFIG["GVZ_LIMIT"] or data['gvz_pct'] > CONFIG["GVZ_PCT_LIMIT"]:
        if current_time - last_alert_times.get('GVZ', 0) > CONFIG["ALERT_COOLDOWN"]:
            alerts.append(f"🌪 <b>GVZ BÃO VÀNG:</b> {data['gvz']:.2f} (Tăng {data['gvz_pct']:.1f}%)")
            last_alert_times['GVZ'] = current_time

    # 4. Lạm phát Breakeven
    if abs(data['be10_chg']) > CONFIG["BE_CHANGE_LIMIT"]:
        if current_time - last_alert_times.get('BE10', 0) > CONFIG["ALERT_COOLDOWN"]:
            tag = "TĂNG" if data['be10_chg'] > 0 else "GIẢM"
            alerts.append(f"🇺🇸 <b>LẠM PHÁT 10Y {tag} SỐC:</b> {abs(data['be10_chg']):.3f} điểm")
            last_alert_times['BE10'] = current_time

    if alerts:
        msg = "\n".join(alerts)
        send_telegram_msg(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n{msg}")
        return "Alert Sent"

    # --- DASHBOARD D1 (MỖI 30 PHÚT) ---
    current_minute = datetime.now().minute
    if (0 <= current_minute <= 2) or (30 <= current_minute <= 32):
        
        def sign(val): return "+" if val >= 0 else ""
        def icon(val): return "🟢" if val >= 0 else "🔴"

        status_msg = (
            f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
            f"Time: {datetime.now().strftime('%H:%M')}\n"
            f"-------------------------------\n"
            f"🥇 <b>XAU/USD (Spot):</b> {data['gold_price']:.1f}\n"
            f"   {icon(data['gold_change'])} {sign(data['gold_change'])}{data['gold_change']:.1f}$ ({sign(data['gold_pct'])}{data['gold_pct']:.2f}%)\n"
            f"-------------------------------\n"
            f"🐋 <b>SPDR Gold Trust:</b>\n"
            f"   • Tổng: {data['spdr_val']:.2f} tấn\n"
            f"   • H.nay: {sign(data['spdr_chg'])}{data['spdr_chg']:.2f} tấn\n"
            f"-------------------------------\n"
            f"🇺🇸 <b>Lạm phát Kỳ vọng (Breakeven):</b>\n"
            f"   • 10Y: {data['be10_val']:.2f}% (Chg: {sign(data['be10_chg'])}{data['be10_chg']:.3f})\n"
            f"   • 05Y: {data['be05_val']:.2f}% (Chg: {sign(data['be05_chg'])}{data['be05_chg']:.3f})\n"
            f"-------------------------------\n"
            f"📉 <b>VIX:</b> {data['vix']:.2f} ({sign(data['vix_pct'])}{data['vix_pct']:.1f}%)\n"
            f"🌪 <b>GVZ:</b> {data['gvz']:.2f} ({sign(data['gvz_pct'])}{data['gvz_pct']:.1f}%)\n"
        )
        send_telegram_msg(status_msg)
        return "Update Sent"

    return "Checked.", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
