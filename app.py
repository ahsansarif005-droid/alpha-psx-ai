"""
PSX AI Research Terminal — free-source starter build
Data source: psxdata Python package (public PSX website data; third-party open-source connector).
No paid API key is required by the package, but availability, terms, rate limits and data quality can change.
This app never fabricates prices or prediction probabilities.
Python 3.11+ recommended.
"""

import sqlite3
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, brier_score_loss

try:
    import psxdata
except ImportError:
    psxdata = None

st.set_page_config(page_title="PSX AI Research Terminal", page_icon="📈", layout="wide")

DB_NAME = "psx_institutional_master.db"
PK_TZ = ZoneInfo("Asia/Karachi")
DEFAULT_START = "2011-01-01"
FEATURES = [
    "return_1d", "return_5d", "return_20d", "close_vs_ma20",
    "close_vs_ma50", "volatility_20d", "volume_change", "rsi14",
]


# ----------------------------- DATABASE -----------------------------
def now_pk():
    return datetime.now(PK_TZ)


def db():
    con = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
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
            open REAL, high REAL, low REAL, close REAL NOT NULL,
            volume INTEGER, traded_value REAL,
            data_source TEXT NOT NULL,
            ingestion_timestamp TEXT NOT NULL,
            PRIMARY KEY(symbol, trade_date),
            FOREIGN KEY(symbol) REFERENCES companies(symbol)
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
            validation_accuracy REAL,
            validation_precision REAL,
            brier_score REAL,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            records INTEGER DEFAULT 0,
            message TEXT
        );
        """)


def log_sync(operation, status, records=0, message=""):
    with db() as con:
        con.execute(
            "INSERT INTO sync_log(started_at, finished_at, operation, status, records, message) VALUES (?, ?, ?, ?, ?, ?)",
            (now_pk().isoformat(), now_pk().isoformat(), operation, status, int(records), str(message)[:2000]),
        )


def get_companies(active_only=True):
    q = "SELECT symbol, company_name, sector, is_active, source, updated_at FROM companies"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY symbol"
    with db() as con:
        return pd.read_sql_query(q, con)


def get_history(symbol=None):
    q = "SELECT * FROM eod_market_data"
    params = ()
    if symbol:
        q += " WHERE symbol=?"
        params = (symbol,)
    q += " ORDER BY trade_date"
    with db() as con:
        return pd.read_sql_query(q, con, params=params)


def store_companies(df, source):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    # Handle common provider column-name variations.
    rename = {
        "ticker": "symbol", "code": "symbol", "scrip": "symbol",
        "name": "company_name", "company": "company_name",
        "industry": "sector", "sector_name": "sector",
    }
    df = df.rename(columns=rename)
    if "symbol" not in df.columns:
        raise ValueError("Provider company list did not contain a symbol/ticker column.")
    if "company_name" not in df.columns:
        df["company_name"] = df["symbol"]
    if "sector" not in df.columns:
        df["sector"] = ""
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df["company_name"] = df["company_name"].fillna(df["symbol"]).astype(str).str.strip()
    df["sector"] = df["sector"].fillna("").astype(str)
    df = df[(df["symbol"] != "") & (df["symbol"].str.lower() != "nan")]
    df = df.drop_duplicates("symbol", keep="last")
    stamp = now_pk().isoformat()
    with db() as con:
        for row in df[["symbol", "company_name", "sector"]].itertuples(index=False, name=None):
            con.execute("""
                INSERT INTO companies(symbol, company_name, sector, is_active, source, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company_name=excluded.company_name,
                    sector=CASE WHEN excluded.sector='' THEN companies.sector ELSE excluded.sector END,
                    is_active=1, source=excluded.source, updated_at=excluded.updated_at
            """, (*row, source, stamp))
    return len(df)


def normalize_history(df, symbol):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).strip().lower().replace(" ", "_") for c in x.columns]
    aliases = {
        "date": "trade_date", "datetime": "trade_date", "timestamp": "trade_date",
        "ticker": "symbol", "adj_close": "close", "adj._close": "close",
        "vol": "volume", "traded_volume": "volume",
    }
    x = x.rename(columns=aliases)
    if "trade_date" not in x.columns:
        # Some pandas data sources return the date as the index.
        x = x.reset_index()
        x.columns = [str(c).strip().lower().replace(" ", "_") for c in x.columns]
        x = x.rename(columns={"date": "trade_date", "index": "trade_date", "datetime": "trade_date"})
    if "close" not in x.columns:
        return pd.DataFrame()
    for col in ["open", "high", "low", "volume", "traded_value"]:
        if col not in x.columns:
            x[col] = np.nan
    x["trade_date"] = pd.to_datetime(x["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "traded_value"]:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x["symbol"] = symbol
    x = x.dropna(subset=["trade_date", "close"])
    x = x[x["close"] > 0]
    x = x.drop_duplicates(["symbol", "trade_date"], keep="last")
    return x[["symbol", "trade_date", "open", "high", "low", "close", "volume", "traded_value"]]


def save_history(df, source="psxdata"):
    if df.empty:
        return 0
    stamp = now_pk().isoformat()
    count = 0
    with db() as con:
        for sym in df["symbol"].dropna().unique():
            exists = con.execute("SELECT 1 FROM companies WHERE symbol=?", (sym,)).fetchone()
            if not exists:
                con.execute(
                    "INSERT INTO companies(symbol, company_name, sector, is_active, source, updated_at) VALUES (?, ?, '', 1, ?, ?)",
                    (sym, sym, source, stamp),
                )
        for r in df.itertuples(index=False, name=None):
            sym, dt, opn, high, low, close, vol, value = r
            if pd.isna(close):
                continue
            con.execute("""
                INSERT INTO eod_market_data
                (symbol, trade_date, open, high, low, close, volume, traded_value, data_source, ingestion_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    traded_value=excluded.traded_value, data_source=excluded.data_source,
                    ingestion_timestamp=excluded.ingestion_timestamp
            """, (
                sym, dt,
                None if pd.isna(opn) else float(opn),
                None if pd.isna(high) else float(high),
                None if pd.isna(low) else float(low),
                float(close),
                None if pd.isna(vol) else int(vol),
                None if pd.isna(value) else float(value),
                source, stamp,
            ))
            count += 1
    return count


# ----------------------------- FREE SOURCE CONNECTOR -----------------------------
def source_available():
    return psxdata is not None


def sync_company_universe():
    """Pull listed ticker metadata through psxdata; API shape can change between versions."""
    if psxdata is None:
        raise RuntimeError("psxdata is not installed. Add psxdata to requirements.txt and redeploy.")
    errors = []
    for fn_name in ("symbols", "tickers", "screener"):
        fn = getattr(psxdata, fn_name, None)
        if fn is None:
            continue
        try:
            result = fn()
            if isinstance(result, pd.DataFrame):
                df = result.reset_index() if result.index.name else result.copy()
                n = store_companies(df, f"psxdata.{fn_name}")
                if n:
                    return n, f"Universe refreshed using psxdata.{fn_name}."
            elif isinstance(result, (list, tuple)):
                df = pd.DataFrame(result)
                if len(df.columns) == 1:
                    df.columns = ["symbol"]
                n = store_companies(df, f"psxdata.{fn_name}")
                if n:
                    return n, f"Universe refreshed using psxdata.{fn_name}."
        except Exception as exc:
            errors.append(f"{fn_name}: {exc}")
    raise RuntimeError("Could not read ticker universe. " + " | ".join(errors[-3:]))


def fetch_symbol_history(symbol, start_date):
    if psxdata is None:
        raise RuntimeError("psxdata package is not installed.")
    fn = getattr(psxdata, "stocks", None)
    if fn is None:
        raise RuntimeError("Installed psxdata version does not expose stocks().")
    # Use documented public function. The package may change; errors are shown rather than hidden.
    df = fn(symbol, start=start_date)
    return normalize_history(df, symbol)


def sync_history(symbols, start_date, delay_seconds=0.35):
    total_rows, successes, failures = 0, 0, []
    progress = st.progress(0)
    status = st.empty()
    for i, sym in enumerate(symbols, start=1):
        try:
            old = get_history(sym)
            fetch_from = start_date
            if not old.empty:
                last = pd.to_datetime(old["trade_date"], errors="coerce").max()
                if pd.notna(last):
                    # Re-fetch last few days to correct late/finalized records.
                    fetch_from = max(pd.Timestamp(start_date), last - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
            fresh = fetch_symbol_history(sym, fetch_from)
            if not fresh.empty:
                total_rows += save_history(fresh, "psxdata public PSX source")
                successes += 1
            else:
                failures.append(f"{sym}: no rows returned")
        except Exception as exc:
            failures.append(f"{sym}: {str(exc)[:180]}")
        status.write(f"Fetching {i}/{len(symbols)}: {sym} — {successes} symbols succeeded")
        progress.progress(i / max(1, len(symbols)))
        time.sleep(delay_seconds)  # be polite to the public source
    return successes, total_rows, failures


# ----------------------------- ANALYTICS -----------------------------
def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def build_features(raw):
    x = raw.sort_values("trade_date").copy()
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
    x["next_return"] = x["close"].shift(-1) / x["close"] - 1
    x["target"] = (x["next_return"] >= 0.05).astype(float)
    x.loc[x["next_return"].isna(), "target"] = np.nan
    return x


def score_symbol(symbol, min_rows):
    raw = get_history(symbol)
    if len(raw) < min_rows:
        return None, f"Only {len(raw)} EOD rows; need {min_rows}."
    x = build_features(raw)
    train = x.dropna(subset=FEATURES + ["target"]).copy()
    latest = x.dropna(subset=FEATURES).tail(1)
    if len(train) < 80 or latest.empty:
        return None, "Insufficient usable rows after feature calculation."
    y = train["target"].astype(int)
    if y.nunique() < 2 or y.value_counts().min() < 5:
        return None, "Not enough examples of both target classes."
    cut = int(len(train) * 0.8)
    if cut < 50 or len(train) - cut < 15 or train["target"].iloc[:cut].nunique() < 2:
        return None, "Not enough chronological validation data."
    model = RandomForestClassifier(
        n_estimators=250, min_samples_leaf=5,
        class_weight="balanced_subsample", random_state=42, n_jobs=-1
    )
    model.fit(train[FEATURES].iloc[:cut], y.iloc[:cut])
    pred = model.predict(train[FEATURES].iloc[cut:])
    prob_test = model.predict_proba(train[FEATURES].iloc[cut:])[:, list(model.classes_).index(1)]
    acc = accuracy_score(y.iloc[cut:], pred)
    precision = precision_score(y.iloc[cut:], pred, zero_division=0)
    brier = brier_score_loss(y.iloc[cut:], prob_test)
    final = RandomForestClassifier(
        n_estimators=250, min_samples_leaf=5,
        class_weight="balanced_subsample", random_state=42, n_jobs=-1
    )
    final.fit(train[FEATURES], y)
    probability = float(final.predict_proba(latest[FEATURES])[0][list(final.classes_).index(1)])
    row = latest.iloc[0]
    return {
        "Symbol": symbol,
        "Latest EOD Date": row["trade_date"],
        "Latest Close": float(row["close"]),
        "Estimated Probability: next close >= +5%": probability,
        "Chronological Holdout Accuracy": float(acc),
        "Chronological Holdout Precision": float(precision),
        "Holdout Brier Score (lower better)": float(brier),
        "History Rows": int(len(raw)),
        "Model": "Random Forest v1",
    }, None


# ----------------------------- STREAMLIT UI -----------------------------
init_db()
st.sidebar.title("📊 PSX AI Research Terminal")
st.sidebar.caption(f"Pakistan time: {now_pk():%Y-%m-%d %H:%M:%S}")
st.sidebar.markdown("---")
page = st.sidebar.radio("Modules", [
    "1. Next-Day AI Prediction",
    "2. Auto Data Sync",
    "3. Company & Historical Data",
    "4. System Health",
])
st.sidebar.info("Free-source mode: psxdata package + public PSX website data. No paid API key configured.")
st.sidebar.caption("Public-source access may fail or change. Failed syncs are logged; no fake data is generated.")

if page == "1. Next-Day AI Prediction":
    st.title("📈 Next-Day Prediction & Probability")
    st.write("Target: next trading session close is at least 5% above the latest stored EOD close.")
    st.warning("Experimental research estimate only. Not a guarantee, not a buy/sell instruction. Test with paper trading.")
    companies = get_companies()
    if companies.empty:
        st.info("No companies in database. Open Auto Data Sync and refresh the free-source company universe.")
    else:
        symbols = st.multiselect("Companies to scan", companies["symbol"].tolist(), default=companies["symbol"].tolist()[:25])
        min_rows = st.number_input("Minimum EOD rows", 60, 4000, 120, 20)
        if st.button("Run AI Scan", type="primary"):
            if not symbols:
                st.warning("Select at least one symbol.")
            else:
                results, skipped = [], []
                bar = st.progress(0)
                for i, sym in enumerate(symbols, 1):
                    try:
                        row, err = score_symbol(sym, int(min_rows))
                        if row:
                            results.append(row)
                        else:
                            skipped.append({"Symbol": sym, "Reason": err})
                    except Exception as exc:
                        skipped.append({"Symbol": sym, "Reason": str(exc)[:250]})
                    bar.progress(i / len(symbols))
                if results:
                    out = pd.DataFrame(results).sort_values("Estimated Probability: next close >= +5%", ascending=False)
                    st.success(f"Scored {len(results)} symbols; skipped {len(skipped)}.")
                    st.dataframe(out.style.format({
                        "Latest Close": "{:.2f}",
                        "Estimated Probability: next close >= +5%": "{:.1%}",
                        "Chronological Holdout Accuracy": "{:.1%}",
                        "Chronological Holdout Precision": "{:.1%}",
                        "Holdout Brier Score (lower better)": "{:.3f}",
                    }), use_container_width=True)
                    st.download_button("Download predictions CSV", out.to_csv(index=False).encode(), "psx_ai_predictions.csv", "text/csv")
                    with db() as con:
                        for r in results:
                            con.execute("""
                            INSERT INTO predictions
                            (run_timestamp, symbol, trade_date, reference_close, target_probability,
                             target_definition, model_version, validation_accuracy, validation_precision, brier_score, note)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                now_pk().isoformat(), r["Symbol"], r["Latest EOD Date"], r["Latest Close"],
                                r["Estimated Probability: next close >= +5%"],
                                "Next session close >= latest EOD close * 1.05",
                                r["Model"], r["Chronological Holdout Accuracy"],
                                r["Chronological Holdout Precision"], r["Holdout Brier Score (lower better)"],
                                "Research-only; needs walk-forward backtest and paper trading."
                            ))
                else:
                    st.warning("No company had enough clean historical data to score.")
                if skipped:
                    with st.expander(f"Skipped symbols ({len(skipped)})"):
                        st.dataframe(pd.DataFrame(skipped), use_container_width=True)

