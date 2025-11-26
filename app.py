from flask import Flask
import requests
import pandas as pd
import io
import time
import random
from datetime import datetime, timedelta
import pytz
import json

app = Flask(__name__)

# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
CONFIG = {
    "TELEGRAM_TOKEN": "8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo",
    "TELEGRAM_CHAT_ID": "5464507208",
    
    "GOLD_H1_LIMIT": 40.0,
    "RSI_HIGH": 82, "RSI_LOW": 18, "RSI_PRICE_MOVE": 30.0,
    "VIX_VAL_LIMIT": 30, "VIX_PCT_LIMIT": 15.0,
    "GVZ_VAL_LIMIT": 25, "GVZ_PCT_LIMIT": 10.0,
    "INF_10Y_LIMIT": 0.25, "INF_05Y_LIMIT": 0.20,
    "FED_PCT_LIMIT": 15.0,
    
    "ALERT_COOLDOWN": 3600
}

# Cache để lưu dữ liệu cũ phòng khi mạng lag quá không lấy được
CACHE = {
    'last_fed': {'label': 'Đang tải...', 'prob': 0, 'change': 0},
    'last_alert': {}
}

# ==============================================================================
# 2. HÀM LẤY FEDWATCH (TẤN CÔNG API CỦA CME)
# ==============================================================================
def get_cme_fedwatch():
    """
    Cố gắng lấy dữ liệu % lãi suất từ API ẩn của CME Group.
    """
    try:
        # API JSON nội bộ của CME
        url = "https://www.cmegroup.com/CmeWS/mvc/XS/json/FedWatch/ALL"
        
        # Header ngụy trang giống hệt trình duyệt thật
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
            "Origin": "https://www.cmegroup.com",
            "X-Requested-With": "XMLHttpRequest"
        }
        
        # Timeout 10s để cố chờ dữ liệu về
        r = requests.get(url, headers=headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            # Lấy cuộc họp gần nhất (Meeting đầu tiên)
            meeting = data[0]
            prob_list = meeting['problist']
            
            # Tìm kịch bản có xác suất cao nhất
            best = max(prob_list, key=lambda x: float(x['probability']))
            
            prob_val = float(best['probability'])
            label = f"{best['min']}-{best['max']}" # Ví dụ 425-450
            
            # Tính thay đổi so với lần trước
            old_prob = CACHE['last_fed']['prob']
            change = prob_val - old_prob if old_prob > 0 else 0.0
            
            # Lưu cache
            CACHE['last_fed'] = {'label': label, 'prob': prob_val, 'change': change}
            
            return prob_val, label, change
            
    except Exception as e:
        print(f"CME Error: {e}")
    
    # Nếu lỗi, trả về dữ liệu cũ trong Cache chứ không trả về 0
    return CACHE['last_fed']['prob'], CACHE['last_fed']['label'], 0.0

# ==============================================================================
# 3. HÀM LẤY LẠM PHÁT (YAHOO -> FRED)
# ==============================================================================
def get_breakeven_hardcore(series_id):
    """
    Thử Yahoo trước. Nếu Yahoo chặn (trả về 0 hoặc rỗng), qua FRED lấy ngay.
    Không bao giờ trả về Yield.
    """
    y_sym = "^T10YIE" if series_id == "T10YIE" else "^T5YIE"
    
    # 1. Thử Yahoo
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{y_sym}?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        r = requests.get(url, headers=headers, timeout=5)
        d = r.json()
        closes = [c for c in d['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            return closes[-1], closes[-1] - closes[-2]
    except: pass
    
    # 2. Thử FRED (Nguồn chính phủ)
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df[series_id] != '.']
            df[series_id] = pd.to_numeric(df[series_id])
            if len(df) >= 2:
                curr = float(df.iloc[-1][series_id])
                prev = float(df.iloc[-2][series_id])
                return curr, curr - prev
    except: pass
    
    return 0.0, 0.0

# ==============================================================================
# 4. CÁC HÀM KHÁC (VÀNG, SPDR, VIX)
# ==============================================================================
def get_gold_binance():
    try:
        # Tăng độ lì lợm: Thử 2 lần nếu mạng lag
        for _ in range(2):
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT", timeout=8)
                d = r.json()
                k = requests.get("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=20", timeout=8)
                kd = k.json()
                
                closes = [float(x[4]) for x in kd]
                if len(closes) >= 15:
                    delta = pd.Series(closes).diff()
                    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / loss
                    rsi = 100 - (100 / (1 + rs))
                    curr_rsi = float(rsi.iloc[-1])
                else: curr_rsi = 50.0

                last = kd[-1]
                h1 = float(last[2]) - float(last[3])
                return {'p': float(d['lastPrice']), 'c': float(d['priceChange']), 'pct': float(d['priceChangePercent']), 'h1': h1, 'rsi': curr_rsi}
            except: time.sleep(1)
    except: pass
    return None

def get_yahoo_basic(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        data = r.json()
        closes = [c for c in data['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            return closes[-1], closes[-1] - closes[-2], (closes[-1] - closes[-2])/closes[-2]*100
    except: return None

def get_spdr():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8, verify=False)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skiprows=6)
            col = [c for c in df.columns if "Tonnes" in str(c)]
            if col:
                df = df.dropna(subset=[col[0]])
                return float(df.iloc[-1][col[0]]), float(df.iloc[-1][col[0]]) - float(df.iloc[-2][col[0]])
    except: pass
    return None

# ==============================================================================
# 5. TỔNG HỢP DỮ LIỆU
# ==============================================================================
def get_full_data():
    data = {}
    
    # 1. Vàng (Quan trọng nhất)
    data['gold'] = get_gold_binance()
    if not data['gold']: data['gold'] = {'p': 0, 'c': 0, 'pct': 0, 'h1': 0, 'rsi': 50}

    # 2. CME FedWatch (Cố lấy thật)
    fed_p, fed_lbl, fed_c = get_cme_fedwatch()
    data['fed'] = {'p': fed_p, 'lbl': fed_lbl, 'chg': fed_c}

    # 3. Lạm phát (Yahoo -> Fred)
    inf10_p, inf10_c = get_breakeven_hardcore("T10YIE")
    data['inf10'] = {'p': inf10_p, 'c': inf10_c}
    
    inf05_p, inf05_c = get_breakeven_hardcore("T5YIE")
    data['inf05'] = {'p': inf05_p, 'c': inf05_c}

    # 4. Risk (VIX/GVZ/SPDR)
    vix = get_yahoo_basic("^VIX")
    data['vix'] = {'p': vix[0], 'c': vix[1], 'pct': vix[2]} if vix else {'p': 0, 'c': 0, 'pct': 0}
    
    gvz = get_yahoo_basic("^GVZ")
    data['gvz'] = {'p': gvz[0], 'c': gvz[1], 'pct': gvz[2]} if gvz else {'p': 0, 'c': 0, 'pct': 0}
    
    spdr = get_spdr()
    data['spdr'] = {'v': spdr[0], 'c': spdr[1]} if spdr else {'v': 0, 'c': 0}

    return data

def send_tele(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                      json={"chat_id": CONFIG['TELEGRAM_CHAT_ID'], "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================================
# 6. ROUTING
# ==============================================================================
@app.route('/')
def home(): return "Bot V43 - Hardcore Data"

@app.route('/test')
def test():
    d = get_full_data()
    send_tele(f"🔔 TEST OK. Gold: {d['gold']['p']}")
    return "OK", 200

@app.route('/run_check')
def run_check():
    try:
        d = get_full_data()
        alerts = []
        now = time.time()
        
        # --- CẢNH BÁO ---
        if d['gold']['rsi'] > CONFIG['RSI_HIGH'] and d['gold']['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - CACHE['last_alert'].get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚀 <b>SIÊU TREND TĂNG:</b> RSI {d['gold']['rsi']:.0f} + H1 chạy {d['gold']['h1']:.1f}$")
                CACHE['last_alert']['RSI'] = now
        
        if d['gold']['rsi'] < CONFIG['RSI_LOW'] and d['gold']['h1'] > CONFIG['RSI_PRICE_MOVE']:
            if now - CACHE['last_alert'].get('RSI', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🩸 <b>SIÊU TREND GIẢM:</b> RSI {d['gold']['rsi']:.0f} + H1 sập {d['gold']['h1']:.1f}$")
                CACHE['last_alert']['RSI'] = now

        if d['gold']['h1'] > CONFIG['GOLD_H1_LIMIT']:
            if now - CACHE['last_alert'].get('H1', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🚨 <b>VÀNG SỐC:</b> H1 {d['gold']['h1']:.1f} giá")
                CACHE['last_alert']['H1'] = now
        
        if d['vix']['p'] > CONFIG['VIX_VAL_LIMIT'] or d['vix']['pct'] > CONFIG['VIX_PCT_LIMIT']:
             if now - CACHE['last_alert'].get('VIX', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"⚠️ <b>VIX BÁO ĐỘNG:</b> {d['vix']['p']:.2f}")
                CACHE['last_alert']['VIX'] = now
        
        # LẠM PHÁT
        if abs(d['inf10']['c']) > CONFIG['INF_10Y_LIMIT']:
            if now - CACHE['last_alert'].get('INF', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🇺🇸 <b>LẠM PHÁT SỐC:</b> Đổi {abs(d['inf10']['c']):.3f} điểm")
                CACHE['last_alert']['INF'] = now

        # FEDWATCH (Check % thay đổi > 15%)
        if abs(d['fed']['chg']) > CONFIG['FED_PCT_LIMIT']:
            if now - CACHE['last_alert'].get('FED', 0) > CONFIG['ALERT_COOLDOWN']:
                alerts.append(f"🏦 <b>FED ĐỔI KÈO:</b> {d['fed']['lbl']} đổi {d['fed']['chg']:.1f}%")
                CACHE['last_alert']['FED'] = now

        if alerts:
            send_tele(f"🔥🔥 <b>CẢNH BÁO KHẨN</b> 🔥🔥\n\n" + "\n".join(alerts))
            return "Alert Sent", 200

        # --- DASHBOARD (00-05, 30-35) ---
        vn_now = datetime.utcnow() + timedelta(hours=7)
        last_sent = CACHE.get('last_dash', 0)
        is_time = vn_now.minute in [0,1,2,3,4,5,30,31,32,33,34,35]
        
        if is_time and (now - last_sent > 1200):
            def s(v): return "+" if v >= 0 else ""
            def i(v): return "🟢" if v >= 0 else "🔴"
            
            spdr_txt = f"{d['spdr']['v']:.2f} tấn" if d['spdr']['v'] > 0 else "Chờ cập nhật"
            spdr_chg = f"({s(d['spdr']['c'])}{d['spdr']['c']:.2f})" if d['spdr']['v'] > 0 else ""
            
            be10_str = f"{d['inf10']['p']:.3f}%" if d['inf10']['p'] > 0 else "N/A"
            be05_str = f"{d['inf05']['p']:.3f}%" if d['inf05']['p'] > 0 else "N/A"
            
            # Hiển thị FedWatch
            if d['fed']['prob'] > 0:
                fed_str = f"Kịch bản {d['fed']['lbl']}: <b>{d['fed']['p']}%</b>"
            else:
                fed_str = "Đang tải từ CME..."
            
            vix_str = f"{d['vix']['p']:.2f}" if d['vix']['p'] > 0 else "N/A"
            gvz_str = f"{d['gvz']['p']:.2f}" if d['gvz']['p'] > 0 else "N/A"

            msg = (
                f"📊 <b>MARKET DASHBOARD (D1)</b>\n"
                f"Time: {vn_now.strftime('%H:%M')}\n"
                f"-------------------------------\n"
                f"🥇 <b>GOLD (PAXG):</b> {d['gold']['p']:.1f}\n"
                f"   {i(d['gold']['c'])} {s(d['gold']['c'])}{d['gold']['c']:.1f}$ ({s(d['gold']['pct'])}{d['gold']['pct']:.2f}%)\n"
                f"   🎯 <b>RSI (H1):</b> {d['gold']['rsi']:.1f}\n"
                f"-------------------------------\n"
                f"🐋 <b>SPDR Gold:</b> {spdr_txt} {spdr_chg}\n"
                f"-------------------------------\n"
                f"🇺🇸 <b>Lạm phát (Breakeven):</b>\n"
                f"   • 10Y: {be10_str} (Chg: {s(d['inf10']['c'])}{d['inf10']['c']:.3f})\n"
                f"   • 05Y: {be05_str} (Chg: {s(d['inf05']['c'])}{d['inf05']['c']:.3f})\n"
                f"-------------------------------\n"
                f"🏦 <b>CME FedWatch (Real):</b>\n"
                f"   • {fed_str}\n"
                f"-------------------------------\n"
                f"📉 <b>Risk:</b>\n"
                f"   • VIX: {vix_str}\n"
                f"   • GVZ: {gvz_str}\n"
            )
            send_tele(msg)
            CACHE['last_dash'] = now
            return "Report Sent", 200

        return "Checked", 200
    except Exception as e: return f"Err: {e}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
