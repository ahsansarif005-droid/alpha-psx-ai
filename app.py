import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score
from sklearn.model_selection import TimeSeriesSplit

# ============================================================
# PSX EOD ANALYTICS — safe starter version
# IMPORTANT:
# - This app does NOT pretend to fetch live PSX data.
# - Import data from a licensed/API source or CSV until a real
#   provider connector is configured.
# - It will not display fabricated prices/probabilities.
# ============================================================

st.set_page_config(
    page_title="PSX EOD Analytics & Prediction Terminal",
    page_icon="📈",
    layout="wide",
)

DB_NAME = "psx_institutional_master.db"
PAKISTAN_TZ = ZoneInfo("Asia/Karachi")
MARKET_FETCH_AFTER = time(17, 0)


def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database():
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                symbol TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                sector TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                source TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eod_market_data (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume INTEGER,
                traded_value REAL,
                data_source TEXT NOT NULL,
                ingestion_timestamp TEXT NOT NULL,
                PRIMARY KEY (symbol, trade_date),
                FOREIGN KEY (symbol) REFERENCES companies(symbol)
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                reference_close REAL NOT NULL,
                target_probability REAL,
                target_definition TEXT NOT NULL,
                model_version TEXT NOT NULL,
                note TEXT
            );
            """
        )


def now_pakistan():
    return datetime.now(PAKISTAN_TZ)


def after_market_time():
    return now_pakistan().time().replace(tzinfo=None) >= MARKET_FETCH_AFTER


def normalize_symbol(value):
    return str(value).strip().upper()


def upsert_companies(uploaded_df, source_label):
    required = {"symbol", "company_name"}
    missing = required - set(uploaded_df.columns)
    if missing:
        raise ValueError("Company file must contain columns: symbol, company_name. Optional: sector, is_active.")

    df = uploaded_df.copy()
    df["symbol"] = df["symbol"].map(normalize_symbol)
    df["company_name"] = df["company_name"].astype(str).str.strip()
    if "sector" not in df.columns:
        df["sector"] = ""
    if "is_active" not in df.columns:
        df["is_active"] = 1

    df["is_active"] = pd.to_numeric(df["is_active"], errors="coerce").fillna(1).astype(int)
    df["updated_at"] = now_pakistan().isoformat()
    df["source"] = source_label
    df = df[(df["symbol"] != "") & (df["company_name"] != "")]
    df = df.drop_duplicates(subset=["symbol"], keep="last")

    with get_connection() as conn:
        for row in df[["symbol", "company_name", "sector", "is_active", "source", "updated_at"]].itertuples(index=False, name=None):
            conn.execute(
                """
                INSERT INTO companies (symbol, company_name, sector, is_active, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company_name=excluded.company_name,
                    sector=excluded.sector,
                    is_active=excluded.is_active,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                row,
            )
    return len(df)


def import_eod_data(uploaded_df, source_label):
    """Import CSV with columns: symbol, trade_date, close; OHLCV columns are optional."""
    df = uploaded_df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"symbol", "trade_date", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("EOD file must contain: symbol, trade_date, close. Optional: open, high, low, volume, traded_value.")

    for col in ["open", "high", "low", "volume", "traded_value"]:
        if col not in df.columns:
            df[col] = np.nan

    df["symbol"] = df["symbol"].map(normalize_symbol)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    numeric_cols = ["open", "high", "low", "close", "volume", "traded_value"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["symbol", "trade_date", "close"])
    df = df[df["close"] > 0]
    # Reject impossible OHLC rows when all values are present.
    has_ohlc = df[["open", "high", "low"]].notna().all(axis=1)
    invalid_ohlc = has_ohlc & (
        (df["high"] < df[["open", "low", "close"]].max(axis=1))
        | (df["low"] > df[["open", "high", "close"]].min(axis=1))
    )
    df = df[~invalid_ohlc]
    df["volume"] = df["volume"].round().astype("Int64")
    timestamp = now_pakistan().isoformat()

    inserted = 0
    unknown_symbols = []
    with get_connection() as conn:
        known = {r[0] for r in conn.execute("SELECT symbol FROM companies").fetchall()}
        for sym in sorted(set(df["symbol"]) - known):
            unknown_symbols.append(sym)
        # Do not silently create companies with unknown names.
        df = df[df["symbol"].isin(known)]

        for row in df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "traded_value"]].itertuples(index=False, name=None):
            conn.execute(
                """
                INSERT INTO eod_market_data
                (symbol, trade_date, open, high, low, close, volume, traded_value, data_source, ingestion_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    traded_value=excluded.traded_value, data_source=excluded.data_source,
                    ingestion_timestamp=excluded.ingestion_timestamp
                """,
                (*row[:7], None if pd.isna(row[7]) else float(row[7]), source_label, timestamp),
            )
            inserted += 1
    return inserted, unknown_symbols


