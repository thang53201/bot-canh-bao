import telegram
import asyncio
import yfinance as yf
import pandas as pd
import requests
import io
from flask import Flask
from datetime import datetime
import pytz
import os
from bs4 import BeautifulSoup

app = Flask(__name__)

# --- CẤU HÌNH ---
# Khuyến nghị: Điền TOKEN và CHAT_ID vào Environment Variables trên Render
TOKEN = os.environ.get('TOKEN', '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo') 
CHAT_ID = os.environ.get('CHAT_ID', '5464507208') 

# Danh sách Ticker
TICKERS = {
    'GOLD': 'GC=F',   # Vàng tương lai
    'GVZ': '^GVZ',    # Biến động Vàng
    'VIX': '^VIX',    # Biến động CK Mỹ
    'US10Y': '^TNX',  # Lợi suất 10 năm
    'FED_FUT': 'ZQ=F' # Fed Funds Futures
}

# --- 1. HÀM DATA SPDR ---
def get_spdr_data():
    try:
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
        s = requests.get(url, verify=False, timeout=5).content
        df = pd.read_csv(io.BytesIO(s), skiprows=1)
        df = df[['Date', 'Total Net Asset Value Tonnes']].dropna().tail(5)
        return df
    except: return None

# --- 2. HÀM QUÉT TIN TỨC ---
def check_geopolitics_news():
    try:
        # RSS Google News (Topic: World/War/Geopolitics)
        url = "https://news.google.com/rss/topics/CAAqJggBCiJCAQAqSVgQASowCacGJQindUBKX/sections/CAQiSkIBCipJWUABKh0ICjIJY29tOmlkOnduL2JtL21pbGl0YXJ5X3dhcgoXCAoiCWNvbTppZDp3bi9ibS9taWxpdGFyeV93YXI?hl=en-US&gl=US&ceid=US%3Aen"
        response = requests.get(url, timeout=5)
        soup = BeautifulSoup(response.content, features="xml")
        items = soup.findAll('item')
        
        keywords = ['nuclear', 'missile', 'invasion', 'airstrike', 'war declared', 'conflict escalation', 'biden', 'putin', 'iran', 'israel']
        news_alerts = []
        
        for item in items[:3]:
            title = item.title.text.lower()
            for key in keywords:
                if key in title:
                    orig_link = item.link.text
                    news_alerts.append(f"📰 <b>TIN NÓNG ({key.upper()}):</b>\n{item.title.text}\n(<a href='{orig_link}'>Xem chi tiết</a>)")
                    break 
        return news_alerts
    except: return []

# --- 3. HÀM LẤY DATA THỊ TRƯỜNG ---
def get_market_data():
    return yf.download(list(TICKERS.values()), period="5d", interval="1h", progress=False)

async def send_telegram(message, is_alert=False):
    try:
        bot = telegram.Bot(token=TOKEN)
        sent_msg = await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML', disable_web_page_preview=True)
        if is_alert:
            try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=sent_msg.message_id)
            except: pass
    except Exception as e: print(f"Lỗi Telegram: {e}")

