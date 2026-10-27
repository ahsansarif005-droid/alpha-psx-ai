
"""
PSX AI Research Terminal
Free-source educational research prototype.
Python 3.11+ recommended.
No paid API key required by psxdata.
Probabilities are experimental, not trading advice.
"""

import sqlite3
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    brier_score_loss,
)

try:
    import psxdata
except ImportError:
    psxdata = None

st.set_page_config(
    page_title="PSX AI Research Terminal",
    page_icon="📈",
    layout="wide",
)

DB_NAME = "psx_institutional_master.db"
PK_TZ = ZoneInfo("Asia/Karachi")

FEATURES = [
    "return_1d",
    "return_5d",
    "return_20d",
    "close_vs_ma20",
    "close_vs_ma50",
    "volatility_20d",
    "volume_change",
    "rsi14",
]

# ============================================================
# DATABASE
# ============================================================

def now_pk():
    return datetime.now(PK_TZ)

def db():
    con = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    """Create required tables and safely add missing columns."""
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            symbol TEXT PRIMARY KEY,
            company_name TEXT NOT NULL DEFAULT '',
            sector TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            source TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
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
            data_source TEXT NOT NULL DEFAULT 'unknown',
            ingestion_timestamp TEXT NOT NULL DEFAULT '',
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

        # Migrate older or partially created databases.
        migrations = {
            "companies": [
                ("company_name", "TEXT NOT NULL DEFAULT ''"),
                ("sector", "TEXT DEFAULT ''"),
                ("is_active", "INTEGER NOT NULL DEFAULT 1"),
                ("source", "TEXT DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "eod_market_data": [
                ("open", "REAL"),
                ("high", "REAL"),
                ("low", "REAL"),
                ("close", "REAL NOT NULL DEFAULT 0"),
                ("volume", "INTEGER"),
                ("traded_value", "REAL"),
                ("data_source", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("ingestion_timestamp", "TEXT NOT NULL DEFAULT ''"),
            ],
            "predictions": [
                ("run_timestamp", "TEXT NOT NULL DEFAULT ''"),
                ("symbol", "TEXT NOT NULL DEFAULT ''"),
                ("trade_date", "TEXT NOT NULL DEFAULT ''"),
                ("reference_close", "REAL NOT NULL DEFAULT 0"),
                ("target_probability", "REAL"),
                ("target_definition", "TEXT NOT NULL DEFAULT ''"),
                ("model_version", "TEXT NOT NULL DEFAULT ''"),
                ("validation_accuracy", "REAL"),
                ("validation_precision", "REAL"),
                ("brier_score", "REAL"),
                ("note", "TEXT"),
            ],
            "sync_log": [
                ("started_at", "TEXT NOT NULL DEFAULT ''"),
                ("finished_at", "TEXT"),
                ("operation", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT ''"),
                ("records", "INTEGER DEFAULT 0"),
                ("message", "TEXT"),
            ],
        }

        for table, fields in migrations.items():
            existing = {
                row[1]
                for row in con.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

            for column, definition in fields:
                if column not in existing:
                    con.execute(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} {definition}"
                    )

def log_sync(operation, status, records=0, message=""):
    with db() as con:
        con.execute("""
            INSERT INTO sync_log
            (started_at, finished_at, operation, status, records, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            now_pk().isoformat(),
            now_pk().isoformat(),
            operation,
            status,
            int(records),
            str(message)[:2000],
        ))

def get_companies(active_only=True):
    """Read company records and provide a useful database error."""
    init_db()

    with db() as con:
        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(companies)"
            ).fetchall()
        }

        required = {
            "symbol", "company_name", "sector",
            "is_active", "source", "updated_at",
        }

        missing = required - columns
        if missing:
            raise RuntimeError(
                f"Database schema mismatch. Missing columns: "
                f"{sorted(missing)}. Database: {DB_NAME}"
            )

        query = """
            SELECT symbol, company_name, sector,
                   is_active, source, updated_at
            FROM companies
        """

        if active_only:
            query += " WHERE is_active=1"

        query += " ORDER BY symbol"

        return pd.read_sql_query(query, con)

def get_history(symbol=None):
    init_db()

    query = "SELECT * FROM eod_market_data"
    params = ()

    if symbol:
        query += " WHERE symbol=?"
        params = (symbol,)

    query += " ORDER BY trade_date"

    with db() as con:
        return pd.read_sql_query(
            query, con, params=params
        )

# ============================================================
# COMPANY UNIVERSE
# ============================================================

def store_companies(df, source):
    df = df.copy()
    df.columns = [
        str(c).strip().lower()
        for c in df.columns
    ]

    aliases = {
        "ticker": "symbol",
        "code": "symbol",
        "scrip": "symbol",
        "name": "company_name",
        "company": "company_name",
        "industry": "sector",
        "sector_name": "sector",
    }

    df = df.rename(columns=aliases)

    # Support providers returning symbols as the DataFrame index.
    if "symbol" not in df.columns:
        if df.index.name:
            df = df.reset_index()
            df.columns = [
                str(c).strip().lower()
                for c in df.columns
            ]
            df = df.rename(columns={
                "index": "symbol",
                "ticker": "symbol",
                "code": "symbol",
            })

    if "symbol" not in df.columns:
        raise ValueError(
            "Provider output has no recognizable symbol column."
        )

    if "company_name" not in df.columns:
        df["company_name"] = df["symbol"]

    if "sector" not in df.columns:
        df["sector"] = ""

    df["symbol"] = (
        df["symbol"].astype(str).str.strip().str.upper()
    )
    df["company_name"] = (
        df["company_name"]
        .fillna(df["symbol"])
        .astype(str)
        .str.strip()
    )
    df["sector"] = df["sector"].fillna("").astype(str)

    df = df[
        (df["symbol"] != "")
        & (df["symbol"].str.lower() != "nan")
    ].drop_duplicates("symbol", keep="last")

    stamp = now_pk().isoformat()

    with db() as con:
        for row in df[
            ["symbol", "company_name", "sector"]
        ].itertuples(index=False, name=None):
            con.execute("""
                INSERT INTO companies
                (symbol, company_name, sector, is_active,
                 source, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company_name=excluded.company_name,
                    sector=CASE
                        WHEN excluded.sector=''
                        THEN companies.sector
                        ELSE excluded.sector
                    END,
                    is_active=1,
                    source=excluded.source,
                    updated_at=excluded.updated_at
            """, (*row, source, stamp))

    return len(df)

def sync_company_universe():
    if psxdata is None:
        raise RuntimeError(
            "psxdata is not installed. Check requirements.txt."
        )

    errors = []

    for fn_name in ("symbols", "tickers", "screener"):
        fn = getattr(psxdata, fn_name, None)

        if not callable(fn):
            continue

        try:
            result = fn()

            if isinstance(result, pd.DataFrame):
                df = result.copy()

                if (
                    "symbol" not in
                    [str(c).strip().lower() for c in df.columns]
                    and df.index.name
                ):
                    df = df.reset_index()

                count = store_companies(
                    df, f"psxdata.{fn_name}"
                )

            elif isinstance(result, (list, tuple)):
                if result and all(
                    isinstance(item, str)
                    for item in result
                ):
                    df = pd.DataFrame({
                        "symbol": result
                    })
                else:
                    df = pd.DataFrame(result)

                count = store_companies(
                    df, f"psxdata.{fn_name}"
                )
            else:
                continue

            if count > 0:
                return count, (
                    f"Company universe loaded using "
                    f"psxdata.{fn_name}."
                )

        except Exception as exc:
            errors.append(
                f"{fn_name}: {type(exc).__name__}: {exc}"
            )

    raise RuntimeError(
        "Could not load company universe. "
        + " | ".join(errors[-3:])
    )

# ============================================================
# HISTORICAL EOD DATA
# ============================================================

def normalize_history(df, symbol):
    if (
        df is None
        or not isinstance(df, pd.DataFrame)
        or df.empty
    ):
        return pd.DataFrame()

    x = df.copy()
    x.columns = [
        str(c).strip().lower().replace(" ", "_")
        for c in x.columns
    ]

    aliases = {
        "date": "trade_date",
        "datetime": "trade_date",
        "timestamp": "trade_date",
        "ticker": "symbol",
        "vol": "volume",
        "traded_volume": "volume",
        "value": "traded_value",
    }
    x = x.rename(columns=aliases)

    if "trade_date" not in x.columns:
        x = x.reset_index()
        x.columns = [
            str(c).strip().lower().replace(" ", "_")
            for c in x.columns
        ]
        x = x.rename(columns={
            "date": "trade_date",
            "index": "trade_date",
            "datetime": "trade_date",
        })

    if "close" not in x.columns:
        return pd.DataFrame()

    for col in [
        "open", "high", "low", "volume", "traded_value"
    ]:
        if col not in x.columns:
            x[col] = np.nan

    x["trade_date"] = (
        pd.to_datetime(x["trade_date"], errors="coerce")
        .dt.strftime("%Y-%m-%d")
    )

    for col in [
        "open", "high", "low", "close",
        "volume", "traded_value",
    ]:
        x[col] = pd.to_numeric(x[col], errors="coerce")

    x["symbol"] = symbol
    x = x.dropna(subset=["trade_date", "close"])
    x = x[x["close"] > 0]
    x = x.drop_duplicates(
        ["symbol", "trade_date"], keep="last"
    )

    return x[[
        "symbol", "trade_date", "open", "high", "low",
        "close", "volume", "traded_value",
    ]]

def fetch_symbol_history(symbol, start_date):
    if psxdata is None:
        raise RuntimeError("psxdata package is not installed.")

    fn = getattr(psxdata, "stocks", None)

    if not callable(fn):
        raise RuntimeError(
            "Installed psxdata version does not expose stocks()."
        )

    result = fn(symbol, start=start_date)
    return normalize_history(result, symbol)

def save_history(df, source="psxdata"):
    if df.empty:
        return 0

    stamp = now_pk().isoformat()
    count = 0

    with db() as con:
        for sym in df["symbol"].dropna().unique():
            exists = con.execute(
                "SELECT 1 FROM companies WHERE symbol=?",
                (sym,),
            ).fetchone()

            if not exists:
                con.execute("""
                    INSERT INTO companies
                    (symbol, company_name, sector, is_active,
                     source, updated_at)
                    VALUES (?, ?, '', 1, ?, ?)
                """, (sym, sym, source, stamp))

        for row in df.itertuples(index=False, name=None):
            sym, dt, opn, high, low, close, vol, value = row

            if pd.isna(close):
                continue

            con.execute("""
                INSERT INTO eod_market_data
                (symbol, trade_date, open, high, low, close,
                 volume, traded_value, data_source,
                 ingestion_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    traded_value=excluded.traded_value,
                    data_source=excluded.data_source,
                    ingestion_timestamp=excluded.ingestion_timestamp
            """, (
                sym,
                dt,
                None if pd.isna(opn) else float(opn),
                None if pd.isna(high) else float(high),
                None if pd.isna(low) else float(low),
                float(close),
                None if pd.isna(vol) else int(vol),
                None if pd.isna(value) else float(value),
                source,
                stamp,
            ))
            count += 1

    return count

def sync_history(symbols, start_date, delay_seconds=0.5):
    total_rows = 0
    successes = 0
    failures = []

    progress = st.progress(0)
    status = st.empty()

    for i, symbol in enumerate(symbols, start=1):
        try:
            old = get_history(symbol)
            fetch_from = start_date

            if not old.empty:
                last = pd.to_datetime(
                    old["trade_date"], errors="coerce"
                ).max()

                if pd.notna(last):
                    fetch_from = max(
                        pd.Timestamp(start_date),
                        last - pd.Timedelta(days=5),
                    ).strftime("%Y-%m-%d")

            fresh = fetch_symbol_history(symbol, fetch_from)

            if not fresh.empty:
                total_rows += save_history(
                    fresh, "psxdata public PSX source"
                )
                successes += 1
            else:
                failures.append(
                    f"{symbol}: no rows returned"
                )

        except Exception as exc:
            failures.append(
                f"{symbol}: {type(exc).__name__}: {exc}"
            )

        status.write(
            f"Fetching {i}/{len(symbols)}: {symbol} "
            f"— {successes} symbols succeeded"
        )
        progress.progress(i / max(1, len(symbols)))
        time.sleep(delay_seconds)

    return successes, total_rows, failures

# ============================================================
# ANALYTICS
# ============================================================

def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(
        period, min_periods=period
    ).mean()
    loss = (-delta.clip(upper=0)).rolling(
        period, min_periods=period
    ).mean()

    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def build_features(raw):
    x = raw.sort_values("trade_date").copy()

    x["return_1d"] = x["close"].pct_change()
    x["return_5d"] = x["close"].pct_change(5)
    x["return_20d"] = x["close"].pct_change(20)

    x["ma20"] = x["close"].rolling(
        20, min_periods=20
    ).mean()
    x["ma50"] = x["close"].rolling(
        50, min_periods=50
    ).mean()

    x["close_vs_ma20"] = x["close"] / x["ma20"] - 1
    x["close_vs_ma50"] = x["close"] / x["ma50"] - 1

    x["volatility_20d"] = x["return_1d"].rolling(
        20, min_periods=20
    ).std()

    if x["volume"].notna().any():
        x["volume_change"] = x["volume"].pct_change()
    else:
        x["volume_change"] = np.nan

    x["rsi14"] = calculate_rsi(x["close"], 14)

    x["next_return"] = (
        x["close"].shift(-1) / x["close"] - 1
    )
    x["target"] = np.where(
        x["next_return"].notna(),
        (x["next_return"] >= 0.05).astype(float),
        np.nan,
    )

    return x

def score_symbol(symbol, min_rows):
    raw = get_history(symbol)

    if len(raw) < min_rows:
        return None, (
            f"Only {len(raw)} EOD rows; need {min_rows}."
        )

    x = build_features(raw)

    train = x.dropna(
        subset=FEATURES + ["target"]
    ).copy()

    latest = x.dropna(
        subset=FEATURES
    ).tail(1)

    if len(train) < 80 or latest.empty:
        return None, "Insufficient usable historical rows."

    y = train["target"].astype(int)

    if y.nunique() < 2 or y.value_counts().min() < 5:
        return None, "Not enough examples of both target classes."

    cut = int(len(train) * 0.8)

    if (
        cut < 50
        or len(train) - cut < 15
        or train["target"].iloc[:cut].nunique() < 2
    ):
        return None, "Insufficient chronological validation data."

    model = RandomForestClassifier(
        n_estimators=200,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        train[FEATURES].iloc[:cut],
        y.iloc[:cut],
    )

    validation_x = train[FEATURES].iloc[cut:]
    validation_y = y.iloc[cut:]
    predicted = model.predict(validation_x)

    classes = list(model.classes_)
    if 1 not in classes:
        return None, "Model did not learn the positive target class."

    positive_index = classes.index(1)
    validation_probability = model.predict_proba(
        validation_x
    )[:, positive_index]

    accuracy = accuracy_score(
        validation_y, predicted
    )
    precision = precision_score(
        validation_y, predicted, zero_division=0
    )
    brier = brier_score_loss(
        validation_y, validation_probability
    )

    final_model = RandomForestClassifier(
        n_estimators=200,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )

    final_model.fit(train[FEATURES], y)

    final_classes = list(final_model.classes_)
    probability = float(
        final_model.predict_proba(
            latest[FEATURES]
        )[0][final_classes.index(1)]
    )

    row = latest.iloc[0]

    return {
        "Symbol": symbol,
        "Latest EOD Date": row["trade_date"],
        "Latest Close": float(row["close"]),
        "Estimated Probability: next close >= +5%": probability,
        "Chronological Holdout Accuracy": float(accuracy),
        "Chronological Holdout Precision": float(precision),
        "Holdout Brier Score": float(brier),
        "History Rows": int(len(raw)),
        "Model": "Random Forest v1",
    }, None

# ============================================================
# APPLICATION
# ============================================================

init_db()

st.sidebar.title("📊 PSX AI Research Terminal")
st.sidebar.caption(
    f"Pakistan time: {now_pk():%Y-%m-%d %H:%M:%S}"
)

page = st.sidebar.radio(
    "Modules",
    [
        "1. Next-Day AI Prediction",
        "2. Auto Data Sync",
        "3. Company & Historical Data",
        "4. System Health",
    ],
)

st.sidebar.info(
    "Free-source research mode. Public data availability "
    "and terms can change."
)

if page == "1. Next-Day AI Prediction":
    st.title("📈 Next-Day Prediction & Probability")

    st.write(
        "Target: next trading session close is at least "
        "5% above the latest stored EOD close."
    )

    st.warning(
        "Experimental estimate only. Not a guarantee or "
        "buy/sell instruction. Use paper trading."
    )

    companies = get_companies()

    if companies.empty:
        st.info(
            "Database is ready but has no companies yet. "
            "Open Auto Data Sync and refresh the universe."
        )
    else:
        symbols = st.multiselect(
            "Companies to scan",
            companies["symbol"].tolist(),
            default=companies["symbol"].tolist()[:25],
        )

        min_rows = st.number_input(
            "Minimum EOD rows",
            min_value=60,
            max_value=4000,
            value=120,
            step=20,
        )

        if st.button("Run AI Scan", type="primary"):
            if not symbols:
                st.warning("Select at least one company.")
            else:
                results = []
                skipped = []
                progress = st.progress(0)

                for i, symbol in enumerate(symbols, start=1):
                    try:
                        row, error = score_symbol(
                            symbol, int(min_rows)
                        )
                        if row:
                            results.append(row)
                        else:
                            skipped.append({
                                "Symbol": symbol,
                                "Reason": error,
                            })
                    except Exception as exc:
                        skipped.append({
                            "Symbol": symbol,
                            "Reason": (
                                f"{type(exc).__name__}: {exc}"
                            )[:250],
                        })

                    progress.progress(i / len(symbols))

                if results:
                    out = pd.DataFrame(results).sort_values(
                        "Estimated Probability: next close >= +5%",
                        ascending=False,
                    )

                    st.success(
                        f"Scored {len(results)} symbols; "
                        f"skipped {len(skipped)}."
                    )

                    st.dataframe(
                        out.style.format({
                            "Latest Close": "{:.2f}",
                            "Estimated Probability: next close >= +5%": "{:.1%}",
                            "Chronological Holdout Accuracy": "{:.1%}",
                            "Chronological Holdout Precision": "{:.1%}",
                            "Holdout Brier Score": "{:.3f}",
                        }),
                        use_container_width=True,
                    )

                    st.download_button(
                        "Download predictions CSV",
                        out.to_csv(index=False).encode("utf-8"),
                        "psx_ai_predictions.csv",
                        "text/csv",
                    )

                    with db() as con:
                        for result in results:
                            con.execute("""
                                INSERT INTO predictions
                                (run_timestamp, symbol, trade_date,
                                 reference_close, target_probability,
                                 target_definition, model_version,
                                 validation_accuracy,
                                 validation_precision, brier_score, note)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                now_pk().isoformat(),
                                result["Symbol"],
                                result["Latest EOD Date"],
                                result["Latest Close"],
                                result[
                                    "Estimated Probability: next close >= +5%"
                                ],
                                "Next session close >= latest EOD close * 1.05",
                                result["Model"],
                                result["Chronological Holdout Accuracy"],
                                result["Chronological Holdout Precision"],
                                result["Holdout Brier Score"],
                                "Experimental research only.",
                            ))
                else:
                    st.warning(
                        "No company had enough clean history to score."
                    )

                if skipped:
                    with st.expander(
                        f"Skipped symbols ({len(skipped)})"
                    ):
                        st.dataframe(
                            pd.DataFrame(skipped),
                            use_container_width=True,
                        )

elif page == "2. Auto Data Sync":
    st.title("🔄 Company Universe & EOD Data Sync")

    if psxdata is None:
        st.error(
            "psxdata is not installed. Check requirements.txt "
            "and redeploy."
        )
    else:
        st.success("psxdata package imported successfully.")

        col1, col2 = st.columns(2)

        with col1:
            if st.button("Refresh Company Universe"):
                try:
                    with st.spinner("Reading company list..."):
                        count, message = sync_company_universe()

                    log_sync(
                        "company universe refresh",
                        "success",
                        count,
                        message,
                    )

                    st.success(
                        f"{message} Records: {count}"
                    )

                except Exception as exc:
                    log_sync(
                        "company universe refresh",
                        "failed",
                        0,
                        str(exc),
                    )
                    st.error(
                        f"{type(exc).__name__}: {exc}"
                    )

        with col2:
            if st.button("Test Source (OGDC)"):
                try:
                    with st.spinner("Testing OGDC history..."):
                        test = fetch_symbol_history(
                            "OGDC", "2024-01-01"
                        )

                    if test.empty:
                        st.warning(
                            "Source returned no OGDC rows."
                        )
                    else:
                        st.success(
                            f"Test passed: {len(test)} rows."
                        )
                        st.dataframe(
                            test.tail(10),
                            use_container_width=True,
                        )

                except Exception as exc:
                    st.error(
                        f"{type(exc).__name__}: {exc}"
                    )

    st.divider()
    st.subheader("Download Historical EOD Data")

    companies = get_companies()

    if companies.empty:
        st.info(
            "First click Refresh Company Universe above."
        )
    else:
        selected = st.multiselect(
            "Symbols to sync",
            companies["symbol"].tolist(),
            default=companies["symbol"].tolist()[:5],
        )

        start_date = st.date_input(
            "Start date",
            value=date(2011, 1, 1),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
        )

        delay = st.slider(
            "Delay between symbols (seconds)",
            min_value=0.2,
            max_value=2.0,
            value=0.5,
            step=0.1,
        )

        if st.button(
            "Fetch / Update Selected EOD History",
            type="primary",
        ):
            if not selected:
                st.warning("Select at least one symbol.")
            elif psxdata is None:
                st.error("psxdata is not installed.")
            else:
                with st.spinner(
                    "Fetching data. Keep this page open."
                ):
                    succeeded, rows, failures = sync_history(
                        selected,
                        start_date.isoformat(),
                        delay,
                    )

                log_sync(
                    "EOD history sync",
                    "partial" if failures else "success",
                    rows,
                    f"success_symbols={succeeded}; "
                    f"failures={len(failures)}",
                )

                st.success(
                    f"Completed. Successful symbols: "
                    f"{succeeded}/{len(selected)}. "
                    f"Rows inserted/updated: {rows}."
                )

                if failures:
                    with st.expander(
                        f"Fetch failures ({len(failures)})"
                    ):
                        st.code("\n".join(failures))

elif page == "3. Company & Historical Data":
    st.title("🏢 Company & Historical Data")

    companies = get_companies(active_only=False)

    if companies.empty:
        st.info(
            "No companies stored yet. Use Auto Data Sync."
        )
    else:
        st.dataframe(
            companies, use_container_width=True
        )

        symbol = st.selectbox(
            "Inspect symbol",
            companies["symbol"].tolist(),
        )

        history = get_history(symbol)

        if history.empty:
            st.info(
                "No historical data stored for this company."
            )
        else:
            st.metric("Stored EOD rows", len(history))

            st.write(
                f"Date range: {history['trade_date'].min()} "
                f"to {history['trade_date'].max()}"
            )

            st.line_chart(
                history.set_index("trade_date")["close"]
            )

            st.dataframe(
                history.sort_values(
                    "trade_date", ascending=False
                ),
                use_container_width=True,
            )

            st.download_button(
                "Download history CSV",
                history.to_csv(index=False).encode("utf-8"),
                f"{symbol}_eod.csv",
                "text/csv",
            )

        all_history = get_history()

        if not all_history.empty:
            coverage = all_history.groupby("symbol").agg(
                rows=("trade_date", "count"),
                first_date=("trade_date", "min"),
                latest_date=("trade_date", "max"),
            ).reset_index().sort_values(
                "rows", ascending=False
            )

            st.subheader("Historical data coverage")
            st.dataframe(
                coverage, use_container_width=True
            )

elif page == "4. System Health":
    st.title("🛠️ System Health")

    companies = get_companies(active_only=False)
    history = get_history()

    with db() as con:
        prediction_count = con.execute(
            "SELECT COUNT(*) FROM predictions"
        ).fetchone()[0]

        logs = pd.read_sql_query(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 30",
            con,
        )

    a, b, c = st.columns(3)
    a.metric("Company records", len(companies))
    b.metric("EOD rows", len(history))
    c.metric("Saved predictions", prediction_count)

    st.write(
        "Data source:",
        "psxdata available"
        if psxdata is not None
        else "psxdata not installed",
    )

    if not history.empty:
        st.write(
            "Latest stored trade date:",
            history["trade_date"].max(),
        )
        st.write(
            "Symbols with history:",
            history["symbol"].nunique(),
        )

    st.subheader("Recent sync log")
    st.dataframe(logs, use_container_width=True)

    st.subheader("System limitations")
    st.markdown("""
    - This is an experimental research prototype.
    - Public data access can change or be throttled.
    - No intraday historical data is assumed.
    - Model probabilities are not guaranteed to be calibrated.
    - Walk-forward testing, corporate-action handling,
      trading costs and paper trading are still required.
    - Check PSX data licensing terms before redistribution.
    """)

st.caption(
    "PSX AI Research Terminal • Free-source prototype • "
    "No automatic live trading"
)
