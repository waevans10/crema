"""Offline regressions for the 2026-11-17 audit; device and paid calls are mocked."""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crema import db
from crema.config import CremaConfig
from crema.dataset import import_bundle, validate_bundle
from crema.export import build_export_bundle, stamp_acceptance
from crema.push import push_edit


@pytest.fixture
async def conn(tmp_path):
    connection = await db.connect(tmp_path / 'test.db')
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize('closed', [False, True])
async def test_retention_preserves_experiment_and_prunes_source(conn, closed):
    await db.upsert_shot(conn, '1', {})
    rid = await db.insert_review(conn, '1', 'mock', {'score': 7})
    eid = await db.start_experiment(conn, rid, None, 'finer', 7, 3)
    if closed:
        await db.close_experiment(conn, eid)
    await conn.execute("UPDATE shots SET created_at=unixepoch('now')-40*86400")
    await conn.commit()
    assert await db.prune_old(conn, 30) == 1
    row = await (await conn.execute('SELECT * FROM experiments WHERE id=?', (eid,))).fetchone()
    assert row['source_review_id'] is None and row['baseline_cup'] == 3
    assert row['status'] == ('closed' if closed else 'active')
    assert not await db.get_review(conn, rid)


async def test_sharing_envelope_and_snapshot_replacement(conn):
    await db.upsert_shot(conn, '1', {'duration_seconds': 10}, captured_at=100)
    await db.upsert_shot(conn, '2', {'duration_seconds': 20}, captured_at=200)
    bundle = stamp_acceptance(await build_export_bundle(conn, CremaConfig()))
    await import_bundle(conn, bundle)
    await conn.execute("DELETE FROM shots WHERE id='1'")
    await conn.commit()
    smaller = stamp_acceptance(await build_export_bundle(conn, CremaConfig()))
    await import_bundle(conn, smaller)
    rows = await (await conn.execute('SELECT * FROM community_shots')).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]['telemetry_json'])['duration_seconds'] == 20
    # Invalid consent and legacy data fail explicitly, without modifying the pool.
    for bad in [dict(bundle, terms_version=1), dict(bundle, terms_accepted_at='bad'),
                dict(bundle, schema_version=1)]:
        with pytest.raises(ValueError):
            await import_bundle(conn, bad)
    assert len(await (await conn.execute('SELECT * FROM community_shots')).fetchall()) == 1


async def test_import_rolls_back_replacement_on_write_failure(conn):
    await db.upsert_shot(conn, '1', {'duration_seconds': 10})
    bundle = await build_export_bundle(conn, CremaConfig())
    await import_bundle(conn, bundle)
    await conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON community_shots BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    await conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await import_bundle(conn, bundle)
    rows = await (await conn.execute('SELECT * FROM community_shots')).fetchall()
    assert len(rows) == 1
    assert not conn.in_transaction


async def test_web_reviews_already_ingested_shot_and_retries_failure(conn, tmp_path, monkeypatch):
    import crema.web.app as web
    await db.upsert_shot(conn, '1', {}, captured_at=100)
    await db.insert_review(conn, '1', 'mock', {'score': 7})
    await db.upsert_shot(conn, '2', {}, captured_at=200)
    monkeypatch.setattr(web, '_cfg', CremaConfig(db_path=tmp_path/'test.db'))
    monkeypatch.setattr(web, 'ingest_new_shots', AsyncMock(return_value=[]))
    async def review(c, cfg):
        await db.insert_review(c, '2', 'mock', {'score': 8})
    mock = AsyncMock(side_effect=[RuntimeError('offline failure')])
    monkeypatch.setattr(web, 'review_recent', mock)
    await web.run_review()
    mock.side_effect = review
    await web.run_review()
    await web.run_review()
    assert mock.await_count == 2  # failure, successful retry, then skip


async def test_push_checks_rating_and_ack_before_device_call(conn):
    await db.upsert_shot(conn, '1', {})
    rid = await db.insert_review(conn, '1', 'mock', {})
    eid = await db.insert_pending_edit(conn, 'test', 'test',
        {'label': 'test', 'temperature': 93, 'phases': []}, review_id=rid,
        stop_changes=['removed stop'])
    with patch('crema.push.GaggimateWebSocketClient') as client:
        client.return_value.create_or_update_profile = AsyncMock(return_value={'id': 'new'})
        with pytest.raises(RuntimeError, match='Rate'):
            await push_edit(conn, CremaConfig(), eid)
        await db.set_shot_cup_rating(conn, '1', 4)
        with pytest.raises(RuntimeError, match='acknowledge'):
            await push_edit(conn, CremaConfig(), eid)
        client.return_value.create_or_update_profile.assert_not_awaited()
        assert (await push_edit(conn, CremaConfig(), eid, acknowledge_stops=True))['status'] == 'pushed'


