"""Grind & dose per shot, prefill, and the pull-this card."""
import pytest

from crema import db
from crema.config import CremaConfig
from crema.prompts import build_user_message


@pytest.fixture
async def conn(tmp_path):
    connection = await db.connect(tmp_path / 'test.db')
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def web(tmp_path, monkeypatch):
    import crema.web.app as web
    monkeypatch.setattr(web, '_cfg', CremaConfig(db_path=tmp_path / 'test.db'))
    return web


async def test_grind_dose_round_trip(conn):
    await db.upsert_shot(conn, '1', {})
    assert await db.set_shot_grind_dose(conn, '1', '12', 18.0)
    shot = await db.get_shot(conn, '1')
    assert shot['grind_setting'] == '12' and shot['dose_g'] == 18.0
    recent = (await db.recent_shots(conn, limit=1))[0]
    assert recent['grind_setting'] == '12' and recent['dose_g'] == 18.0
    assert not await db.set_shot_grind_dose(conn, '999', '1', 18)


def test_parse_dose(web):
    assert web.parse_dose('') is None
    assert web.parse_dose(' ') is None
    assert web.parse_dose('18,5') == 18.5
    assert web.parse_dose('5') == 5
    assert web.parse_dose('30') == 30
    for bad in ('abc', '0', '31', '4.9', 'nan', 'inf'):
        with pytest.raises(ValueError):
            web.parse_dose(bad)


async def test_route_rejects_bad_dose_and_saves_nothing(conn, web):
    await db.upsert_shot(conn, '1', {})
    response = await web.save_tasting_notes('1', notes='x', coffee='', cup_rating='4', grind='12', dose='31')
    assert 'error=' in response.headers['location']
    shot = await db.get_shot(conn, '1')
    assert shot['cup_rating'] is None and shot['tasting_notes'] is None and shot['grind_setting'] is None


async def test_route_saves_grind_dose(conn, web):
    await db.upsert_shot(conn, '1', {})
    await web.save_tasting_notes('1', notes='', coffee='', cup_rating='4', grind='12', dose='18')
    shot = await db.get_shot(conn, '1')
    assert shot['grind_setting'] == '12' and shot['dose_g'] == 18.0 and shot['cup_rating'] == 4
    await web.save_tasting_notes('1', notes='', coffee='', cup_rating='4', grind='x' * 30, dose='')
    shot = await db.get_shot(conn, '1')
    assert shot['grind_setting'] == 'x' * 20 and shot['dose_g'] is None


def test_prompt_includes_grind_dose():
    shot = {'id': '1', 'transformed': {}, 'grind_setting': '12', 'dose_g': 18.0}
    message = build_user_message([shot])
    assert 'GRIND: 12' in message and 'DOSE: 18.0 g' in message
    bare = build_user_message([{'id': '1', 'transformed': {}}])
    assert 'GRIND' not in bare and 'DOSE' not in bare


async def _bean(conn, dose=None):
    bean_id = await db.insert_bean(conn, 'Guji', 'light')
    if dose is not None:
        await db.set_bean_recipe(conn, bean_id, dose, None, None, None)
    return bean_id


async def _shot(conn, sid, t, bean_id, grind=None, dose=None, weight=None, rating=None):
    await db.upsert_shot(conn, sid, {'final_weight_g': weight} if weight else {}, captured_at=t, bean_id=bean_id)
    await db.set_shot_grind_dose(conn, sid, grind, dose)
    if rating:
        await db.set_shot_cup_rating(conn, sid, rating)
    return await db.get_shot(conn, sid)


async def test_prefill_uses_earlier_shot_on_same_bean(conn):
    bean = await _bean(conn, dose=18)
    await _shot(conn, '1', 100, bean, '12', 18.5)
    shot2 = await _shot(conn, '2', 200, bean)
    assert await db.grind_dose_prefill(conn, shot2) == ('12', 18.5)


async def test_prefill_never_uses_later_shot(conn):
    bean = await _bean(conn)
    shot1 = await _shot(conn, '1', 100, bean, '12', 18.5)
    shot2 = await _shot(conn, '2', 200, bean)
    await _shot(conn, '3', 300, bean, '9', 17.0)
    assert await db.grind_dose_prefill(conn, shot2) == ('12', 18.5)
    assert await db.grind_dose_prefill(conn, shot1) == ('12', 18.5)


async def test_prefill_ignores_other_beans(conn):
    bean, other = await _bean(conn), await _bean(conn)
    await _shot(conn, '1', 100, other, '30', 20.0)
    shot2 = await _shot(conn, '2', 200, bean)
    assert await db.grind_dose_prefill(conn, shot2) == (None, None)


async def test_prefill_falls_back_to_bean_dose(conn):
    bean = await _bean(conn, dose=17)
    shot = await _shot(conn, '1', 100, bean)
    assert await db.grind_dose_prefill(conn, shot) == (None, 17.0)


async def test_prefill_without_bean(conn):
    await _bean(conn, dose=17)
    shot = await _shot(conn, '1', 100, None)
    assert await db.grind_dose_prefill(conn, shot) == (None, None)