def analyze_market():
    alerts = []
    report_lines = []
    
    # Lấy dữ liệu nến H1 (cho Vàng)
    df = get_market_data()
    # Lấy dữ liệu Daily (cho các chỉ số % thay đổi)
    daily = yf.download(list(TICKERS.values()), period="2d", progress=False)['Close']

    # --- 1. GVZ (Biến động Vàng) ---
    try:
        gvz_now = daily[TICKERS['GVZ']].iloc[-1]
        gvz_prev = daily[TICKERS['GVZ']].iloc[-2]
        gvz_pct = ((gvz_now - gvz_prev) / gvz_prev) * 100
        
        report_lines.append(f"🌊 <b>GVZ:</b> {gvz_now:.2f} ({gvz_pct:+.2f}%)")
        
        if gvz_pct > 10 or gvz_now > 25:
            alerts.append(f"⚠️ <b>GVZ BÁO ĐỘNG:</b> {gvz_now:.2f} (Tăng {gvz_pct:.1f}%)")
    except: pass

    # --- 2. VIX (Sợ hãi) ---
    try:
        vix_now = daily[TICKERS['VIX']].iloc[-1]
        vix_prev = daily[TICKERS['VIX']].iloc[-2]
        vix_pct = ((vix_now - vix_prev) / vix_prev) * 100
        
        report_lines.append(f"😱 <b>VIX:</b> {vix_now:.2f} ({vix_pct:+.2f}%)")
        
        if vix_pct > 15 or vix_now > 30: # Yêu cầu: >15% hoặc >30
            alerts.append(f"⚠️ <b>VIX SỢ HÃI CAO:</b> {vix_now:.2f} (Tăng {vix_pct:.1f}%)")
    except: pass

    # --- 3. US10Y ---
    try:
        us10y_now = daily[TICKERS['US10Y']].iloc[-1] / 10
        us10y_prev = daily[TICKERS['US10Y']].iloc[-2] / 10
        change = us10y_now - us10y_prev
        
        report_lines.append(f"🇺🇸 <b>US10Y:</b> {us10y_now:.3f}% (Var: {change:+.3f})")
        if abs(change) > 0.25:
            alerts.append(f"⚠️ <b>LÃI SUẤT MỸ BIẾN ĐỘNG:</b> {change:+.3f} điểm")
    except: pass

    # --- 4. FEDWATCH (Dựa trên ZQ=F) ---
    try:
        # Tính Lãi suất kỳ vọng (Implied Rate) = 100 - Giá
        rate_now = 100 - daily[TICKERS['FED_FUT']].iloc[-1]
        rate_prev = 100 - daily[TICKERS['FED_FUT']].iloc[-2]
        
        # Tính % thay đổi của lãi suất
        rate_pct_change = ((rate_now - rate_prev) / rate_prev) * 100
        
        report_lines.append(f"🏦 <b>Fed Expectation:</b> {rate_now:.2f}% ({rate_pct_change:+.1f}%)")

        # CẢNH BÁO: Nếu kỳ vọng lãi suất thay đổi > 5% (Ví dụ 4.0% -> 4.2%)
        if abs(rate_pct_change) > 5.0:
            trend = "TĂNG" if rate_pct_change > 0 else "GIẢM"
            alerts.append(f"🏦 <b>FED PIVOT:</b> Kỳ vọng lãi suất {trend} mạnh ({abs(rate_pct_change):.1f}%)")
    except: pass

    # --- 5. XAUUSD (Nến H1) ---
    try:
        gold_h1 = df.xs(TICKERS['GOLD'], level=1, axis=1).iloc[-1]
        spread = gold_h1['High'] - gold_h1['Low']
        current = gold_h1['Close']
        
        # Logic: 1$ = 10 pips. 400 pips = 40$.
        pips = spread * 10 
        
        report_lines.append(f"🥇 <b>GOLD:</b> {current:.1f} (H1: {spread:.1f}$ ~ {pips:.0f} pips)")
        
        if spread > 40.0: # 40$ spread = 400 pips
            alerts.append(f"⚠️ <b>VÀNG CHẠY MẠNH (H1):</b> {spread:.1f}$ (~{pips:.0f} pips)")
    except: pass
    
    # --- 6. SPDR ---
    try:
        spdr_df = get_spdr_data()
        if spdr_df is not None:
            today = float(spdr_df.iloc[-1]['Total Net Asset Value Tonnes'])
            chg = today - float(spdr_df.iloc[-2]['Total Net Asset Value Tonnes'])
            report_lines.append(f"🐳 <b>SPDR:</b> {today:.2f} tấn ({chg:+.2f} tấn)")
            
            if abs(chg) > 5:
                act = "GOM" if chg > 0 else "XẢ"
                alerts.append(f"⚠️ <b>CÁ VOI SPDR {act}:</b> {abs(chg):.2f} TẤN")
            
            last3 = spdr_df.tail(4)['Total Net Asset Value Tonnes'].diff().dropna().tail(3)
            if all(x > 0 for x in last3): alerts.append("⚠️ <b>SPDR:</b> Mua ròng 3 ngày")
            elif all(x < 0 for x in last3): alerts.append("⚠️ <b>SPDR:</b> Bán ròng 3 ngày")
    except: report_lines.append("SPDR: N/A")

    # --- 7. CHECK TIN TỨC ---
    news_alerts = check_geopolitics_news()
    if news_alerts: alerts.extend(news_alerts)

    return alerts, "\n".join(report_lines)

@app.route('/run_bot')
def run_bot():
    alerts, report = analyze_market()
    now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    
    # 1. Có biến -> Gửi Alert ngay lập tức
    if alerts:
        msg = "🚨 <b>CẢNH BÁO RỦI RO</b> 🚨\n\n" + "\n".join(alerts)
        asyncio.run(send_telegram(msg, is_alert=True))
    
    # 2. Đầu mỗi tiếng (phút 00) -> Gửi Report
    if now.minute == 0: 
        msg = f"📊 <b>MARKET UPDATE</b> ({now.strftime('%H:%M')})\n{'-'*20}\n{report}\n{'-'*20}\n<i>Bot check news & risk every min</i>"
        asyncio.run(send_telegram(msg, is_alert=False))
        return "Sent Report"
    
    return "Checked"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
