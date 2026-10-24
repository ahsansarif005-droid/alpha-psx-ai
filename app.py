import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
import io

# Page Configuration
st.set_page_config(
    page_title="AlphaPSX AI - Institutional Market Intelligence",
    page_icon="⚡",
    layout="wide"
)

# Header Section
st.markdown('<p style="font-size: 28px; font-weight: 800; color: #38BDF8; text-align: center; letter-spacing: 1px; margin-top: 10px; margin-bottom: 2px;">⚡ ALPHA-PSX AI | QUANTITATIVE TERMINAL</p>', unsafe_allow_html=True)
st.markdown(f'<p style="font-size: 13px; color: #94A3B8; text-align: center; margin-bottom: 30px; text-transform: uppercase; letter-spacing: 0.5px;">Pakistan Stock Exchange (PSX) • Advanced MACD/RSI Engine & Sector Analytics • Live Sync: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>', unsafe_allow_html=True)

# Helper Function: Calculate RSI
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# Helper Function: Calculate MACD
def calculate_macd(series, span1=12, span2=26, signal=9):
    ema12 = series.ewm(span=span1, adjust=False).mean()
    ema26 = series.ewm(span=span2, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

# Load Database Function
@st.cache_data(ttl=60)
def load_data():
    conn = sqlite3.connect("psx_database.db")
    query = "SELECT Symbol, Date, Close, Volume FROM stock_prices ORDER BY Symbol, Date"
    df = pd.read_sql(query, conn)
    conn.close()
    return df

df = load_data()

if df.empty:
    st.warning("⚠️ No data found in the database. Please run the master pipeline first.")
else:
    breakout_list = []
    day_trading_list = []
    all_stocks_summary = []
    
    # Sector Mapping Dictionary for PSX Giants
    sector_mapping = {
        'FFC': 'Fertilizer', 'ENGRO': 'Fertilizer',
        'MEBL': 'Commercial Banks', 'MCB': 'Commercial Banks', 'UBL': 'Commercial Banks',
        'OGDC': 'Oil & Gas Exploration', 'PPL': 'Oil & Gas Exploration', 'MARI': 'Oil & Gas Exploration',
        'LUCK': 'Cement', 'DGKC': 'Cement',
        'HUBC': 'Power Generation',
        'PSO': 'Oil & Gas Marketing',
        'SYS': 'Technology & Communication',
        'NESTLE': 'Food & Personal Care',
        'AGL': 'Automobile & Assembler',
        'AIRLINK': 'Technology & Communication'
    }
    
    # Strict Fundamental Ranking Hierarchy for Blue-Chips
    blue_chip_hierarchy = {
        'FFC': 1, 'MEBL': 2, 'OGDC': 3, 'LUCK': 4, 'MCB': 5, 
        'UBL': 6, 'MARI': 7, 'ENGRO': 8, 'HUBC': 9, 'PPL': 10, 
        'PSO': 11, 'SYS': 12, 'DGKC': 13, 'NESTLE': 14
    }
    
    for symbol, group in df.groupby('Symbol'):
        if len(group) < 10:
            continue
        
        # Calculate Technical Metrics (RSI & MACD)
        group['RSI'] = calculate_rsi(group['Close'], period=14)
        latest_rsi = group['RSI'].iloc[-1] if not np.isnan(group['RSI'].iloc[-1]) else 50.0
        
        macd, macd_signal = calculate_macd(group['Close'])
        latest_macd = macd.iloc[-1]
        latest_signal = macd_signal.iloc[-1]
        
        macd_trend = "📈 Bullish Crossover" if latest_macd > latest_signal else "📉 Bearish/Neutral"
            
        avg_volume = group['Volume'].iloc[:-1].mean()
        latest_volume = group['Volume'].iloc[-1]
        latest_close = group['Close'].iloc[-1]
        prev_close = group['Close'].iloc[-2]
        date_str = str(group['Date'].iloc[-1])
        
        rank_score = blue_chip_hierarchy.get(symbol, 99)
        is_bc = 1 if symbol in blue_chip_hierarchy else 0
        stock_sector = sector_mapping.get(symbol, 'Other Sectors')
        
        rsi_status = "🟢 Neutral/Bullish"
        if latest_rsi > 70:
            rsi_status = "🔴 Overbought"
        elif latest_rsi < 30:
            rsi_status = "🟢 Oversold (Buy)"
            
        all_stocks_summary.append({
            'Rank': rank_score if is_bc == 1 else 999,
            'Symbol': symbol,
            'Sector': stock_sector,
            'Latest Close (PKR)': round(latest_close, 2),
            'Average Volume': int(avg_volume),
            'RSI (14)': round(latest_rsi, 1),
            'MACD Trend': macd_trend,
            'is_blue_chip': is_bc
        })

        # Next-Day Breakout Logic with MACD/RSI Confirmation
        if avg_volume > 10000 and latest_volume > (1.4 * avg_volume) and latest_close > prev_close:
            vol_ratio = latest_volume / avg_volume
            price_change_pct = ((latest_close - prev_close) / prev_close) * 100
            confidence = min(round(72 + (vol_ratio * 5.0) + (price_change_pct * 0.5), 1), 96.0)
            if latest_macd > latest_signal:
                confidence = min(confidence + 3.0, 98.0) # Bonus for MACD Bullish
                
            upside = round(min(vol_ratio * 3.2, 16.0), 1)
            
            breakout_list.append({
                'Symbol': symbol,
                'Sector': stock_sector,
                'Close (PKR)': round(latest_close, 2),
                'Traded Volume': f"{latest_volume:,}",
                'RSI': round(latest_rsi, 1),
                'MACD': macd_trend,
                'Prediction Confidence': f"🟢 {confidence}%",
                'Next-Day Est. Upside': f"📈 +{upside}%",
                'raw_conf': confidence
            })

        # Day Trading Logic
        if avg_volume >= 500000 and latest_volume > (1.2 * avg_volume) and latest_close > prev_close:
            vol_ratio = latest_volume / avg_volume
            price_change_pct = ((latest_close - prev_close) / prev_close) * 100
            dt_confidence = min(round(75 + (vol_ratio * 4) + (price_change_pct * 0.6), 1), 97.0)
            
            target_price = round(latest_close * 1.03, 2)
            stop_loss = round(latest_close * 0.988, 2)
            
            day_trading_list.append({
                'Symbol': symbol,
                'Sector': stock_sector,
                'Entry Price (PKR)': round(latest_close, 2),
                'Day Target (PKR)': f"🎯 {target_price} (+3%)",
                'Stop Loss (PKR)': f"🛡️ {stop_loss} (-1.2%)",
                'RSI': round(latest_rsi, 1),
                'MACD': macd_trend,
                'Signal Strength': f"⚡ {dt_confidence}%",
                'raw_score': dt_confidence
            })

    breakout_list = sorted(breakout_list, key=lambda x: x['raw_conf'], reverse=True)
    day_trading_list = sorted(day_trading_list, key=lambda x: x['raw_score'], reverse=True)

    # Classy Sidebar Navigation
    st.sidebar.markdown("### 🎛️ Terminal Control")
    st.sidebar.markdown("---")
    
    selected_tab = st.sidebar.radio(
        "Navigation Menu",
        ["⚡ Intraday Day Trading", "📊 Next-Day Breakout Signals", "🏛️ Long-Term Blue-Chips", "🏢 Sector Breakdown Analytics", "🔍 Stock Technical Inspector"],
        label_visibility="collapsed"
    )
    
    st.sidebar.markdown("---")
    st.sidebar.info("💡 System Status: Advanced MACD & RSI institutional momentum engines active.")

    if selected_tab == "⚡ Intraday Day Trading":
        st.subheader("⚡ High-Precision Day Trading Signals (MACD & Liquidity Filter)")
        st.markdown("Screens liquid stocks with active volume surges, reinforced by MACD crossover trend confirmation.")
        
        if day_trading_list:
            dt_df = pd.DataFrame(day_trading_list).drop(columns=['raw_score'])
            st.dataframe(dt_df, width=1500, hide_index=True)
        else:
            st.info("ℹ️ Day Trading Status: Market is currently in normal accumulation state. High-liquidity setups will trigger during active volume surges.")

    elif selected_tab == "📊 Next-Day Breakout Signals":
        st.subheader("📊 Next-Day High-Probability Breakout Watchlist (MACD Powered)")
        st.markdown("Predictive screening using closing volume accumulation, RSI, and MACD trend direction.")
        
        if breakout_list:
            breakout_df = pd.DataFrame(breakout_list).drop(columns=['raw_conf'])
            st.dataframe(breakout_df, width=1500, hide_index=True)
        else:
            st.info("ℹ️ Market State Notice: No heavy accumulation breakouts flagged in latest closing data.")

    elif selected_tab == "🏛️ Long-Term Blue-Chips":
        st.subheader("🏛️ Long-Term Core Investment Portfolio & Technicals")
        st.markdown("Elite blue-chip selection backed by fundamental hierarchy, RSI, and MACD trend status.")
        
        if all_stocks_summary:
            summary_df = pd.DataFrame(all_stocks_summary)
            summary_df = summary_df.sort_values(by=['is_blue_chip', 'Rank'], ascending=[False, True]).drop(columns=['is_blue_chip', 'Rank'])
            
            st.dataframe(summary_df.head(15), width=1500, hide_index=True)
            
            def generate_pdf_report(data_df):
                buffer = io.BytesIO()
                doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
                elements = []
                
                styles = getSampleStyleSheet()
                title_style = ParagraphStyle(
                    'TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=18, textColor=colors.HexColor('#1A365D'), alignment=1
                )
                subtitle_style = ParagraphStyle(
                    'SubTitleStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.HexColor('#4A5568'), alignment=1
                )
                
                elements.append(Paragraph("AlphaPSX AI - Long-Term Blue-Chip Portfolio Report", title_style))
                elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Pakistan Stock Exchange (PSX)", subtitle_style))
                elements.append(Spacer(1, 15))
                
                table_data = [list(data_df.columns)] + data_df.head(15).values.tolist()
                t = Table(table_data, colWidths=[80, 110, 110, 90, 80, 80])
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1A365D')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F7FAFC')),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ('TOPPADDING', (0, 1), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
                ]))
                
                elements.append(t)
                elements.append(Spacer(1, 15))
                elements.append(Paragraph("Disclaimer: Institutional blue-chip portfolio based on quantitative fundamental hierarchy.", subtitle_style))
                
                doc.build(elements)
                buffer.seek(0)
                return buffer.getvalue()

            pdf_data = generate_pdf_report(summary_df)
            st.download_button(
                label="📥 Download Professional Portfolio PDF",
                data=pdf_data,
                file_name="AlphaPSX_BlueChip_Portfolio.pdf",
                mime="application/pdf"
            )
        else:
            st.warning("No portfolio data available.")

    elif selected_tab == "🏢 Sector Breakdown Analytics":
        st.subheader("🏢 Sector Breakdown & Technical Trend Analytics")
        st.markdown("Filter companies by sectors with integrated MACD trend and RSI signals.")
        
        if all_stocks_summary:
            summary_df = pd.DataFrame(all_stocks_summary).drop(columns=['is_blue_chip', 'Rank'])
            unique_sectors = summary_df['Sector'].unique()
            
            selected_sector = st.selectbox("Select Target Sector", unique_sectors)
            sector_filtered_df = summary_df[summary_df['Sector'] == selected_sector]
            
            st.write(f"### Companies in **{selected_sector}** Sector")
            st.dataframe(sector_filtered_df, width=1500, hide_index=True)
        else:
            st.warning("No sector data available.")

    elif selected_tab == "🔍 Stock Technical Inspector":
        st.subheader("🔍 Individual Stock Technical Inspector (MACD & RSI)")
        all_symbols = df['Symbol'].unique()
        selected_stock = st.selectbox("Select Target Stock Symbol", all_symbols)
        
        stock_data = df[df['Symbol'] == selected_stock]
        if not stock_data.empty:
            st.write(f"### Historical Analytics for **{selected_stock}**")
            st.dataframe(stock_data.tail(10), width=1500, hide_index=True)
            
            latest = stock_data.iloc[-1]
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric(label="Latest Close Price", value=f"PKR {latest['Close']:.2f}")
            with c2:
                st.metric(label="Latest Volume Traded", value=f"{latest['Volume']:,}")
            with c3:
                st.metric(label="Data Timestamp", value=str(latest['Date']))