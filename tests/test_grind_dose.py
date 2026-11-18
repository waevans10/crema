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