async def test_drafting_requires_cup_rating_before_paid_call(conn):
    from crema.draft import draft_from_review
    await db.upsert_shot(conn, '1', {'profile_id': 'p'})
    rid = await db.insert_review(conn, '1', 'mock', {})
    with patch('crema.draft.AsyncAnthropic') as client:
        with pytest.raises(RuntimeError, match='Rate'):
            await draft_from_review(conn, CremaConfig(), rid)
        client.assert_not_called()


async def test_only_one_push_claims_draft(conn, tmp_path):
    eid = await db.insert_pending_edit(conn, 'test', 'test',
        {'label': 'test', 'temperature': 93, 'phases': []})
    started, finish = asyncio.Event(), asyncio.Event()
    async def save(**kwargs):
        started.set()
        await finish.wait()
        return {'id': 'new'}
    second = await db.connect(tmp_path/'test.db')
    try:
        with patch('crema.push.GaggimateWebSocketClient') as client:
            client.return_value.create_or_update_profile = AsyncMock(side_effect=save)
            task = asyncio.create_task(push_edit(conn, CremaConfig(), eid))
            try:
                await asyncio.wait_for(started.wait(), 2)
                with pytest.raises(RuntimeError):
                    await asyncio.wait_for(push_edit(second, CremaConfig(), eid), .5)
            finally:
                finish.set()
                await task
            assert client.return_value.create_or_update_profile.await_count == 1
    finally:
        await second.close()


async def test_ambiguous_push_cannot_be_retried_but_can_be_discarded(conn):
    from crema.push import discard_edit
    eid = await db.insert_pending_edit(conn, 'test', 'test',
        {'label': 'test', 'temperature': 93, 'phases': []})
    with patch('crema.push.GaggimateWebSocketClient') as client:
        client.return_value.create_or_update_profile = AsyncMock(side_effect=TimeoutError('no reply'))
        with pytest.raises(RuntimeError, match='Check the machine'):
            await push_edit(conn, CremaConfig(), eid)
        with pytest.raises(RuntimeError):
            await push_edit(conn, CremaConfig(), eid)
        assert client.return_value.create_or_update_profile.await_count == 1
    assert (await db.get_pending_edit(conn, eid))['status'] == 'unknown'
    await discard_edit(conn, eid)
    assert (await db.get_pending_edit(conn, eid))['status'] == 'discarded'
    with pytest.raises(RuntimeError):
        await discard_edit(conn, eid)


async def test_unrated_baseline_does_not_close_existing_experiment(conn):
    old = await db.start_experiment(conn, None, None, 'old', 7, 3)
    with pytest.raises(ValueError, match='Rate'):
        await db.start_experiment(conn, None, None, 'new', 7, None)
    assert (await db.active_experiment(conn))['id'] == old


async def test_gaggiuino_ingest_tracks_experiment_and_exports_metrics(conn, monkeypatch):
    from crema import gaggiuino
    cfg = CremaConfig(machine='gaggiuino')
    eid = await db.start_experiment(conn, None, None, 'finer', 7, 3)
    raw = {'id': 1, 'timestamp': 100, 'profile': {'name': 'PRIVATE', 'id': 1},
           'datapoints': {'timeInShot': [0, 100], 'pressure': [0, 90],
                          'temperature': [900, 920], 'shotWeight': [0, 360],
                          'pumpFlow': [0, 20]}}
    monkeypatch.setattr(gaggiuino, '_get_json', AsyncMock(side_effect=[1, raw]))
    assert await gaggiuino.ingest_new_shots_gaggiuino(conn, cfg) == ['000001']
    experiment = await db.active_experiment(conn)
    assert experiment['id'] == eid and experiment['shots'][0]['id'] == '000001'
    bundle = await build_export_bundle(conn, cfg)
    assert bundle['machine'] == 'gaggiuino'
    telemetry = bundle['shots'][0]['telemetry']
    assert telemetry['curves']['pressure_bar'] == [0, 9]
    assert telemetry['avg_temperature_c'] == 91
    assert 'PRIVATE' not in json.dumps(bundle)


