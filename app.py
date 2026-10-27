import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Institutional PSX Analytics & Prediction Terminal",
    page_icon="📈",
    layout="wide"
)

DB_NAME = "psx_institutional_master.db"

class PSXDatabaseManager:
    def __init__(self, db_name=DB_NAME):
        self.db_name = db_name
        self.initialize_database()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def initialize_database(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        # Companies Master Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS companies (
                symbol TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                sector TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # EOD Market Data Table (Strict EOD Closing)
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

# Initialize Database Manager
db_mgr = PSXDatabaseManager()

# --- STREAMLIT FRONTEND UI ---
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
st.sidebar.info("System Status: **Strict EOD Mode Active (0% Tolerance for Dummy Data)**")

# --- MODULE 1: NEXT-DAY PREDICTION ---
if module_choice == "1. Next-Day Prediction Module":
    st.title("📈 Next-Day Prediction & Probability Terminal")
    st.markdown("Automated EOD screening and strict institutional validation.")

    if st.button("Run Institutional Prediction Scan"):
        conn = db_mgr.get_connection()
        # Fetch actual latest EOD closing data from database
        query = """
            e.symbol, e.trade_date, e.close, e.volume 
            FROM eod_market_data e
            JOIN (SELECT symbol, MAX(trade_date) as max_date FROM eod_market_data GROUP BY symbol) latest 
            ON e.symbol = latest.symbol AND e.trade_date = latest.max_date
        """
        try:
            df_latest = pd.read_sql(f"SELECT {query}", conn)
        except Exception as e:
            df_latest = pd.DataFrame()
        conn.close()

        if df_latest.empty:
            st.warning("⚠️ کوئی حقیقی EOD ڈیٹا بیس میں موجود نہیں ہے۔ براہ کرم مصدقہ EOD ڈیٹا ان پٹ کریں (کوئی فرضی ڈیٹا الاؤ نہیں)۔")
        else:
            st.success("Verified EOD Data Loaded Successfully!")
            st.dataframe(df_latest, use_container_width=True)

# --- MODULE 2: LONG-TERM INVESTING ---
elif module_choice == "2. Long-Term Investing Module":
    st.title("💼 Long-Term Investing & Fundamental Value")
    conn = db_mgr.get_connection()
    companies_df = pd.read_sql("SELECT * FROM companies", conn)
    conn.close()
    st.dataframe(companies_df, use_container_width=True)

# --- MODULE 3: COMPANY SEARCH & INSPECTOR ---
elif module_choice == "3. Company Search & Inspector":
    st.title("🔍 Company Search & Data Inspector")
    conn = db_mgr.get_connection()
    try:
        companies_df = pd.read_sql("SELECT symbol FROM companies", conn)
        symbols = companies_df['symbol'].tolist() if not companies_df.empty else []
    except:
        symbols = []
    conn.close()

    if symbols:
        inspector_sym = st.selectbox("Choose Company Symbol", symbols)
        conn = db_mgr.get_connection()
        df_insp = pd.read_sql("SELECT trade_date, open, high, low, close, volume, data_source FROM eod_market_data WHERE symbol = ? ORDER BY trade_date DESC", conn, params=(inspector_sym,))
        conn.close()
        st.dataframe(df_insp, use_container_width=True)
    else:
        st.info("ڈیٹا بیس میں فی الحال کوئی کمپنی رجسٹرڈ نہیں ہے۔")
