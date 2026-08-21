import asyncio
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "intraday.db")))
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]
PAPER_START = float(os.getenv("PAPER_START", "25000"))
NY = ZoneInfo("America/New_York")

app = FastAPI(title="Intraday Brain API", version="0.4.0")
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

async def yahoo_chart(symbol: str, interval: str = "5m", range_: str = "1d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_, "interval": interval, "includePrePost": "false", "events": "div,splits"}
    headers = {"User-Agent": "Mozilla/5.0 Intraday-Brain/0.4"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        payload = r.json()
    result = (payload.get("chart") or {}).get("result")
    if not result:
        err = (payload.get("chart") or {}).get("error") or {}
        raise RuntimeError(err.get("description") or "Yahoo returned no chart data")
    result = result[0]
    timestamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
    bars = []
    for i, ts in enumerate(timestamps):
        o = q.get("open", [None] * len(timestamps))[i]
        h = q.get("high", [None] * len(timestamps))[i]
        l = q.get("low", [None] * len(timestamps))[i]
        c = q.get("close", [None] * len(timestamps))[i]
        v = q.get("volume", [None] * len(timestamps))[i]
        if any(x is None for x in (o, h, l, c)):
            continue
        dt = datetime.fromtimestamp(ts, timezone.utc)
        local = dt.astimezone(NY)
        if local.weekday() >= 5 or not (9 * 60 + 30 <= local.hour * 60 + local.minute < 16 * 60):
            continue
        bars.append({"t": dt.isoformat(), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0)})
    return bars

def ema(values, span):
    if not values:
        return None
    alpha = 2 / (span + 1)
    e = float(values[0])
    for value in values[1:]:
        e = alpha * float(value) + (1 - alpha) * e
    return e

def score_bars(bars):
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    session_date = datetime.fromisoformat(bars[-1]["t"]).astimezone(NY).date()
    today = [b for b in bars if datetime.fromisoformat(b["t"]).astimezone(NY).date() == session_date]
    if not today:
        today = bars
    pv = sum(b["c"] * b["v"] for b in today)
    vol = sum(b["v"] for b in today)
    vwap = pv / vol if vol else None
    latest = today[-1]
    recent = volumes[-5:]
    avg_vol = sum(recent) / len(recent) if recent else 0
    rvol = latest["v"] / avg_vol if avg_vol else None
    e9 = ema(closes[-60:], 9)
    e20 = ema(closes[-60:], 20)
    opening = today[:6]
    or_high = max(b["h"] for b in opening) if opening else latest["h"]
    or_low = min(b["l"] for b in opening) if opening else latest["l"]
    first_open = opening[0]["o"] if opening else latest["o"]
    morning_move = (latest["c"] / first_open - 1) * 100 if first_open else 0
    score = 0
    reasons = []
    if vwap is not None and latest["c"] > vwap:
        score += 20; reasons.append("above VWAP")
    if e9 is not None and e20 is not None and e9 > e20:
        score += 15; reasons.append("EMA9 > EMA20")
    if rvol is not None and rvol >= 1.25:
        score += 20; reasons.append("relative volume elevated")
    if latest["c"] >= or_high:
        score += 15; reasons.append("opening-range strength")
    if morning_move >= 1:
        score += 15; reasons.append("morning expansion")
    if latest["c"] > latest["o"]:
        score += 10; reasons.append("green bar")
    state = "SETUP" if score >= 70 else "TREND" if score >= 60 else "WAIT"
    return {
        "price": latest["c"], "vwap": vwap, "rvol": rvol, "ema9": e9, "ema20": e20,
        "opening_high": or_high, "opening_low": or_low, "morning_move_pct": morning_move,
        "score": score, "state": state,
        "reason": ", ".join(reasons) if reasons else "No qualifying setup factors yet",
        "updated_at": latest["t"], "bar_count": len(today)
    }

@app.on_event("startup")
def startup():
    init_db()

@app.get("/api/health")
def health():
    return {"ok": True, "paper_mode": get_setting("paper_mode") == "on", "time": datetime.now(timezone.utc).isoformat(), "data_source": "Yahoo Finance"}

@app.get("/api/status")
def status():
    with db() as c:
        open_count = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED'").fetchone()[0]
        signals = c.execute("SELECT COUNT(*) FROM signals WHERE created_at >= date('now')").fetchone()[0]
    return {"paper_mode": get_setting("paper_mode") == "on", "balance": float(get_setting("paper_balance") or PAPER_START), "today_pnl": float(pnl), "open_trades": open_count, "signals": signals, "data_source": "Yahoo Finance"}

async def scan_one(symbol):
    try:
        bars = await yahoo_chart(symbol, "5m", "1d")
        scored = score_bars(bars)
        if scored is None:
            return {"symbol": symbol, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "NO DATA", "reason": "Yahoo returned no regular-session bars"}
        return {"symbol": symbol, **scored}
    except Exception as e:
        return {"symbol": symbol, "price": None, "vwap": None, "rvol": None, "score": 0, "state": "ERROR", "reason": f"Yahoo: {e}"}

@app.get("/api/scanner")
async def scanner():
    out = await asyncio.gather(*(scan_one(s) for s in WATCHLIST))
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        for x in out:
            c.execute("INSERT INTO signals(symbol,price,vwap,rvol,score,state,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (x.get("symbol"), x.get("price"), x.get("vwap"), x.get("rvol"), x.get("score", 0), x.get("state"), x.get("reason"), now))
    return sorted(out, key=lambda z: z.get("score", 0), reverse=True)

@app.get("/api/chart/{symbol}")
async def chart(symbol: str):
    bars = await yahoo_chart(symbol.upper(), "5m", "1d")
    if not bars:
        raise HTTPException(404, "No Yahoo Finance regular-session bars")
    return {"symbol": symbol.upper(), "source": "Yahoo Finance", "bars": bars, "indicators": score_bars(bars)}

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
        raise HTTPException(400, "Only long paper trades are supported")
    if t.qty < 1 or t.entry <= t.stop or t.target <= t.entry:
        raise HTTPException(400, "Invalid trade geometry")
    with db() as c:
        open_count = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        if open_count >= 2:
            raise HTTPException(409, "Maximum open trades reached")
        c.execute("INSERT INTO trades(symbol,side,qty,entry,stop,target,score,reason,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (t.symbol.upper(), "BUY", t.qty, t.entry, t.stop, t.target, t.score, t.reason, "OPEN", datetime.now(timezone.utc).isoformat()))
        trade_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"ok": True, "trade_id": trade_id, "paper_only": True}

@app.post("/api/paper/toggle")
def paper_toggle():
    new = "off" if get_setting("paper_mode") == "on" else "on"
    set_setting("paper_mode", new)
    return {"paper_mode": new == "on"}
