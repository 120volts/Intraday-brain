# Intraday Brain backend

This is the server-side paper-trading API for the iPhone dashboard.

## Current capabilities

- FastAPI HTTP API
- SQLite trade/signal database
- Alpaca 5-minute stock-bar data adapter
- VWAP, relative volume and EMA calculations
- Setup scoring
- Paper-position creation
- Position/history/status endpoints
- Docker image

The scanner reads data only. It does not submit Alpaca orders, and there is no Fidelity integration.

## Run locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Put Alpaca PAPER credentials in .env
uvicorn app:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.

## Dashboard endpoints

- `GET /api/status` returns paper-mode state and local paper-account metrics.
- `GET /api/scanner` fetches and scores the watchlist, caching each scan for five minutes and logging the resulting signals to SQLite.
- `GET /api/positions` returns locally recorded open paper positions.
- `GET /api/history` returns locally recorded paper-trade history.

Use `http://localhost:8000/docs` on the Mac to inspect and test the API. Credentials stay only in `backend/.env`, which is ignored by Git.

## Data provider

The first adapter uses Alpaca because it has an official paper-trading API and a documented 5-minute historical stock-bars endpoint. The default Basic feed is IEX-only, so this is suitable for development/paper testing but should not be treated as consolidated U.S. market coverage. See the project's market-data configuration before relying on signals.

## Safety

This backend is paper-only. It does not contain Fidelity credentials and it does not submit orders to Fidelity. The next execution layer should remain disabled until the strategy has passed historical and live paper tests.
