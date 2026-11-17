/**
 * crema community shot pool — accepts opt-in `crema share` bundles into R2.
 *
 * POST /v1/bundles   JSON bundle (schema_version 2) → stored as
 *                    bundles/<participant_id>/<timestamp>-<uuid>.json
 *
 * Bundles are private in R2; curation/publication of the pooled dataset
 * (CC BY-NC 4.0) is a manual, offline step — this endpoint only collects.
 */

interface Env {
  POOL: R2Bucket;
}

// A whole home installation's history is well under this; anything bigger
// is not a crema bundle.
const MAX_BUNDLE_BYTES = 10 * 1024 * 1024;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function json(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/v1/bundles") {
      return json(404, { ok: false, error: "not found" });
    }
    if (request.method !== "POST") {
      return json(405, { ok: false, error: "POST only" });
    }

    // Reject unbounded payloads before reading the body (128 MB isolate limit).
    const declared = Number(request.headers.get("content-length") ?? NaN);
    if (!Number.isFinite(declared) || declared <= 0 || declared > MAX_BUNDLE_BYTES) {
      return json(413, { ok: false, error: `content-length required, max ${MAX_BUNDLE_BYTES} bytes` });
    }

    let bundle: {
      schema_version?: unknown;
      participant_id?: unknown;
      shots?: unknown;
      reviews?: unknown;
      terms_version?: unknown;
      terms_accepted_at?: unknown;
      experiments?: unknown;
      machine?: unknown;
    };
    const raw = await request.text();
    if (raw.length > MAX_BUNDLE_BYTES) {
      return json(413, { ok: false, error: "bundle too large" });
    }
    try {
      bundle = JSON.parse(raw);
    } catch {
      return json(400, { ok: false, error: "invalid JSON" });
    }

    if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) {
      return json(400, { ok: false, error: "bundle must be an object" });
    }
    const allowed = new Set(["schema_version", "participant_id", "machine", "shots", "reviews", "experiments", "terms_version", "terms_accepted_at"]);
    if (Object.keys(bundle).some(key => !allowed.has(key))) {
      return json(400, { ok: false, error: "unknown bundle field" });
    }
    if (bundle.schema_version !== 2) {
      return json(400, { ok: false, error: "schema version 2 required; retain legacy bundles separately" });
    }
    if (typeof bundle.participant_id !== "string" || !UUID_RE.test(bundle.participant_id)) {
      return json(400, { ok: false, error: "participant_id must be a UUID" });
    }
    if (!Array.isArray(bundle.shots) || bundle.shots.length === 0) {
      return json(400, { ok: false, error: "no shots in bundle" });
    }
    if (!Array.isArray(bundle.reviews)) {
      return json(400, { ok: false, error: "reviews must be a list" });
    }
    if (!Array.isArray(bundle.experiments) || !["gaggimate", "gaggiuino"].includes(String(bundle.machine))) {
      return json(400, { ok: false, error: "invalid experiments or machine" });
    }
    // Indices identify rows within this snapshot, not across submissions.
    const shots = bundle.shots as Array<{ shot_index?: unknown }>;
    const reviews = bundle.reviews as Array<{ shot_index?: unknown }>;
    const experiments = bundle.experiments as Array<{ experiment_index?: unknown; followup_shot_indices?: unknown }>;
    const validIndex = (value: unknown): value is number => Number.isInteger(value) && Number(value) > 0;
    if (shots.some(s => !s || !validIndex(s.shot_index))) {
      return json(400, { ok: false, error: "invalid shot index" });
    }
    const indices = new Set(shots.map(s => s.shot_index));
    if (indices.size !== shots.length || reviews.some(r => !r || !indices.has(r.shot_index)) ||
        experiments.some(e => !e || !validIndex(e.experiment_index) ||
          !Array.isArray(e.followup_shot_indices) || e.followup_shot_indices.some(i => !indices.has(i))) ||
        new Set(experiments.map(e => e.experiment_index)).size !== experiments.length) {
      return json(400, { ok: false, error: "duplicate index or missing shot reference" });
    }
    // Every stored bundle must carry its own evidence of terms acceptance.
    if (bundle.terms_version !== 2 || (typeof bundle.terms_accepted_at !== "string" ||
        !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(bundle.terms_accepted_at) ||
        !Number.isFinite(Date.parse(bundle.terms_accepted_at)))) {
      return json(400, { ok: false, error: "terms acceptance missing — share via `crema share`" });
    }

    // One object per submission; re-shares just add a newer snapshot for the
    // same install, and curation dedupes offline.
    const key = `bundles/${bundle.participant_id.toLowerCase()}/${Date.now()}-${crypto.randomUUID()}.json`;
    try {
      await env.POOL.put(key, raw, {
        httpMetadata: { contentType: "application/json" },
        customMetadata: {
          schema_version: "2",
          terms_version: String(bundle.terms_version),
          terms_accepted_at: bundle.terms_accepted_at,
          shots: String(bundle.shots.length),
          reviews: String(bundle.reviews.length),
        },
      });
    } catch (err) {
      console.log(JSON.stringify({ event: "pool_put_failed", key, error: String(err) }));
      return json(500, { ok: false, error: "storage error, try again later" });
    }

    console.log(
      JSON.stringify({
        event: "bundle_stored",
        key,
        shots: bundle.shots.length,
        reviews: bundle.reviews.length,
      }),
    );
    return json(200, { ok: true, shots: bundle.shots.length, reviews: bundle.reviews.length });
  },
} satisfies ExportedHandler<Env>;
