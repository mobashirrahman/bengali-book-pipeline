# Folio frontend

This directory contains the React + TypeScript + Vite frontend for the Folio book-streaming experience. It remains a separate package and consumes the normalized catalogue API, including its protected document content and download endpoints.

## Run locally

```bash
cd frontend
npm install
npm run dev
```

The app calls the normalized catalogue at the same origin by default. Set `VITE_API_BASE_URL` when the API is on another origin:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

To preview the deterministic local catalogue without making an API request, use `VITE_DEMO_MODE=true`. If the health check or initial catalogue request fails, the app switches to the same explicit demo mode and shows the reason in the status banner. Demo records are labeled as demo records throughout the UI.

## Routes

- `/` — featured home and horizontal shelves
- `/discover` — search and results grid; supports `?q=`
- `/works/:id` — work detail, cover provenance, catalogue ratings, and a sync-aware personal rating control
- `/read/:id` — protected PDF reader/download or in-app EPUB reader; demo records show an explicit preview shell

The typed adapter in `src/lib/api.ts` covers `/v2/health`, `/v2/stats`, `/v2/search`, `/v2/works`, `/v2/works/:id/ratings`, `/v2/editions`, `/v2/documents`, and selected asset content, including `AbortSignal` cancellation, HTTP errors, and the API's `after` cursors. Protected document URLs are built from `VITE_API_BASE_URL` at `/v2/documents/:id/content` and `/v2/documents/:id/download`; browser credentials/cookies remain available to those URLs. Personal rating writes pass backend errors through the UI; the default backend's 401 is shown as `Sign in to sync`.

Live PDF documents open in a browser iframe and expose a download action. Live EPUB documents are rendered in the app through `epubjs`. Unsupported or missing accepted documents remain visible as unavailable states.

## Checks

```bash
npm run typecheck
npm test
npm run build
```
