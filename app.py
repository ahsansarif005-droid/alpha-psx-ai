import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from sklearn.ensemble import RandomForestClassifier

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Institutional PSX Analytics & Prediction Terminal",
    page_icon="📈",
    layout="wide"
)

DB_NAME = "psx_institutional_master.db"

class PSXManualPortalTerminal:
    def __init__(self, db_name=DB_NAME):
        self.db_name = db_name
        self.initialize_master_database()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def initialize_master_database(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS companies (
                symbol TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                sector TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS eod_market_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_date DATE NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                traded_value REAL,
                data_source TEXT NOT NULL,
                ingestion_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (symbol) REFERENCES companies (symbol),
                UNIQUE(symbol, trade_date)
            )
        ''')
        conn.commit()
        conn.close()

terminal = PSXManualPortalTerminal()

# --- STREAMLIT FRONTEND UI (VERTICAL LAYOUT) ---
st.sidebar.title("📊 PSX Institutional Terminal")
st.sidebar.markdown("---")
module_choice = st.sidebar.radio(
    "Select Operation Module:",
    (
        "1. Next-Day Prediction Module",
        "2. Long-Term Investing Module",
        "3. Company Search & Inspector"
    )
)

st.sidebar.markdown("---")
st.sidebar.info("System Status: **Manual File Portal & Top-10/15 Filter Active**")

# --- MODULE 1: NEXT-DAY PREDICTION ---
if module_choice == "1. Next-Day Prediction Module":
    st.title("📈 Institutional Portal & AI Prediction Engine")
    st.markdown("Upload verified EOD historical files, secure deep archive in database, and run advanced ML target screening.")
    st.markdown("---")

    # SECTION 1: FILE UPLOADER PORTAL (Top to Bottom)
    st.subheader("📁 Step 1: Upload Historical / Daily EOD Data File")
    st.markdown("اپنا مصدقہ تاریخی یا روزانہ کا EOD ڈیٹا (CSV یا Excel فارمیٹ میں) یہاں اپ لوڈ کریں۔ فائل کے کالمز میں `symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume` شامل ہونے چاہئیں[span_3](start_span)[span_3](end_span)۔")
    
    uploaded_file = st.file_uploader("Choose CSV or Excel file", type=["csv", "xlsx"])
    
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith('.csv'):
                df_upload = pd.read_csv(uploaded_file)
            else:
                df_upload = pd.read_excel(uploaded_file)
            
            st.success(f"فائل کامیابی سے پڑھ لی گئی ہے! کل رکارڈز: {len(df_upload)}")
            
            if st.button("Save Uploaded Data to Permanent Database"):
                conn = terminal.get_connection()
                cursor = conn.cursor()
                
                saved_count = 0
                for _, row in df_upload.iterrows():
                    sym = str(row.get('symbol')).upper().strip()
                    t_date = str(row.get('trade_date'))[:10]
                    open_p = float(row.get('open', 0))
                    high_p = float(row.get('high', 0))
                    low_p = float(row.get('low', 0))
                    close_p = float(row.get('close', 0))
                    vol = int(row.get('volume', 0))
                    val = vol * close_p
                    
                    # Ensure company exists in master
                    cursor.execute("INSERT OR IGNORE INTO companies (symbol, company_name, sector) VALUES (?, ?, ?)", (sym, sym, "General"))
                    
                    cursor.execute('''
                        INSERT OR IGNORE INTO eod_market_data 
                        (symbol, trade_date, open, high, low, close, volume, traded_value, data_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (sym, t_date, open_p, high_p, low_p, close_p, vol, val, "MANUAL_PORTAL_UPLOAD"))
                    saved_count += 1
                
                conn.commit()
                conn.close()
                st.success(f"کامیابی کے ساتھ {saved_count} رکارڈز ڈیٹا بیس میں مستقل محفوظ کر دیے گئے ہیں!")
        except Exception as e:
            st.error(f"فائل پروسیسنگ میں خرابی: {e}")

    st.markdown("---")

    # SECTION 2: AI PREDICTION SCAN (Top to Bottom)
    st.subheader("🤖 Step 2: Run AI Prediction & Top 10-15 Screening")
    st.markdown("ڈیٹا بیس میں موجود تمام کمپنیوں کا تجزیہ کریں اور اگلے دن کے لیے +5% ٹارگٹ والی ٹاپ 10 سے 15 بہترین کمپنیوں کی فہرست حاصل کریں[span_4](start_span)[span_4](end_span)۔")

    if st.button("Run AI Prediction & Filter Top Stocks"):
        with st.spinner("Running Machine Learning algorithms and filtering top conviction stocks..."):
            conn = terminal.get_connection()
            try:
                companies = pd.read_sql("SELECT symbol FROM companies WHERE is_active = 1", conn)
            except:
                companies = pd.DataFrame()

            if companies.empty:
                st.warning("⚠️ ڈیٹا بیس میں کوئی کمپنی موجود نہیں۔ براہ کرم پہلے اوپر فائل اپ لوڈ کریں۔")
            else:
                all_predictions = []
                for sym in companies['symbol']:
                    df = pd.read_sql("SELECT * FROM eod_market_data WHERE symbol = ? ORDER BY trade_date ASC", conn, params=(sym,))
                    if len(df) < 30:
                        continue
                    
                    # Technical Indicators
                    df['MA20'] = df['close'].rolling(20).mean()
                    df['MA50'] = df['close'].rolling(50).mean()
                    
                    delta = df['close'].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / loss
                    df['RSI'] = 100 - (100 / (1 + rs))
                    
                    df['Volume_MA20'] = df['volume'].rolling(20).mean()
                    df['RVOL'] = df['volume'] / df['Volume_MA20']
                    
                    # Target: Next day close >= current close * 1.05 (+5%)
                    df['target'] = (df['close'].shift(-1) >= df['close'] * 1.05).astype(int)
                    df_clean = df.dropna()

                    if len(df_clean) > 20:
                        X = df_clean[['close', 'volume', 'RSI', 'RVOL', 'MA20', 'MA50']]
                        y = df_clean['target']
                        
                        model = RandomForestClassifier(n_estimators=100, random_state=42)
                        model.fit(X, y)
                        
                        latest_row = df.iloc[-1]
                        latest_features = X.tail(1)
                        prob = float(model.predict_proba(latest_features)[0][1]) * 100

                        current_price = float(latest_row['close'])
                        target_price = round(current_price * 1.05, 2)

                        all_predictions.append({
                            "Symbol": sym,
                            "Latest Date": latest_row['trade_date'],
                            "Current Price (Rs)": round(current_price, 2),
                            "Target Price (+5% Rs)": target_price,
                            "AI Probability (%)": round(prob, 2),
                            "RSI (14)": round(latest_row['RSI'], 1),
                            "RVOL": round(latest_row['RVOL'], 2)
                        })
                conn.close()

                if all_predictions:
                    res_df = pd.DataFrame(all_predictions)
                    # Sort by highest AI Probability and pick Top 10-15
                    res_df = res_df.sort_values(by="AI Probability (%)", ascending=False).head(15)
                    res_df['AI Probability (%)'] = res_df['AI Probability (%)'].astype(str) + "%"
                    res_df['Signal'] = "HIGH CONVICTION BUY"
                    
                    st.markdown("---")
                    st.subheader("🏆 Top High-Probability Prediction Report (Top 15)")
                    st.success("AI Prediction Scan Completed Successfully!")
                    st.dataframe(res_df, use_container_width=True)
                    
                    # Export/Download Option for Report
                    csv_data = res_df.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label="📥 Download Prediction Report (CSV/Excel Compatible)",
                        data=csv_data,
                        file_name=f"PSX_Top_Predictions_{datetime.today().strftime('%Y-%m-%d')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.warning("تجزیے کے لیے درکار تاریخی ڈیٹا ناکافی ہے۔")

# --- MODULE 2: LONG-TERM INVESTING ---
elif module_choice == "2. Long-Term Investing Module":
    st.title("💼 Long-Term Deep Archive & Fundamental Value")
    conn = terminal.get_connection()
    try:
        companies_df = pd.read_sql("SELECT * FROM companies", conn)
    except:
        companies_df = pd.DataFrame()
    conn.close()
    if not companies_df.empty:
        st.dataframe(companies_df, use_container_width=True)

# --- MODULE 3: COMPANY SEARCH & INSPECTOR ---
elif module_choice == "3. Company Search & Inspector":
    st.title("🔍 Data Inspector & Archive Viewer")
    conn = terminal.get_connection()
    try:
        companies_df = pd.read_sql("SELECT symbol FROM companies", conn)
        symbols = companies_df['symbol'].tolist() if not companies_df.empty else []
    except:
        symbols = []
    conn.close()

    if symbols:
        inspector_sym = st.selectbox("Choose Company Symbol", symbols)
        conn = terminal.get_connection()
        df_insp = pd.read_sql("SELECT trade_date, open, high, low, close, volume, data_source FROM eod_market_data WHERE symbol = ? ORDER BY trade_date DESC", conn, params=(inspector_sym,))
    conn.close()
    if not df_insp.empty:
        st.dataframe(df_insp, use_container_width=True)
        st.line_chart(df_insp.set_index('trade_date')['close'].iloc[::-1])
    else:
        st.warning("اس کمپنی کا کوئی ریکارڈ موجود نہیں۔")
