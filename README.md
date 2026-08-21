# Intraday Brain dashboard

This is the phone-first front end for the paper-only trading system.

## Run locally

Any static web server can serve it. For example:

    python -m http.server 8080

Then open `http://YOUR-COMPUTER-IP:8080` on your phone while both devices are on the same network. The dashboard automatically uses the same computer on port `8000` for its API.

Start the backend first, following [backend/README.md](backend/README.md). The dashboard gets its status, scanner, positions, and history from that API; it does not contain demo market values.

To point the dashboard at a different API host, add `?api=http://HOST:8000` to the dashboard URL. This is useful only when the static site and API are on different hosts.

For internet access, deploy the folder to a HTTPS static host.

## Important

This build reads scanner data from the FastAPI backend. Paper order execution is intentionally disabled in the dashboard while the scanner is validated. It does not connect to Fidelity and cannot place real orders.

Do not add Fidelity credentials to this frontend.
