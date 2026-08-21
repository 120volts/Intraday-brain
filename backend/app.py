import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
DB_PATH = Path(os.getenv("DB_PATH", "intraday.db"))
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]
PAPER_START = float(os.getenv("PAPER_START", "25000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))

app = FastAPI(title="Intraday Brain API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
scanner_cache = {"created_at": None, "data": None}

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
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
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
    x["vwap"] = (x.c * x.v).cumsum() / x.v.cumsum()
    x["vol20"] = x.v.rolling(20).mean()
    x["rvol"] = x.v / x.vol20
    x["ema9"] = x.c.ewm(span=9, adjust=False).mean()
    x["ema20"] = x.c.ewm(span=20, adjust=False).mean()

    latest = x.iloc[-1]
    first = x.iloc[0]
    opening = x.head(min(6, len(x)))
    or_high = float(opening.h.max())
    morning_move = (float(latest.c) / float(first.o) - 1) * 100

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
    return {"symbol": None, "price": float(latest.c), "vwap": float(latest.vwap), "rvol": float(latest.rvol) if not pd.isna(latest.rvol) else 0, "score": score, "state": state, "reason": ", ".join(reasons) or "No qualifying conditions", "updated_at": latest.t.isoformat()}


def cache_is_fresh():
    created_at = scanner_cache["created_at"]
    return created_at and (datetime.now(timezone.utc) - created_at).total_seconds() < 300


def log_signals(rows):
    created_at = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.executemany(
            "INSERT INTO signals(symbol,price,vwap,rvol,score,state,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [(row["symbol"], row["price"], row["vwap"], row["rvol"], row["score"], row["state"], row["reason"], created_at) for row in rows],
        )


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
    if cache_is_fresh():
        return scanner_cache["data"]
    if not os.getenv("APCA_API_KEY_ID"):
        return [{"symbol": s, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "NO DATA", "reason": "Configure Alpaca paper-data credentials on the server"} for s in WATCHLIST]
    out = []
    for symbol in WATCHLIST:
        try:
            x = score_frame(await bars(symbol))
            if x is None:
                raise ValueError("Not enough 5-minute bars returned")
            x["symbol"] = symbol
            out.append(x)
        except Exception as e:
            out.append({"symbol": symbol, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "ERROR", "reason": str(e)})
    out.sort(key=lambda z: z["score"], reverse=True)
    log_signals(out)
    scanner_cache["created_at"] = datetime.now(timezone.utc)
    scanner_cache["data"] = out
    return out


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
