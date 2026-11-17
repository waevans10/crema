import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {test} from 'node:test';
import worker from '../src/index.ts';

// Use the real Python exporter, not a second hand-maintained bundle fixture.
const python = process.env.PYTHON || 'python3';
const bundle = JSON.parse(execFileSync(python, ['-c', `
import asyncio, json, tempfile
from pathlib import Path
from crema import db
from crema.config import CremaConfig
from crema.export import build_export_bundle, stamp_acceptance
async def main():
    with tempfile.TemporaryDirectory() as directory:
        conn = await db.connect(Path(directory)/'source.db')
        try:
            await db.upsert_shot(conn, '1', {'duration_seconds': 25}, captured_at=100)
            await db.insert_review(conn, '1', 'mock', {'score': 8})
            print(json.dumps(stamp_acceptance(await build_export_bundle(conn, CremaConfig()))))
        finally:
            await conn.close()
asyncio.run(main())
`], {encoding: 'utf8'}));

async function collect(body) {
  const raw = JSON.stringify(body);
  const stored = [];
  const response = await worker.fetch(new Request('https://test.invalid/v1/bundles', {
    method: 'POST', headers: {'content-length': String(Buffer.byteLength(raw))}, body: raw,
  }), {POOL: {put: async (...args) => stored.push(args)}});
  return {response, stored};
}

test('real export passes collection and imports the exact stored consent envelope', async () => {
  const {response, stored} = await collect(bundle);
  assert.equal(response.status, 200);
  assert.equal(stored.length, 1);
  assert.ok(stored[0][0].startsWith(`bundles/${bundle.participant_id}/`));
  assert.equal(stored[0][2].customMetadata.schema_version, '2');
  execFileSync(python, ['-c', `
import asyncio, json, sys, tempfile
from pathlib import Path
from crema import db
from crema.dataset import import_bundle
async def main():
    with tempfile.TemporaryDirectory() as directory:
        conn = await db.connect(Path(directory)/'pool.db')
        try:
            result = await import_bundle(conn, json.load(sys.stdin))
            assert len(result.shots) == 1 and result.terms_version == 2
        finally:
            await conn.close()
asyncio.run(main())
`], {input: stored[0][1], encoding: 'utf8'});
});

test('collector rejects legacy, malformed, and dangling-reference bundles before storage', async () => {
  for (const body of [null, [], {...bundle, schema_version: 1}, {...bundle, terms_version: 1},
    {...bundle, terms_accepted_at: 'yesterday'}, {...bundle, participant_id: 'bad'},
    {...bundle, install_id: bundle.participant_id}, {...bundle, shots: [null]},
    {...bundle, shots: [...bundle.shots, ...bundle.shots]},
    {...bundle, reviews: [{shot_index: 99}]}, {...bundle, machine: 'unknown'},
    {...bundle, experiments: [null]}]) {
    const {response, stored} = await collect(body);
    assert.equal(response.status, 400);
    assert.equal(stored.length, 0);
  }
});

test('storage failure reports a retryable failure to the caller', async () => {
  const raw = JSON.stringify(bundle);
  const response = await worker.fetch(new Request('https://test.invalid/v1/bundles', {
    method: 'POST', headers: {'content-length': String(Buffer.byteLength(raw))}, body: raw,
  }), {POOL: {put: async () => {throw Error('offline injected failure')}}});
  assert.equal(response.status, 500);
});
