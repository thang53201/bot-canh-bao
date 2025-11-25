import yfinance as yf
import ccxt
import pandas as pd
import pandas_ta as ta
import time
from datetime import datetime, timedelta

# ================= CẤU HÌNH (CONFIG) =================

# 1. CẤU HÌNH THỜI GIAN
CHECK_INTERVAL_SECONDS = 300    # Check mỗi 5 phút (300s)
REPORT_INTERVAL_MINUTES = 30    # Gửi báo cáo định kỳ mỗi 30 phút
BREAKEVEN_CHECK_HOUR = 7        # Giờ check Breakeven (7h sáng)

# 2. NGƯỠNG CẢNH BÁO (ALERTS)
# VIX & GVZ
VIX_LIMIT = 30
VIX_CHANGE_PCT = 15.0
GVZ_LIMIT = 25
GVZ_CHANGE_PCT = 10.0

# Vàng (Gold)
RSI_UPPER = 80
RSI_LOWER = 20
CANDLE_H1_SIZE = 40.0

# FedWatch (Dùng ^IRX làm tham chiếu)
FED_RATE_CHANGE_PCT = 15.0      # Báo nếu kỳ vọng lãi suất đổi 15%

# ================= HÀM XỬ LÝ DỮ LIỆU =================

def get_gold_realtime():
    """Lấy dữ liệu Vàng từ Binance (Nhanh, chuẩn)"""
    try:
        exchange = ccxt.binance()
        ohlcv = exchange.fetch_ohlcv('PAXG/USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        df['rsi'] = ta.rsi(df['close'], length=14)
        current = df.iloc[-1]
        candle_size = current['high'] - current['low']
        
        return {
            'price': current['close'],
            'rsi': current['rsi'],
            'candle_size': candle_size
        }
    except:
        return None

def get_market_data(check_breakeven=False):
    """
    Lấy VIX, GVZ, IRX (Fed Proxy).
    check_breakeven=True thì mới lấy dữ liệu Lạm phát.
    """
    try:
        # ^IRX là Lợi suất trái phiếu 13 tuần (Proxy tốt nhất cho lãi suất FED ngắn hạn)
        symbols = "^VIX ^GVZ ^IRX" 
        if check_breakeven:
            symbols += " ^T10YIE" # Thêm Breakeven nếu đến giờ check

        data = yf.download(symbols, period="5d", progress=False)
        
        def get_val(sym):
            try:
                s = data['Close'][sym].dropna()
                if s.empty: return 0, 0
                curr, prev = s.iloc[-1], s.iloc[-2]
                chg = ((curr - prev) / prev) * 100
                return curr, chg
            except: return 0, 0

        vix, vix_chg = get_val('^VIX')
        gvz, gvz_chg = get_val('^GVZ')
        irx, irx_chg = get_val('^IRX') # Fed Rate Sentiment
        
        result = {
            'vix': vix, 'vix_chg': vix_chg,
            'gvz': gvz, 'gvz_chg': gvz_chg,
            'fed_proxy': irx, 'fed_chg': irx_chg
        }

        if check_breakeven:
            be, be_chg = get_val('^T10YIE')
            result['breakeven'] = be
        
        return result
    except Exception as e:
        print(f"Lỗi Yahoo: {e}")
        return None

# ================= LOGIC CHÍNH =================

print("=== BOT V29: FEDWATCH & SCHEDULED REPORT STARTED ===")
print(f"- Check mỗi {CHECK_INTERVAL_SECONDS/60} phút.")
print(f"- Báo cáo tổng hợp mỗi {REPORT_INTERVAL_MINUTES} phút.")
print(f"- Breakeven check lúc {BREAKEVEN_CHECK_HOUR}:00 hàng ngày.")

last_report_time = datetime.now() - timedelta(minutes=REPORT_INTERVAL_MINUTES) # Để chạy ngay lần đầu
breakeven_data_cached = "Chưa cập nhật" # Lưu kết quả Breakeven để hiển thị lại

while True:
    now = datetime.now()
    alerts = []
    
    # 1. QUYẾT ĐỊNH CÓ CHECK BREAKEVEN KHÔNG?
    # Chỉ check nếu đang ở giờ quy định (ví dụ 7h00 - 7h05)
    do_check_breakeven = False
    if now.hour == BREAKEVEN_CHECK_HOUR and now.minute < 10:
        do_check_breakeven = True
        
    # 2. LẤY DỮ LIỆU
    gold = get_gold_realtime()
    market = get_market_data(check_breakeven=do_check_breakeven)
    
    # Cập nhật cache Breakeven nếu vừa lấy được
    if market and 'breakeven' in market:
        breakeven_data_cached = f"{market['breakeven']:.2f}%"

    # 3. KIỂM TRA CẢNH BÁO (ALERTS) - BÁO NGAY LẬP TỨC
    if gold and market:
        # --- Check Vàng ---
        if gold['rsi'] >= RSI_UPPER:
            alerts.append(f"🔥 RSI VÀNG NÓNG: {gold['rsi']:.1f} (>=80)")
        if gold['rsi'] <= RSI_LOWER:
            alerts.append(f"❄️ RSI VÀNG LẠNH: {gold['rsi']:.1f} (<=20)")
        if gold['candle_size'] >= CANDLE_H1_SIZE:
            alerts.append(f"⚡ VÀNG GIẬT MẠNH: Nến H1 chạy {gold['candle_size']:.1f} giá")
            
        # --- Check VIX/GVZ ---
        if market['vix'] >= VIX_LIMIT or market['vix_chg'] >= VIX_CHANGE_PCT:
            alerts.append(f"☠️ VIX BÁO ĐỘNG: {market['vix']:.2f} (+{market['vix_chg']:.1f}%)")
        if market['gvz'] >= GVZ_LIMIT or market['gvz_chg'] >= GVZ_CHANGE_PCT:
            alerts.append(f"⚠️ GVZ BÁO ĐỘNG: {market['gvz']:.2f} (+{market['gvz_chg']:.1f}%)")
            
        # --- Check Fed Expectation (^IRX) ---
        if abs(market['fed_chg']) >= FED_RATE_CHANGE_PCT:
             alerts.append(f"🏦 FED WATCH: Kỳ vọng lãi suất biến động mạnh ({market['fed_chg']:.1f}%)!")

    # 4. XỬ LÝ GỬI TIN
    # A. Nếu có CẢNH BÁO KHẨN -> Gửi ngay lập tức
    if alerts:
        print(f"\n[{now.strftime('%H:%M')}] 🚨 PHÁT HIỆN TÍN HIỆU:")
        for msg in alerts:
            print(f"- {msg}")
            # CODE GỬI TELEGRAM KHẨN Ở ĐÂY
    
    # B. Nếu không có cảnh báo -> Kiểm tra xem đã đến giờ gửi Báo cáo định kỳ chưa?
    elif (now - last_report_time).total_seconds() >= (REPORT_INTERVAL_MINUTES * 60):
        # Tạo nội dung báo cáo
        r = gold['rsi'] if gold else 0
        p = gold['price'] if gold else 0
        v = market['vix'] if market else 0
        g = market['gvz'] if market else 0
        f = market['fed_proxy'] if market else 0
        
        report = (
            f"\n[{now.strftime('%H:%M')}] 📊 BÁO CÁO ĐỊNH KỲ (30p):\n"
            f"--------------------------\n"
            f"• Vàng: {p:.1f}$ | RSI: {r:.1f}\n"
            f"• Risk: VIX {v:.1f} | GVZ {g:.1f}\n"
            f"• Fed Watch (IRX): {f:.2f}%\n"
            f"• Lạm phát (BE): {breakeven_data_cached}\n"
            f"--------------------------"
        )
        print(report)
        # CODE GỬI TELEGRAM REPORT Ở ĐÂY
        
        # Reset thời gian
        last_report_time = now
        
    else:
        # In dòng trạng thái chờ (cho bạn biết code vẫn chạy)
        print(f"Checking... (Next Report: {((REPORT_INTERVAL_MINUTES*60) - (now - last_report_time).total_seconds())/60:.0f} min)", end="\r")

    # 5. NGỦ (Sleep)
    time.sleep(CHECK_INTERVAL_SECONDS)
