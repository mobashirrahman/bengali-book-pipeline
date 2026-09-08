# Folio frontend

This directory contains the React + TypeScript + Vite frontend for the Folio book-streaming experience. It remains a separate package and consumes the normalized catalogue API, including its protected document content and download endpoints.

## Run locally

Start the catalogue API first, from the repository root, using the existing
normalized database and the approved `epub-staging` content root:

```bash
.venv/bin/python -m pdf_craft.catalogue.cli serve \
  --db pdf-craft-output/catalogue/catalogue.db \
  --content-root pdf-craft-output/catalogue/epub-staging \
  --host 127.0.0.1 --port 8000
```

Then start the frontend:

```bash
cd frontend
npm install
npm run dev
```

The development server is available at `http://127.0.0.1:5173/`. It binds
explicitly to loopback (`127.0.0.1`) on port `5173` with `strictPort`, and is
intentionally not reachable from other machines. `vite preview` uses the same
loopback binding on port `4173` with `strictPort`.

The app calls the normalized catalogue at the same origin by default, and the
dev (and preview) server proxies only `/v2` requests to the local API at
`http://127.0.0.1:8000`. For local usage, leave `VITE_API_BASE_URL` unset so
requests go through that proxy, and make sure `VITE_DEMO_MODE` is not `true`
so the app uses live catalogue data instead of the deterministic demo set.

For remote access, forward only the frontend port (5173) over SSH; keep both
listeners on loopback and do not assume port 8000 is forwarded. The default
local invocation is plain `npm run dev` with the override unset. Setting an
absolute browser-facing `VITE_API_BASE_URL` is an advanced override: it
bypasses the local proxy, so the endpoint must be separately reachable from
the browser and must list the frontend origin in its CORS configuration.

To preview the deterministic local catalogue without making an API request, use `VITE_DEMO_MODE=true`. If the health check or initial catalogue request fails, the app switches to the same explicit demo mode and shows the reason in the status banner. Demo records are labeled as demo records throughout the UI.

## Routes

- `/` — featured home and horizontal shelves
- `/discover` — search and results grid; supports `?q=`
- `/works/:id` — work detail, cover provenance, catalogue ratings, and a sync-aware personal rating control
- `/read/:id` — protected PDF reader/download or in-app EPUB reader; demo records show an explicit preview shell
- `/shelf` — books saved on this device; demo and live shelves are stored separately
- any other path — a not-found page with exits back home or to discovery

Request failures stay explicit: the typed adapter normalizes network errors into `ApiError` (status `0`) while preserving `AbortError` cancellation, and every view separates loading, empty, and failure states with retry actions where a retry can help. Search and work-detail requests are guarded so a stale response can never overwrite newer content. The API contract is unchanged — same `VITE_API_BASE_URL` base, same `/v2` endpoints, no invented routes.

The typed adapter in `src/lib/api.ts` covers `/v2/health`, `/v2/stats`, `/v2/search`, `/v2/works`, `/v2/works/:id/ratings`, `/v2/editions`, `/v2/documents`, and selected asset content, including `AbortSignal` cancellation, HTTP errors, and the API's `after` cursors. Protected document URLs are built from `VITE_API_BASE_URL` at `/v2/documents/:id/content` and `/v2/documents/:id/download`; browser credentials/cookies remain available to those URLs. Personal rating writes pass backend errors through the UI; the default backend's 401 is shown as `Sign in to sync`.

Live PDF documents open in a browser iframe and expose a download action. Live EPUB documents are rendered in the app through `epubjs`. Unsupported or missing accepted documents remain visible as unavailable states.

## Checks

```bash
npm run typecheck
npm test
npm run build
```