def test_cli_push_requires_explicit_stop_ack_and_review_retries(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import crema.cli as cli
    cfg = CremaConfig(db_path=tmp_path/'cli.db', autoreview=True)
    async def setup():
        c = await db.connect(cfg.db_path)
        try:
            await db.upsert_shot(c, '1', {}, captured_at=100)
            rid = await db.insert_review(c, '1', 'mock', {})
            await db.set_shot_cup_rating(c, '1', 4)
            eid = await db.insert_pending_edit(c, 'test', 'test',
                {'label': 'test', 'temperature': 93, 'phases': []},
                review_id=rid, stop_changes=['removed stop'])
            await db.upsert_shot(c, '2', {}, captured_at=200)
            return eid
        finally:
            await c.close()
    eid = asyncio.run(setup())
    monkeypatch.setattr(cli, '_config', lambda: cfg)
    runner = CliRunner()
    with patch('crema.push.GaggimateWebSocketClient') as device:
        device.return_value.create_or_update_profile = AsyncMock(return_value={'id': 'new'})
        assert runner.invoke(cli.app, ['push', str(eid)]).exit_code != 0
        device.return_value.create_or_update_profile.assert_not_awaited()
        result = runner.invoke(cli.app, ['push', str(eid), '--acknowledge-stops'])
        assert result.exit_code == 0, result.output
        device.return_value.create_or_update_profile.assert_awaited_once()
    monkeypatch.setattr(cli, 'ingest_new_shots', AsyncMock(return_value=[]))
    async def review(c, config):
        await db.insert_review(c, '2', 'mock', {'score': 8})
        return {'suggestions': {'score': 8}}
    reviews = AsyncMock(side_effect=review)
    monkeypatch.setattr(cli, 'review_recent', reviews)
    monkeypatch.setattr(cli, 'maybe_autoshare', AsyncMock(return_value=None))
    assert runner.invoke(cli.app, ['review']).exit_code == 0
    assert runner.invoke(cli.app, ['review']).exit_code == 0
    assert reviews.await_count == 1


async def test_refinement_cannot_discard_a_claimed_push(conn):
    from crema.draft import draft_from_review
    from crema.prompts import DraftedProfile
    from types import SimpleNamespace
    await db.upsert_shot(conn, '1', {'profile_id': 'base'})
    await db.set_shot_cup_rating(conn, '1', 4)
    rid = await db.insert_review(conn, '1', 'mock', {})
    profile = {'label': 'test', 'temperature': 93, 'phases': [
        {'name': 'brew', 'phase': 'brew', 'duration': 25,
         'pump': {'target': 'pressure', 'pressure': 9, 'flow': 0}}]}
    await db.upsert_profile(conn, 'base', 'test', profile)
    eid = await db.insert_pending_edit(conn, 'test', 'test', profile, review_id=rid, base_profile_id='base')
    async def parse(**kwargs):
        await db.set_edit_status(conn, eid, 'pushing')
        return SimpleNamespace(parsed_output=DraftedProfile(**profile, change_summary='test'))
    with patch('crema.draft.AsyncAnthropic') as client:
        client.return_value.messages.parse = AsyncMock(side_effect=parse)
        with pytest.raises(RuntimeError, match='changed while refining'):
            await draft_from_review(conn, CremaConfig(), rid, refine_edit_id=eid)
    assert (await db.get_pending_edit(conn, eid))['status'] == 'pushing'
    assert len(await db.list_pending_edits(conn)) == 1


async def test_failed_review_backs_off_instead_of_retrying_every_tick(conn):
    from crema import review
    await db.upsert_shot(conn, '1', {'duration_seconds': 25}, captured_at=100)
    assert await db.newest_shot_needs_review(conn)
    with patch('crema.review.AsyncAnthropic') as client:
        client.return_value.messages.parse = AsyncMock(side_effect=RuntimeError('boom'))
        with pytest.raises(RuntimeError):
            await review.review_recent(conn, CremaConfig())
    assert not await db.newest_shot_needs_review(conn)
    await db.set_setting(conn, 'review_failed', f"1:{int(__import__('time').time()) - db.REVIEW_RETRY_SECONDS - 1}")
    assert await db.newest_shot_needs_review(conn)
    await db.upsert_shot(conn, '2', {'duration_seconds': 25}, captured_at=200)
    await db.set_setting(conn, 'review_failed', f"1:{int(__import__('time').time())}")
    assert await db.newest_shot_needs_review(conn)  # backoff is per shot


def test_participant_id_is_lowercased_on_import():
    bundle = {'schema_version': 2, 'participant_id': 'ABCDEF01-2345-6789-ABCD-EF0123456789',
              'machine': 'gaggimate', 'shots': [], 'reviews': [], 'experiments': []}
    assert validate_bundle(bundle).participant_id == 'abcdef01-2345-6789-abcd-ef0123456789'


async def test_failure_after_claude_call_also_backs_off(conn):
    from crema import review
    await db.upsert_shot(conn, '1', {'duration_seconds': 25}, captured_at=100)
    response = MagicMock()
    response.parsed_output.model_dump.return_value = {'score': 7}
    parsed = AsyncMock(return_value=response)
    with patch('crema.review.AsyncAnthropic') as client, \
            patch('crema.review.execution_score', side_effect=KeyError('telemetry')):
        client.return_value.messages.parse = parsed
        with pytest.raises(KeyError):
            await review.review_recent(conn, CremaConfig())
    assert not await db.newest_shot_needs_review(conn)
