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
