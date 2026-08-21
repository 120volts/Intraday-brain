import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "intraday.db")))
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]
PAPER_START = float(os.getenv("PAPER_START", "25000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))

app = FastAPI(title="Intraday Brain API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class PaperTrade(BaseModel):
    symbol: str
    side: str = "buy"
    qty: int
    entry: float
    stop: float
    target: float
    score: int = 0
    reason: str = "manual paper signal"


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty INTEGER, entry REAL, exit REAL, stop REAL, target REAL, score INTEGER, reason TEXT, status TEXT, opened_at TEXT, closed_at TEXT, pnl REAL DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, price REAL, vwap REAL, rvol REAL, score INTEGER, state TEXT, reason TEXT, created_at TEXT)")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('paper_balance', ?)", (str(PAPER_START),))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('paper_mode','on')")


def get_setting(key):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def alpaca_headers():
    # Accept both the official APCA_* names and the simpler names we used
    # earlier while setting up the local .env file.
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Alpaca API credentials are not configured")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


async def bars(symbol: str, limit: int = 100):
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {"timeframe": "5Min", "limit": limit, "feed": os.getenv("ALPACA_FEED", "iex")}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=alpaca_headers(), params=params)
        r.raise_for_status()
        data = r.json().get("bars", [])
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def score_frame(df: pd.DataFrame):
    if df.empty or len(df) < 20:
        return None
    x = df.copy()
    x["t"] = pd.to_datetime(x["t"], utc=True)
    x["session"] = x["t"].dt.date
    # VWAP resets each trading session instead of accumulating across days.
    x["pv"] = x.c * x.v
    x["vwap"] = x.groupby("session")["pv"].cumsum() / x.groupby("session")["v"].cumsum()
    x["vol20"] = x.v.rolling(20).mean()
    x["rvol"] = x.v / x.vol20
    x["ema9"] = x.c.ewm(span=9, adjust=False).mean()
    x["ema20"] = x.c.ewm(span=20, adjust=False).mean()

    latest = x.iloc[-1]
    today = x[x.session == latest.session]
    opening = today.head(min(6, len(today)))
    if opening.empty:
        opening = x.tail(min(6, len(x)))
    or_high = float(opening.h.max())
    first_open = float(opening.iloc[0].o)
    morning_move = (float(latest.c) / first_open - 1) * 100

    score = 0
    reasons = []
    if latest.c > latest.vwap:
        score += 20; reasons.append("above VWAP")
    if latest.ema9 > latest.ema20:
        score += 15; reasons.append("EMA9 > EMA20")
    if float(latest.rvol or 0) >= 1.25:
        score += 20; reasons.append("relative volume")
    if latest.c >= or_high:
        score += 15; reasons.append("opening-range strength")
    if morning_move >= 1:
        score += 15; reasons.append("morning expansion")
    if latest.c > latest.o:
        score += 10; reasons.append("green bar")
    state = "SETUP" if score >= 70 else "TREND" if score >= 60 else "WAIT"
    return {"symbol": None, "price": float(latest.c), "vwap": float(latest.vwap), "rvol": float(latest.rvol) if not pd.isna(latest.rvol) else 0, "score": score, "state": state, "reason": ", ".join(reasons), "updated_at": latest.t.isoformat()}


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"ok": True, "paper_mode": get_setting("paper_mode") == "on", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status():
    with db() as c:
        open_count = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED'").fetchone()[0]
        signals = c.execute("SELECT COUNT(*) FROM signals WHERE created_at >= date('now')").fetchone()[0]
    return {"paper_mode": get_setting("paper_mode") == "on", "balance": float(get_setting("paper_balance") or PAPER_START), "today_pnl": float(pnl), "open_trades": open_count, "signals": signals}


@app.get("/api/scanner")
async def scanner():
    if not (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")):
        return [{"symbol": s, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "NO DATA", "reason": "Configure Alpaca paper-data credentials on the server"} for s in WATCHLIST]
    out = []
    for symbol in WATCHLIST:
        try:
            x = score_frame(await bars(symbol))
            if x is None:
                raise RuntimeError("Not enough bars returned")
            x["symbol"] = symbol
            out.append(x)
            with db() as c:
                c.execute("INSERT INTO signals(symbol,price,vwap,rvol,score,state,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (symbol, x["price"], x["vwap"], x["rvol"], x["score"], x["state"], x["reason"], datetime.now(timezone.utc).isoformat()))
        except Exception as e:
            out.append({"symbol": symbol, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "ERROR", "reason": str(e)})
    out.sort(key=lambda z: z["score"], reverse=True)
    return out


@app.get("/api/activity")
def activity():
    with db() as c:
        rows = c.execute("SELECT symbol,price,vwap,rvol,score,state,reason,created_at FROM signals ORDER BY id DESC LIMIT 40").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/positions")
def positions():
    with db() as c:
        rows = c.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/history")
def history():
    with db() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/paper/trade")
def paper_trade(t: PaperTrade):
    if get_setting("paper_mode") != "on":
        raise HTTPException(409, "Paper trading is paused")
    if t.side.lower() != "buy":
        raise HTTPException(400, "v0.1 only supports long paper trades")
    if t.qty < 1 or t.entry <= t.stop or t.target <= t.entry:
        raise HTTPException(400, "Invalid trade geometry")
    with db() as c:
        open_count = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        if open_count >= 2:
            raise HTTPException(409, "Maximum open trades reached")
        c.execute("INSERT INTO trades(symbol,side,qty,entry,stop,target,score,reason,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (t.symbol.upper(), "BUY", t.qty, t.entry, t.stop, t.target, t.score, t.reason, "OPEN", datetime.now(timezone.utc).isoformat()))
        trade_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"ok": True, "trade_id": trade_id}


@app.post("/api/paper/toggle")
def paper_toggle():
    new = "off" if get_setting("paper_mode") == "on" else "on"
    set_setting("paper_mode", new)
    return {"paper_mode": new == "on"}
