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

## Data provider

The first adapter uses Alpaca because it has an official paper-trading API and a documented 5-minute historical stock-bars endpoint. The default Basic feed is IEX-only, so this is suitable for development/paper testing but should not be treated as consolidated U.S. market coverage. See the project's market-data configuration before relying on signals.

## Safety

This backend is paper-only. It does not contain Fidelity credentials and it does not submit orders to Fidelity. The next execution layer should remain disabled until the strategy has passed historical and live paper tests.