def load_company_data(active_only=True):
    query = "SELECT symbol, company_name, sector, is_active, source, updated_at FROM companies"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY symbol"
    with get_connection() as conn:
        return pd.read_sql_query(query, conn)


def load_eod(symbol=None):
    query = "SELECT * FROM eod_market_data"
    params = ()
    if symbol:
        query += " WHERE symbol = ?"
        params = (symbol,)
    query += " ORDER BY trade_date"
    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def make_features(df):
    """Features only use current/past data; label is next-session close >= +5%."""
    x = df.sort_values("trade_date").copy()
    x["return_1d"] = x["close"].pct_change()
    x["return_5d"] = x["close"].pct_change(5)
    x["return_20d"] = x["close"].pct_change(20)
    x["ma20"] = x["close"].rolling(20, min_periods=20).mean()
    x["ma50"] = x["close"].rolling(50, min_periods=50).mean()
    x["close_vs_ma20"] = x["close"] / x["ma20"] - 1
    x["close_vs_ma50"] = x["close"] / x["ma50"] - 1
    x["volatility_20d"] = x["return_1d"].rolling(20, min_periods=20).std()
    x["volume_change"] = x["volume"].pct_change() if x["volume"].notna().any() else np.nan
    x["rsi14"] = calculate_rsi(x["close"], 14)
    x["next_close_return"] = x["close"].shift(-1) / x["close"] - 1
    x["target_up_5pct"] = (x["next_close_return"] >= 0.05).astype("float")
    x.loc[x["next_close_return"].isna(), "target_up_5pct"] = np.nan
    return x


def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


FEATURES = [
    "return_1d", "return_5d", "return_20d", "close_vs_ma20",
    "close_vs_ma50", "volatility_20d", "volume_change", "rsi14",
]


def predict_symbol(symbol, min_rows=120):
    raw = load_eod(symbol)
    if raw.empty or len(raw) < min_rows:
        return None, f"Insufficient history: need at least {min_rows} rows; found {len(raw)}."

    if raw["close"].isna().any():
        raw = raw.dropna(subset=["close"])

    featured = make_features(raw)
    train = featured.dropna(subset=FEATURES + ["target_up_5pct"]).copy()
    latest = featured.dropna(subset=FEATURES).tail(1)

    if len(train) < 60 or latest.empty:
        return None, "Not enough valid rows after calculating features."

    y = train["target_up_5pct"].astype(int)
    if y.nunique() < 2 or y.value_counts().min() < 5:
        return None, "Historical target classes are too sparse to estimate a useful probability."

    # Chronological holdout; never randomly shuffle time-series observations.
    split_at = int(len(train) * 0.8)
    if split_at < 40 or len(train) - split_at < 10:
        return None, "Not enough rows for a time-ordered validation split."

    x_train = train[FEATURES].iloc[:split_at]
    y_train = y.iloc[:split_at]
    x_test = train[FEATURES].iloc[split_at:]
    y_test = y.iloc[split_at:]

    if y_train.nunique() < 2:
        return None, "Training period contains only one target class."

    model = RandomForestClassifier(
        n_estimators=300,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
    )
    model.fit(x_train, y_train)
    test_pred = model.predict(x_test)
    test_accuracy = accuracy_score(y_test, test_pred)
    test_precision = precision_score(y_test, test_pred, zero_division=0)

    # Refit on all known labeled history for the current estimate.
    final_model = RandomForestClassifier(
        n_estimators=300,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
    )
    final_model.fit(train[FEATURES], y)
    probability = float(final_model.predict_proba(latest[FEATURES])[0][list(final_model.classes_).index(1)])
    last_row = latest.iloc[0]

    return {
        "Symbol": symbol,
        "Trade Date": last_row["trade_date"],
        "Latest Close": float(last_row["close"]),
        "Estimated P(Close +5% next session)": probability,
        "Validation Accuracy (chronological holdout)": float(test_accuracy),
        "Validation Precision": float(test_precision),
        "History Rows": int(len(raw)),
        "Model": "RandomForest v1 (starter; requires walk-forward testing)",
    }, None


