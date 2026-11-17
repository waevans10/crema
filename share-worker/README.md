# crema community shot pool (Cloudflare Worker)

Collection endpoint for opt-in [`crema share`](../README.md) bundles. Stores
each submission in a private R2 bucket as
`bundles/<participant_id>/<timestamp>-<uuid>.json`. Collection only — curating and
publishing the pooled dataset (CC BY-NC 4.0) is a manual step.

## Deploy (one time)

```bash
cd share-worker
npm install
npx wrangler login                     # opens the browser
npx wrangler r2 bucket create crema-pool
npx wrangler deploy                    # prints the workers.dev URL
```

Then point crema installs at it in `.env`:

```
CREMA_SHARE_URL=https://crema-pool.<your-subdomain>.workers.dev/v1/bundles
```

## Notes

- Free tier is plenty: bundles are ~100 KB–2 MB, capped at 10 MB.
- `npm run check` type-checks; `npm run dev` serves locally for testing:
  `curl -X POST localhost:8787/v1/bundles -H 'content-type: application/json' -d @../crema-export-*.json`
- Bundles are private in R2. Nothing is served back out by this Worker.

## Bundle versions and verification

The collector now requires schema version **2**, `participant_id` (a UUID), and
version-2 consent with a timezone-bearing timestamp. It checks the envelope and
snapshot references; the Python importer validates the full nested record schema.
Uploads stay private and are not published automatically.

Existing version-1 objects in R2 are left untouched. New version-1 submissions
receive HTTP 400 with an upgrade message. Keep legacy objects separate from the
canonical version-2 dataset; do not relabel them or infer new consent. Users of
older clients must upgrade and accept the version-2 terms before sharing again.
Deploy this collector update before enabling version-2 uploads from clients.

The Python importer accepts both local version-2 exports and consent-stamped
version-2 submissions. Each import atomically replaces that participant's current
snapshot, including removing stale shots/experiments. Import submissions in
collection order when rebuilding a current-state pool. R2 keeps the original
submissions for historical analysis; shot indices are only meaningful within one
snapshot and must not be joined across submissions.

From the repository root, with Node 22.18+ and Python dependencies installed:

```bash
npm ci --ignore-scripts --prefix share-worker
npm run check --prefix share-worker
uv run npm --prefix share-worker test
```

The contract test generates a bundle with the actual Python exporter, stores it
through the Worker using a fake R2 bucket, then imports those exact bytes. It also
checks malformed/legacy submissions and storage failures. No remote service is used.