elif page == "2. Auto Data Sync":
    st.title("🔄 Free-source Company Listing & EOD Sync")
    st.markdown("This module uses the open-source `psxdata` Python package, which retrieves public PSX website data. It is not an official PSX API.")
    if psxdata is None:
        st.error("psxdata is unavailable in this environment. Check requirements.txt and deployment logs.")
    else:
        st.success("psxdata package imported successfully.")
        st.caption("First historical sync for hundreds of symbols can take a long time and may be throttled by the public website. Start with a small test batch.")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Refresh Company Universe"):
                try:
                    with st.spinner("Reading public ticker metadata..."):
                        count, msg = sync_company_universe()
                    log_sync("company universe refresh", "success", count, msg)
                    st.success(f"{msg} Records: {count}")
                except Exception as exc:
                    log_sync("company universe refresh", "failed", 0, str(exc))
                    st.error(f"Universe refresh failed: {exc}")
        with col2:
            if st.button("Test Source (OGDC)"):
                try:
                    with st.spinner("Requesting OGDC history..."):
                        test = fetch_symbol_history("OGDC", "2024-01-01")
                    if test.empty:
                        st.warning("Source returned no rows for OGDC.")
                    else:
                        st.success(f"Source test passed: {len(test)} rows returned.")
                        st.dataframe(test.tail(10), use_container_width=True)
                except Exception as exc:
                    st.error(f"Source test failed: {exc}")
    st.markdown("---")
    st.subheader("Download history")
    companies = get_companies()
    if companies.empty:
        st.info("Refresh the company universe first.")
    else:
        selected = st.multiselect("Symbols to sync", companies["symbol"].tolist(), default=companies["symbol"].tolist()[:5])
        start_date = st.date_input("Start date", value=date(2011, 1, 1), min_value=date(1990, 1, 1), max_value=date.today())
        delay = st.slider("Delay between symbols (seconds)", 0.2, 2.0, 0.5, 0.1)
        if st.button("Fetch / Update Selected EOD History", type="primary"):
            if psxdata is None:
                st.error("Install psxdata first.")
            elif not selected:
                st.warning("Select one or more symbols.")
            else:
                try:
                    with st.spinner("Fetching selected symbols. Keep this page open until finished."):
                        succeeded, rows, failures = sync_history(selected, start_date.isoformat(), delay)
                    log_sync("EOD history sync", "partial" if failures else "success", rows,
                             f"success_symbols={succeeded}; failures={len(failures)}")
                    st.success(f"Completed. Successful symbols: {succeeded}/{len(selected)}. Rows inserted/updated: {rows}.")
                    if failures:
                        with st.expander(f"Fetch failures ({len(failures)})"):
                            st.code("\n".join(failures))
                except Exception as exc:
                    log_sync("EOD history sync", "failed", 0, str(exc))
                    st.error(f"Sync stopped: {exc}")

