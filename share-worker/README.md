# crema community shot pool (Cloudflare Worker)

Collection endpoint for opt-in [`crema share`](../README.md) bundles. Stores
each submission in a private R2 bucket as
`bundles/<install_id>/<timestamp>-<uuid>.json`. Collection only — curating and
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
