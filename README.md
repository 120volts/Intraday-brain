# Intraday Brain dashboard

This is the phone-first front end for the trading system.

## Run locally

Any static web server can serve it. For example:

    python -m http.server 8080

Then open http://YOUR-COMPUTER-IP:8080 on your phone while both devices are on the same network.

For internet access, deploy the folder to a HTTPS static host.

## Important

This build is a UI prototype with simulated paper trades. It does not connect to Fidelity and cannot place real orders.

The next backend will provide:
- live market data
- scanner calculations
- strategy signals
- paper execution
- trade database
- performance metrics
- authentication
- server-side risk controls

Do not add Fidelity credentials to this frontend.