elif page == "3. Company & Historical Data":
    st.title("🏢 Company & Historical Data Inspector")
    companies = get_companies(active_only=False)
    if companies.empty:
        st.info("No companies stored yet. Use Auto Data Sync.")
    else:
        st.dataframe(companies, use_container_width=True)
        symbol = st.selectbox("Inspect symbol", companies["symbol"].tolist())
        history = get_history(symbol)
        if history.empty:
            st.info("No historical EOD data stored for this symbol.")
        else:
            st.metric("Stored EOD rows", len(history))
            st.write(f"Date range: {history['trade_date'].min()} to {history['trade_date'].max()}")
            st.line_chart(history.set_index("trade_date")["close"])
            st.dataframe(history.sort_values("trade_date", ascending=False), use_container_width=True)
            st.download_button("Download symbol history CSV", history.to_csv(index=False).encode(), f"{symbol}_eod.csv", "text/csv")
        all_history = get_history()
        if not all_history.empty:
            coverage = all_history.groupby("symbol").agg(
                rows=("trade_date", "count"), first_date=("trade_date", "min"),
                latest_date=("trade_date", "max")
            ).reset_index().sort_values("rows", ascending=False)
            st.subheader("Data coverage")
            st.dataframe(coverage, use_container_width=True)

elif page == "4. System Health":
    st.title("🛠️ System Health")
    companies = get_companies(active_only=False)
    history = get_history()
    with db() as con:
        pred_count = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        logs = pd.read_sql_query("SELECT * FROM sync_log ORDER BY id DESC LIMIT 30", con)
    a, b, c = st.columns(3)
    a.metric("Company records", len(companies))
    b.metric("EOD rows", len(history))
    c.metric("Saved prediction runs", pred_count)
    st.write("Data source:", "psxdata available" if psxdata is not None else "psxdata not installed")
    if not history.empty:
        st.write("Latest stored trade date:", history["trade_date"].max())
        st.write("Symbols with history:", history["symbol"].nunique())
    st.subheader("Recent sync log")
    st.dataframe(logs, use_container_width=True)
    st.subheader("Important limitations")
    st.markdown("""
    - Free public-source access is not a service-level guarantee; the site/package may change, fail or throttle requests.
    - No intraday historical data is assumed. This app is for daily EOD research.
    - The current model is a starter model, not institutional-grade validation.
    - For production-quality results, add walk-forward testing, corporate-action-adjusted returns, market/sector features, costs, calibration, and paper trading.
    - Do not republish or sell PSX data without checking PSX's current terms and licensing rules.
    """)

st.caption("PSX AI Research Terminal • Free-source prototype • No automatic live trading")
