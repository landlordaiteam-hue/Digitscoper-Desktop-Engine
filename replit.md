# Digitscoper Desktop Engine

Digitscoper is a standalone FastAPI desktop utility for local phone signal lookups, Pro watchlists, and SQLite administration.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the embedded FastAPI dashboard (port 8080 in the managed preview)
- `pnpm --filter @workspace/api-server run desktop` — open the dashboard in a native pywebview window
- `pnpm --filter @workspace/api-server run package:desktop` — create a one-file PyInstaller executable in `dist/`
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- The app creates `artifacts/api-server/digitscoper.db` automatically.
- Set `ADMIN_PASSWORD` before sharing the app to replace the development admin password.

## Stack

- Python 3.13, FastAPI, Uvicorn, bcrypt, SQLite, pywebview, and PyInstaller
- The application is intentionally kept as a single-file monolith at `artifacts/api-server/main.py`.

## Where things live

- `artifacts/api-server/main.py` — database schema, FastAPI routes, native launcher, and embedded HTML/CSS/JavaScript UI
- `artifacts/api-server/.replit-artifact/artifact.toml` — managed preview and production service configuration
- `pyproject.toml` — Python runtime dependencies installed for the project

## Architecture decisions

- SQLite stays next to the executable so the desktop build remains portable and persists lookup history locally.
- User passwords are stored only as bcrypt hashes; Pro access is checked server-side on every save/dashboard request.
- Phone metadata is a deterministic local signal index so the app works without an external API key or network dependency.
- The same FastAPI app serves both the managed `/api` preview prefix and the root URL used by the desktop window.

## Product

- Unified phone lookup with carrier, line, region, risk, business, directory, and public-record signals
- Pro login with automatic lookup history, saved number metadata, saved patterns, a four-digit pattern builder, and simple usage analytics
- Admin lookup ledger inspection and Pro user creation/update
- Responsive dark dashboard with Lookup, Pro, Admin tabs and live session tracking

## User preferences

- Keep the application as a single-file monolith; do not split the Python app into modules.

## Gotchas

- The default seeded credentials are for local development only: `ronald@example.com` / `password123` and admin password `admin123`.
- `pywebview` should be started with `--desktop`; the managed preview runs headlessly with Uvicorn.
- Existing SQLite databases are upgraded in place when new Pro metadata columns are introduced.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
