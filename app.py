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
TOKEN = os.environ.get('TOKEN', '8309991075:AAFYyjFxQQ8CYECXPKeteeUBXQE3Mx2yfUo') 
CHAT_ID = os.environ.get('CHAT_ID', '5464507208') 

# Đã đổi GC=F thành XAUUSD=X để tăng ổn định
TICKERS = {
    'GOLD': 'XAUUSD=X', # Đã đổi sang Gold Spot Index (ổn định hơn Futures)
    'GVZ': '^GVZ',    
    'VIX': '^VIX',    
    'US10Y': '^TNX',  
    'FED_FUT': 'ZQ=F' 
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

async def send_telegram(message, is_alert=False):
    try:
        bot = telegram.Bot(token=TOKEN)
        sent_msg = await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML', disable_web_page_preview=True)
        if is_alert:
            try: await bot.pin_chat_message(chat_id=CHAT_ID, message_id=sent_msg.message_id)
            except: pass
    except Exception as e: print(f"Lỗi Telegram: {e}")

# --- 3. HÀM PHÂN TÍCH CHÍNH ---
def analyze_market():
    alerts = []
    report_lines = []
    
    # Gộp tải dữ liệu Daily và H1 vào 1 lần duy nhất để tối ưu và ổn định
    # Tải dữ liệu Daily (2d) và H1 (1d)
    try:
        # Lấy data Daily (cho % change)
        daily_data = yf.download(list(TICKERS.values()), period="2d", progress=False)['Close']
        # Lấy data H1 (cho Vàng spread)
        h1_data = yf.download(TICKERS['GOLD'], period="1d", interval="1h", progress=False)
    except Exception as e:
        alerts.append(f"❌ <b>LỖI TẢI DỮ LIỆU:</b> Không thể kết nối YFinance. Vui lòng kiểm tra lại dịch vụ.")
        report_lines.append(f"Lỗi: {e}")
        return alerts, "\n".join(report_lines)

    # --- HÀM TRUY CẬP DATA (Đảm bảo giá trị không phải NaN) ---
    def get_value(df, ticker, column='Close'):
        try:
            val = df.loc[:, ticker].iloc[-1]
            # Nếu giá trị là NaN (lỗi tải) thì trả về 0 để tránh crash
            return val if pd.notna(val) else 0.0
        except:
            return 0.0

    # --- Xử lý từng Mã ---

    # 1. GVZ (Biến động Vàng)
    try:
        gvz_now = get_value(daily_data, TICKERS['GVZ'])
        gvz_prev = daily_data.loc[:, TICKERS['GVZ']].iloc[-2] if len(daily_data.loc[:, TICKERS['GVZ']]) >= 2 else 0.0
        gvz_pct = ((gvz_now - gvz_prev) / gvz_prev) * 100 if gvz_prev != 0 else 0.0
        
        report_lines.append(f"🌊 <b>GVZ:</b> {gvz_now:.2f} ({gvz_pct:+.2f}%)")
        if gvz_pct > 10 or gvz_now > 25:
            alerts.append(f"⚠️ <b>GVZ BÁO ĐỘNG:</b> {gvz_now:.2f} (Tăng {gvz_pct:.1f}%)")
    except: pass

    # 2. VIX (Sợ hãi)
    try:
        vix_now = get_value(daily_data, TICKERS['VIX'])
        vix_prev = daily_data.loc[:, TICKERS['VIX']].iloc[-2] if len(daily_data.loc[:, TICKERS['VIX']]) >= 2 else 0.0
        vix_pct = ((vix_now - vix_prev) / vix_prev) * 100 if vix_prev != 0 else 0.0
        
        report_lines.append(f"😱 <b>VIX:</b> {vix_now:.2f} ({vix_pct:+.2f}%)")
        
        if vix_pct > 15 or vix_now > 30:
            alerts.append(f"⚠️ <b>VIX SỢ HÃI CAO:</b> {vix_now:.2f} (Tăng {vix_pct:.1f}%)")
    except: pass

    # 3. US10Y
    try:
        us10y_now_raw = get_value(daily_data, TICKERS['US10Y'])
        us10y_prev_raw = daily_data.loc[:, TICKERS['US10Y']].iloc[-2] if len(daily_data.loc[:, TICKERS['US10Y']]) >= 2 else 0.0
        
        us10y_now = us10y_now_raw / 10
        us10y_prev = us10y_prev_raw / 10
        change = us10y_now - us10y_prev
        
        report_lines.append(f"🇺🇸 <b>US10Y:</b> {us10y_now:.3f}% (Var: {change:+.3f})")
        if abs(change) > 0.25:
            alerts.append(f"⚠️ <b>LÃI SUẤT MỸ BIẾN ĐỘNG:</b> {change:+.3f} điểm")
    except: pass

    # 4. FEDWATCH (ZQ=F)
    try:
        fed_fut_now = get_value(daily_data, TICKERS['FED_FUT'])
        fed_fut_prev = daily_data.loc[:, TICKERS['FED_FUT']].iloc[-2] if len(daily_data.loc[:, TICKERS['FED_FUT']]) >= 2 else 100.0
        
        rate_now = 100 - fed_fut_now
        rate_prev = 100 - fed_fut_prev
        
        rate_pct_change = ((rate_now - rate_prev) / rate_prev) * 100 if rate_prev != 0 else 0.0
        
        report_lines.append(f"🏦 <b>Fed Expectation:</b> {rate_now:.2f}% ({rate_pct_change:+.1f}%)")

        if abs(rate_pct_change) > 5.0:
            trend = "TĂNG" if rate_pct_change > 0 else "GIẢM"
            alerts.append(f"🏦 <b>FED PIVOT:</b> Kỳ vọng lãi suất {trend} mạnh ({abs(rate_pct_change):.1f}%)")
    except: pass

    # 5. XAUUSD (Nến H1)
    try:
        # Sử dụng h1_data đã tải riêng cho Gold Spot
        if not h1_data.empty:
            spread = h1_data['High'].iloc[-1] - h1_data['Low'].iloc[-1]
            current = h1_data['Close'].iloc[-1]
            pips = spread * 10 
            
            report_lines.append(f"🥇 <b>GOLD:</b> {current:.1f} (H1: {spread:.1f}$ ~ {pips:.0f} pips)")
            
            if spread > 40.0: # 40$ spread = 400 pips
                alerts.append(f"⚠️ <b>VÀNG CHẠY MẠNH (H1):</b> {spread:.1f}$ (~{pips:.0f} pips)")
        else:
            report_lines.append(f"🥇 <b>GOLD:</b> N/A (Lỗi tải H1)")
    except: 
        report_lines.append(f"🥇 <b>GOLD:</b> N/A (Lỗi xử lý)")
        
    # 6. SPDR
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

    # 7. CHECK TIN TỨC
    news_alerts = check_geopolitics_news()
    if news_alerts: alerts.extend(news_alerts)

    return alerts, "\n".join(report_lines)

@app.route('/run_bot')
def run_bot():
    alerts, report = analyze_market()
    now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    
    if alerts:
        msg = "🚨 <b>CẢNH BÁO RỦI RO</b> 🚨\n\n" + "\n".join(alerts)
        asyncio.run(send_telegram(msg, is_alert=True))
    
    if now.minute == 0: 
        msg = f"📊 <b>MARKET UPDATE</b> ({now.strftime('%H:%M')})\n{'-'*20}\n{report}\n{'-'*20}\n<i>Bot check news & risk every min</i>"
        asyncio.run(send_telegram(msg, is_alert=False))
        return "Sent Report"
    
    return "Checked"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
