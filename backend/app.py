import os
import sqlite3
from datetime import datetime, timezone, time as dtime
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
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))
ET = ZoneInfo("America/New_York")

app = FastAPI(title="Intraday Brain API", version="0.4.0")
# GitHub Pages hosts the phone dashboard. Keep this explicit rather than relying on browser defaults.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://120volts.github.io", "http://localhost", "http://127.0.0.1"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

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
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

async def yahoo_chart(symbol: str, interval: str = "5m", range_: str = "1d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": range_, "includePrePost": "true", "events": "div,splits"}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        payload = r.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError("Yahoo returned no chart data")
    return result[0]

def yahoo_rows(result):
    ts = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, stamp in enumerate(ts):
        def val(name):
            arr = q.get(name) or []
            return arr[i] if i < len(arr) else None
        close = val("close")
        if close is None:
            continue
        rows.append({
            "t": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "ts": stamp,
            "o": val("open"), "h": val("high"), "l": val("low"),
            "c": close, "v": val("volume") or 0,
        })
    return rows

def is_regular(ts):
    dt = datetime.fromtimestamp(ts, ET)
    return dt.weekday() < 5 and dtime(9, 30) <= dt.time() < dtime(16, 0)

def ema(values, span):
    if not values:
        return None
    alpha = 2 / (span + 1)
    e = float(values[0])
    for v in values[1:]:
        e = alpha * float(v) + (1 - alpha) * e
    return e

def enrich(rows):
    regular = [r for r in rows if r.get("c") is not None and is_regular(r["ts"])]
    if not regular:
        return rows, []
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    closes = []
    volumes = []
    enriched = []
    session = None
    for r in regular:
        dt = datetime.fromtimestamp(r["ts"], ET)
        day = dt.date()
        if day != session:
            session = day
            cumulative_pv = 0.0
            cumulative_vol = 0.0
            closes = []
            volumes = []
        typical = (float(r["h"]) + float(r["l"]) + float(r["c"])) / 3
        cumulative_pv += typical * float(r["v"])
        cumulative_vol += float(r["v"])
        closes.append(float(r["c"]))
        volumes.append(float(r["v"]))
        r = dict(r)
        r["vwap"] = cumulative_pv / cumulative_vol if cumulative_vol else float(r["c"])
        r["ema9"] = ema(closes, 9)
        r["ema20"] = ema(closes, 20)
        prior = volumes[-21:-1]
        avg_vol = sum(prior) / len(prior) if prior else None
        r["rvol"] = float(r["v"]) / avg_vol if avg_vol and avg_vol > 0 else None
        r["et"] = dt.strftime("%H:%M")
        enriched.append(r)
    return rows, enriched

def score_rows(enriched):
    if not enriched:
        return None
    latest = enriched[-1]
    today = datetime.fromtimestamp(latest["ts"], ET).date()
    today_rows = [r for r in enriched if datetime.fromtimestamp(r["ts"], ET).date() == today]
    opening = today_rows[:6]
    if not opening:
        opening = today_rows
    or_high = max(float(r["h"]) for r in opening)
    first_open = float(opening[0]["o"])
    c = float(latest["c"])
    score = 0
    reasons = []
    if c > float(latest["vwap"]): score += 20; reasons.append("above VWAP")
    if latest["ema9"] > latest["ema20"]: score += 15; reasons.append("EMA9 > EMA20")
    if latest["rvol"] is not None and latest["rvol"] >= 1.25: score += 20; reasons.append("relative volume")
    if c >= or_high: score += 15; reasons.append("opening-range strength")
    if c / first_open - 1 >= 0.01: score += 15; reasons.append("morning expansion")
    if c > float(latest["o"]): score += 10; reasons.append("green bar")
    state = "SETUP" if score >= 70 else "TREND" if score >= 60 else "WAIT"
    return {
        "price": c, "vwap": float(latest["vwap"]), "rvol": latest["rvol"],
        "ema9": float(latest["ema9"]), "ema20": float(latest["ema20"]),
        "opening_range_high": or_high, "score": score, "state": state,
        "reason": ", ".join(reasons) or "No qualifying setup conditions yet",
        "bar_time": latest["t"], "source": "Yahoo Finance",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

def yahoo_price(result, rows):
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    now = datetime.now(ET).time()
    if now < dtime(9, 30):
        price = meta.get("preMarketPrice") or price
    elif now >= dtime(16, 0):
        price = meta.get("postMarketPrice") or price
    if price is None and rows:
        price = rows[-1].get("c")
    return float(price) if price is not None else None

def warmup(symbol, result, rows):
    price = yahoo_price(result, rows)
    if price is None:
        raise RuntimeError("Yahoo returned no current price")
    meta = result.get("meta") or {}
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    change_pct = (price / float(prev) - 1) * 100 if prev else None
    return {"symbol": symbol, "price": price, "vwap": None, "rvol": None, "ema9": None, "ema20": None, "opening_range_high": None, "score": 0, "state": "WARMING UP", "reason": "Price available; waiting for enough regular-session bars to calculate the setup", "change_pct": change_pct, "source": "Yahoo Finance", "updated_at": datetime.now(timezone.utc).isoformat()}

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
    out = []
    for symbol in WATCHLIST:
        try:
            result = await yahoo_chart(symbol, "5m", "1d")
            rows = yahoo_rows(result)
            _, enriched = enrich(rows)
            x = {"symbol": symbol, **(score_rows(enriched) or warmup(symbol, result, rows))}
            out.append(x)
            with db() as c:
                c.execute("INSERT INTO signals(symbol,price,vwap,rvol,score,state,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (symbol, x.get("price"), x.get("vwap"), x.get("rvol"), x.get("score", 0), x.get("state"), x.get("reason"), datetime.now(timezone.utc).isoformat()))
        except Exception as e:
            out.append({"symbol": symbol, "price": None, "vwap": None, "rvol": None, "ema9": None, "ema20": None, "score": 0, "state": "ERROR", "reason": f"Yahoo data error: {e}"})
    out.sort(key=lambda z: z.get("score", 0), reverse=True)
    return out

@app.get("/api/chart/{symbol}")
async def chart(symbol: str):
    symbol = symbol.upper().strip()
    if symbol not in WATCHLIST:
        raise HTTPException(404, "Symbol is not on the watchlist")
    result = await yahoo_chart(symbol, "5m", "1d")
    rows = yahoo_rows(result)
    _, enriched = enrich(rows)
    return {"symbol": symbol, "source": "Yahoo Finance", "bars": enriched, "analysis": score_rows(enriched)}

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
