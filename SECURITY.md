# Security Policy

This project signs requests to a real-money exchange. Treat your credentials accordingly.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems. Use
[GitHub private vulnerability reporting](https://github.com/ryanfrigo/kalshi-ai-trading-bot/security/advisories/new)
instead. You'll get a response within a few days. Anything that could cause
unintended order placement, credential exposure, or position oversizing is in scope.

## Handling Your Credentials

- **Never commit** `.env`, `kalshi_private_key`, any `*.pem` file, or backup
  archives. Archives can contain private keys or databases and are not reliably
  inspected by text-only secret scanners.
- If a key ever lands in a commit (even one you amended away), **rotate it
  immediately** in your Kalshi account settings. Public git history is forever.
- Use [Kalshi's demo environment](https://demo.kalshi.co) for development and
  experiments before pointing anything at production.
- The signals-only server uses a GET-only account client. The top-level CLI
  rejects live execution, the shared Kalshi client rejects every non-GET
  request, and the overnight canary launchers are disabled in this build.
  Order submission and cancellation are unavailable.

## Test Suite Safety

A plain `pytest` run is safe: tests that touch the real Kalshi API or spend LLM
credits are marked `live` and skipped. Never opt into live tests; some legacy
tests can place real orders. The signals-only deployment test path must not use
live credentials.