initialize_database()

st.sidebar.title("📊 PSX EOD Analytics Terminal")
st.sidebar.caption(f"Pakistan time: {now_pakistan().strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.markdown("---")
page = st.sidebar.radio(
    "Select Module",
    [
        "1. Next-Day Prediction",
        "2. Long-Term Data Inspector",
        "3. Company Listing & Data Import",
        "4. System Status",
    ],
)

st.sidebar.info(
    "Data status: this app does not fetch live prices until a real, authorized data-provider connector is configured."
)

if page == "1. Next-Day Prediction":
    st.title("📈 Next-Day Prediction & Probability Terminal")
    st.write(
        "Uses imported historical EOD data to estimate the probability that the next session's "
        "closing price will be at least 5% above the latest close."
    )
    st.warning(
        "A model probability is not a guarantee. This starter model is not investment advice and must be "
        "validated with walk-forward backtesting and transaction costs before use."
    )

    companies = load_company_data()
    if companies.empty:
        st.info("No companies found. Open 'Company Listing & Data Import' and import a verified company list first.")
    else:
        selected_symbols = st.multiselect(
            "Companies to scan",
            options=companies["symbol"].tolist(),
            default=companies["symbol"].tolist()[:20],
        )
        min_history = st.number_input("Minimum EOD rows per company", min_value=60, max_value=3000, value=120, step=10)

        if st.button("Run Prediction Scan", type="primary"):
            if not selected_symbols:
                st.warning("Select at least one company.")
            else:
                rows, skipped = [], []
                progress = st.progress(0)
                for i, sym in enumerate(selected_symbols, start=1):
                    result, error = predict_symbol(sym, int(min_history))
                    if result:
                        rows.append(result)
                    else:
                        skipped.append({"Symbol": sym, "Reason": error})
                    progress.progress(i / len(selected_symbols))

                if rows:
                    output = pd.DataFrame(rows).sort_values(
                        "Estimated P(Close +5% next session)", ascending=False
                    )
                    st.success(f"Scan complete: {len(rows)} candidates scored; {len(skipped)} skipped.")
                    st.dataframe(
                        output.style.format({
                            "Latest Close": "{:.2f}",
                            "Estimated P(Close +5% next session)": "{:.1%}",
                            "Validation Accuracy (chronological holdout)": "{:.1%}",
                            "Validation Precision": "{:.1%}",
                        }),
                        use_container_width=True,
                    )
                    st.download_button(
                        "Download prediction results CSV",
                        output.to_csv(index=False).encode("utf-8"),
                        file_name=f"psx_predictions_{now_pakistan().strftime('%Y%m%d_%H%M')}.csv",
                        mime="text/csv",
                    )
                    # Save prediction records with explicit target definition.
                    with get_connection() as conn:
                        for row in rows:
                            conn.execute(
                                """
                                INSERT INTO predictions
                                (run_timestamp, symbol, trade_date, reference_close, target_probability, target_definition, model_version, note)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    now_pakistan().isoformat(), row["Symbol"], row["Trade Date"],
                                    row["Latest Close"], row["Estimated P(Close +5% next session)"],
                                    "Next session close >= latest close * 1.05",
                                    row["Model"],
                                    "Research estimate only; validate with walk-forward backtesting.",
                                ),
                            )
                else:
                    st.warning("No companies could be scored. Review history length and data quality.")
                if skipped:
                    with st.expander(f"Skipped companies ({len(skipped)})"):
                        st.dataframe(pd.DataFrame(skipped), use_container_width=True)

elif page == "2. Long-Term Data Inspector":
    st.title("🔎 Historical Data Inspector")
    companies = load_company_data(active_only=False)
    if companies.empty:
        st.info("No company records yet.")
    else:
        symbol = st.selectbox("Select symbol", companies["symbol"].tolist())
        history = load_eod(symbol)
        if history.empty:
            st.info("No EOD rows found for this symbol.")
        else:
            st.metric("Stored EOD rows", len(history))
            st.write(f"Date range: {history['trade_date'].min()} to {history['trade_date'].max()}")
            st.dataframe(history.sort_values("trade_date", ascending=False), use_container_width=True)
            st.line_chart(history.set_index("trade_date")["close"])
            st.download_button(
                "Download this company's EOD history",
                history.to_csv(index=False).encode("utf-8"),
                file_name=f"{symbol}_eod_history.csv",
                mime="text/csv",
            )

elif page == "3. Company Listing & Data Import":
    st.title("🏢 Company Listing & EOD Data Import")
    st.markdown(
        "Import verified data exported from an authorized source. This module deliberately does not claim "
        "that a live source was contacted unless a real connector has been implemented."
    )

    st.subheader("A. Import company universe")
    st.caption("CSV columns required: symbol, company_name. Optional: sector, is_active.")
    company_file = st.file_uploader("Upload company CSV", type=["csv"], key="company_csv")
    if company_file is not None and st.button("Import / Update Companies"):
        try:
            company_df = pd.read_csv(company_file)
            count = upsert_companies(company_df, "user-imported verified CSV")
            st.success(f"Imported or updated {count} company records.")
        except Exception as exc:
            st.error(f"Company import failed: {exc}")

    st.subheader("B. Import daily historical EOD data")
    st.caption("CSV columns required: symbol, trade_date, close. Recommended: open, high, low, volume, traded_value.")
    eod_file = st.file_uploader("Upload EOD CSV", type=["csv"], key="eod_csv")
    if eod_file is not None and st.button("Validate & Import EOD Data"):
        try:
            eod_df = pd.read_csv(eod_file)
            count, unknown = import_eod_data(eod_df, "user-imported provider CSV")
            st.success(f"Imported/updated {count} EOD rows.")
            if unknown:
                st.warning("These symbols are not in the company table and were skipped: " + ", ".join(unknown[:50]))
        except Exception as exc:
            st.error(f"EOD import failed: {exc}")

    st.subheader("C. Current company universe")
    all_companies = load_company_data(active_only=False)
    st.dataframe(all_companies, use_container_width=True)
    if not all_companies.empty:
        st.download_button(
            "Download company list CSV",
            all_companies.to_csv(index=False).encode("utf-8"),
            file_name="psx_company_universe.csv",
            mime="text/csv",
        )

elif page == "4. System Status":
    st.title("🛠️ System Status & Data Quality")
    companies = load_company_data(active_only=False)
    history = load_eod()
    with get_connection() as conn:
        prediction_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Company records", len(companies))
    c2.metric("EOD rows", len(history))
    c3.metric("Saved predictions", prediction_count)
    c4.metric("After 5:00 PM PKT", "Yes" if after_market_time() else "No")

    if not history.empty:
        st.write("EOD data coverage by symbol")
        coverage = history.groupby("symbol").agg(
            rows=("trade_date", "count"),
            first_date=("trade_date", "min"),
            latest_date=("trade_date", "max"),
            latest_close=("close", "last"),
        ).reset_index()
        st.dataframe(coverage.sort_values("rows", ascending=False), use_container_width=True)
    else:
        st.info("No EOD data has been imported yet.")

    st.markdown("### Data-provider integration checklist")
    st.write(
        "- Confirm provider/API credentials and license.\n"
        "- Implement a provider-specific connector with documented endpoints.\n"
        "- Add retries, rate-limit handling, logging and data reconciliation.\n"
        "- Verify final EOD availability; 5:00 PM is a configurable guardrail, not proof that official data is final.\n"
        "- Run automated tests and paper trading before relying on predictions."
    )
