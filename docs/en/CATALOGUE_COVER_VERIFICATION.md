# Cover verification

Cover URLs staged by catalogue materialization remain metadata-only until the
bounded verifier is explicitly run. The verifier processes existing `cover`
candidate rows in ascending asset ID order, so a later invocation can resume
with the previous `next_after_id` value.

```bash
python -m pdf_craft.catalogue.cli cover verify-batch \
  --db catalogue.db --asset-root pdf-craft-output/assets \
  --limit 100 --after-id 0
```

Each response is streamed with a byte limit and timeout. Redirects, local or
private-network targets, unsupported MIME types, HTML placeholders, malformed
images, and images below `600x900` are rejected. Validated bytes are stored
under the supplied asset root using a SHA-256 path. The existing candidate row
records its verification status, dimensions, digest, timestamp, and source
provenance; rerunning a completed row is idempotent.

Only validated local assets can be selected or served by the API. Remote
candidate URLs are never used directly as browser cover URLs.

The verifier preserves an existing manual cover selection. Automatic choices
apply only to editions without a manual selection. Validation currently checks
image headers and dimensions; full image decoding and high-resolution source
acquisition remain outstanding. No production cover batch has been run.

The frontend goal remains incomplete: authentication, Goodreads acquisition,
working personal shelves, complete catalogue pagination, and browser-level
reader validation are still required. The existing Rokomari candidates are
predominantly thumbnails, not verified high-resolution assets.