async def test_forms_render_prefilled_inputs(conn, web):
    bean_id = await _bean(conn)
    shot = await _shot(conn, '1', 100, bean_id)
    bean = await db.get_bean(conn, bean_id)
    prefill = {'1': ('12"><b>', 18.5)}
    baseline = web._workflow_prompt(shot, bean, None, prefill)
    assert "name='grind'" in baseline and "value='12&quot;&gt;&lt;b&gt;'" in baseline
    assert "name='dose'" in baseline and "value='18.5'" in baseline
    notebook = web._render_shots([shot], {}, prefill)
    assert "name='grind'" in notebook and "value='18.5'" in notebook


async def test_best_shot_picks_highest_then_newest(conn):
    bean = await _bean(conn)
    await _shot(conn, '1', 100, bean, rating=4)
    await _shot(conn, '2', 200, bean, rating=5)
    await _shot(conn, '3', 300, bean, rating=5)
    await _shot(conn, '4', 400, bean, rating=3)
    assert (await db.best_shot_for_bean(conn, bean))['id'] == '3'
    weak = await _bean(conn)
    await _shot(conn, '5', 500, weak, rating=3)
    assert await db.best_shot_for_bean(conn, weak) is None


async def test_pull_card_renders_parts_and_escapes(conn, web):
    bean_id = await _bean(conn)
    bean = await db.get_bean(conn, bean_id)
    shot = await _shot(conn, '1', 100, bean_id, '<b>12', 18.0, 36.0, 5)
    card = web._pull_card(bean, shot)
    assert '&lt;b&gt;12' in card and '<b>12' not in card
    assert '18 g in' in card and '36 g out' in card and '★★★★★' in card
    assert 'recipe-from-shot' in card
    bare = web._pull_card(bean, await _shot(conn, '2', 200, bean_id, rating=4))
    assert 'Grind' not in bare and ' g in' not in bare and ' g out' not in bare and '★★★★' in bare
    assert web._pull_card(bean, None) == ''


async def test_pull_card_hides_save_when_recipe_matches(conn, web):
    bean_id = await _bean(conn)
    await db.set_bean_recipe(conn, bean_id, 18.0, 36.0, None, None)
    shot = await _shot(conn, '1', 100, bean_id, '12', 18.0, 36.0, 5)
    assert 'recipe-from-shot' not in web._pull_card(await db.get_bean(conn, bean_id), shot)


async def test_recipe_from_shot_keeps_profile_and_style(conn, web):
    bean_id = await _bean(conn)
    await db.set_bean_recipe(conn, bean_id, 16.0, 40.0, 'p1', 'sweet')
    await _shot(conn, '1', 100, bean_id, '12', 18.0, 36.0, 5)
    response = await web.save_recipe_from_shot(bean_id, shot_id='1')
    assert 'note=' in response.headers['location']
    bean = await db.get_bean(conn, bean_id)
    assert (bean['target_dose_g'], bean['target_yield_g']) == (18.0, 36.0)
    assert (bean['target_profile_id'], bean['cup_style']) == ('p1', 'sweet')
    other = await _bean(conn)
    await _shot(conn, '2', 200, other, '9', 20.0, 40.0, 5)
    response = await web.save_recipe_from_shot(bean_id, shot_id='2')
    assert 'error=' in response.headers['location']
    assert (await db.get_bean(conn, bean_id))['target_dose_g'] == 18.0


async def test_recipe_from_shot_without_dose_keeps_bean_dose(conn, web):
    bean_id = await _bean(conn)
    await db.set_bean_recipe(conn, bean_id, 18.0, 40.0, None, None)
    shot = await _shot(conn, '1', 100, bean_id, '12', None, 36.0, 5)
    assert 'recipe-from-shot' in web._pull_card(await db.get_bean(conn, bean_id), shot)
    await web.save_recipe_from_shot(bean_id, shot_id='1')
    bean = await db.get_bean(conn, bean_id)
    assert (bean['target_dose_g'], bean['target_yield_g']) == (18.0, 36.0)
    assert 'recipe-from-shot' not in web._pull_card(bean, shot)


async def test_notebook_summary_shows_recorded_grind_and_dose(conn, web):
    bean_id = await _bean(conn)
    shot = await _shot(conn, '1', 100, bean_id, '12', 18.0, 36.0, 5)
    assert 'grind 12 · 18 g in' in web._render_shots([shot], {})
    unrecorded = await _shot(conn, '2', 200, bean_id)
    assert 'grind' not in web._render_shots([unrecorded], {}).split('<details class="sub"><summary>')[1].split('</summary>')[0]


async def test_manual_web_review_ignores_failure_backoff(conn, web, monkeypatch):
    from unittest.mock import AsyncMock
    await db.upsert_shot(conn, '1', {}, captured_at=100)
    await db.set_setting(conn, 'review_failed', f"1:{int(__import__('time').time())}")
    assert not await db.newest_shot_needs_review(conn)  # the timer still backs off
    monkeypatch.setattr(web, 'ingest_new_shots', AsyncMock(return_value=[]))
    review = AsyncMock(return_value=None)
    monkeypatch.setattr(web, 'review_recent', review)
    await web.run_review()
    assert review.await_count == 1


def test_parse_dose_accepts_gram_suffix(web):
    assert web.parse_dose('18g') == 18
    assert web.parse_dose('18.5 G') == 18.5


async def test_prefill_ignores_out_of_range_bean_dose(conn):
    bean = await _bean(conn, dose=35)
    shot = await _shot(conn, '1', 100, bean)
    assert await db.grind_dose_prefill(conn, shot) == (None, None)


def test_parse_dose_rejects_bare_unit(web):
    with pytest.raises(ValueError):
        web.parse_dose('g')
