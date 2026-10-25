"""Offline tests for the haze briefing publisher: validators, SQL, pre-checks, error handling. No network, no API."""
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'briefing'))
import publish  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'haze-briefing'
SNAPSHOT = FIXTURES / '2026-10-08T1114'
PROMPT = (ROOT / 'briefing' / 'prompt.md').read_text()
GOOD = json.loads((FIXTURES / 'answer-good.json').read_text())


class Reply:
    def __init__(self, text, stop='end_turn'):
        self.content = [types.SimpleNamespace(type='text', text=text)]
        self.stop_reason = stop
        self.usage = types.SimpleNamespace(input_tokens=10000, output_tokens=200)


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reply


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / SNAPSHOT.name
        shutil.copytree(SNAPSHOT, self.folder)

    def produce(self, **kw):
        return publish.produce(self.folder, PROMPT, 'w' * 12, **kw)

    def digest(self):
        return self.produce(fake=(json.dumps(GOOD), 'end_turn'))['digest_md']


def kilat_outlook(expires='2026-10-09T06:00:00Z'):
    """An experimental outlook issued the evening before the fixture (Thu 8 Oct 11:14 SGT), steps every 3 h."""
    classes = ['heavy'] * 5 + ['moderate'] * 6 + ['light'] * 4
    steps = []
    for i, smoke in enumerate(classes):
        step = {'time': f'2026-10-{7 + (18 + 3 * i) // 24:02d}T{(18 + 3 * i) % 24:02d}:00:00Z', 'leadHours': 3 * i,
                'class': smoke, 'value': 70.0, 'regions': {'South Sumatra': 0.6, 'Riau/Jambi': 0.1, 'other': 0.3}}
        if i == 3:
            step['pm25'] = {'value': 63, 'low': 47, 'high': 81}
        if i == 10:
            step['pm25'] = {'value': 41, 'low': 30, 'high': 57}
        steps.append(step)
    return {'schemaVersion': 1, 'experimental': True, 'issueTime': '2026-10-07T18:00:00Z', 'expiresAt': expires,
            'model': 'gfs', 'today': {'date': '2026-10-08', 'peakSoFar': 40, 'asOf': '2026-10-07T18:00:00Z'},
            'tomorrow': {'date': '2026-10-09', 'peakPm25': {'value': 120, 'low': 85, 'high': 228}, 'band': 'Elevated',
                         'otherModel': {'model': 'ecmwf', 'value': 95}},
            'smokeOverSingapore': steps, 'status': 'ok'}


class NeaOutlookTests(Base):
    def test_looking_ahead_paragraphs_reach_the_digest(self):
        path = self.folder / 'raw' / 'haze_outlook.json'
        outlook = json.loads(path.read_text())
        outlook['report']['paragraphs'] += ['6. For tomorrow, winds are expected to blow from the southwest.',
                                            '7. For immediate outdoor activities, please check the readings.']
        path.write_text(json.dumps(outlook))
        n = publish.section(self.digest(), 'N')
        self.assertIn('its "tomorrow" is', n)
        self.assertIn('"For tomorrow, winds are expected to blow from the southwest."', n)
        self.assertNotIn('please check the readings', n)
        self.assertNotIn('For updates', n)


class KilatForecastTests(Base):
    def add_outlook(self, outlook):
        (self.folder / 'raw' / 'kilat_outlook.json').write_text(json.dumps(outlook))
        manifest = json.loads((self.folder / 'manifest.json').read_text())
        manifest['sources']['kilat_outlook'] = {'status': 'ok'}
        (self.folder / 'manifest.json').write_text(json.dumps(manifest))

    def test_missing_outlook_says_unavailable(self):
        self.assertIn("[K1] Kilat's experimental forecast: unavailable", self.digest())

    def test_peak_steps_and_direction(self):
        self.add_outlook(kilat_outlook())
        k = publish.section(self.digest(), 'K')
        self.assertIn('can be wrong', k)
        self.assertIn('tomorrow (Fri 9 Oct): about 120 µg/m³ (range 85–228), Elevated band; the ECMWF-driven run says 95', k)
        self.assertNotIn('18:00 (Wed)', k, 'steps more than 90 min old are left out')
        self.assertIn('today 11:00 (Thu) to today 14:00 (Thu): smoke over Singapore heavy, mainly from South Sumatra; '
                      'estimated 1-hr PM2.5 11:00 63 (47–81).', k)
        self.assertIn('Estimated PM2.5 falling', k)
        self.assertIn('thinning', k)
        self.assertNotIn('Riau', k, 'sources under 30% are not named')

    def test_expired_outlook_lists_nothing_ahead(self):
        self.add_outlook(kilat_outlook(expires='2026-10-08T00:00:00Z'))
        k = publish.section(self.digest(), 'K')
        self.assertIn('EXPIRED', k)
        self.assertNotIn('[K2]', k)

    def test_hero_days_must_be_in_the_forecast(self):
        self.add_outlook(kilat_outlook())
        digest = self.digest()
        self.assertIsNone(publish.hero_problem('Smoke from Sumatra may ease by Friday', digest))
        self.assertIsNone(publish.hero_problem('Lighter haze likely tomorrow', digest))
        self.assertEqual(publish.hero_problem('Smoke easing by Monday', digest), 'Monday is not in the Kilat forecast')
        self.add_outlook(kilat_outlook(expires='2026-10-08T00:00:00Z'))
        self.assertEqual(publish.hero_problem('Smoke from Sumatra may ease by Friday', self.digest()),
                         'Friday is not in the Kilat forecast')

    def test_uncited_forecast_warns(self):
        self.add_outlook(kilat_outlook())
        digest = self.digest()
        _, reasons, warnings = publish.validate(json.dumps(GOOD), 'end_turn', digest)
        self.assertEqual(reasons, [])
        self.assertIn('experimental forecast available but not cited in summary or ahead', warnings)
        cited = dict(GOOD, evidence={'summary': ['S1'], 'ahead': ['[K4]', 'N1']})
        self.assertEqual(publish.validate(json.dumps(cited), 'end_turn', digest)[2], [])

    def test_quoting_a_forecast_number_only_warns(self):
        self.add_outlook(kilat_outlook())
        _, reasons, warnings = publish.validate(json.dumps(dict(GOOD, ahead='The model hints at up to 228 tomorrow.')),
                                                'end_turn', self.digest())
        self.assertEqual(reasons, [])
        self.assertIn('quotes experimental forecast number 228', warnings)


class ValidatorTests(Base):
    def check(self, card, stop='end_turn', text=None):
        return publish.validate(text if text is not None else json.dumps(card), stop, self.digest())

    def test_good_card_passes(self):
        card, reasons, warnings = self.check(GOOD)
        self.assertEqual((reasons, warnings), ([], []))
        self.assertEqual(card['lead'], 'S1')

    def test_points_accepted_and_joined_into_summary(self):
        pts = ['PM2.5 is in the normal band across Singapore.', ' Haze is easing through the morning. ']
        card, reasons, warnings = self.check(dict(GOOD, points=pts))
        self.assertEqual((reasons, warnings), ([], []))
        self.assertEqual(card['points'], [p.strip() for p in pts])
        self.assertEqual(card['summary'], ' '.join(card['points']))

    def test_single_point_rejected(self):
        _, reasons, _ = self.check(dict(GOOD, points=['Only one point here.']))
        self.assertIn('points must be 2-4 non-empty strings', reasons)

    def test_point_with_invented_number_rejected(self):
        _, reasons, _ = self.check(dict(GOOD, points=['Fine overall.', 'PM2.5 hit 987 this morning.']))
        self.assertTrue(any('point 2 contains number 987' in r for r in reasons))
        self.assertEqual(len(reasons), 1)

    def test_long_point_warns_only(self):
        _, reasons, warnings = self.check(dict(GOOD, points=['Fine overall.', 'word ' * 30]))
        self.assertEqual(reasons, [])
        self.assertTrue(any('point 2 is 149 chars (target 130)' in w for w in warnings))

    def test_very_long_point_rejected(self):
        _, reasons, _ = self.check(dict(GOOD, points=['Fine overall.', 'x' * 201]))
        self.assertTrue(any('point 2 is 201 chars (hard limit 200)' in r for r in reasons))

    def test_long_headline_warns_then_rejects(self):
        _, reasons, warnings = self.check(dict(GOOD, headline='x' * 61))
        self.assertEqual(reasons, [])
        self.assertTrue(any('headline is 61' in w for w in warnings))
        _, reasons, _ = self.check(dict(GOOD, headline='x' * 91))
        self.assertTrue(any('headline is 91' in r for r in reasons))

    def test_invented_number_rejected(self):
        _, reasons, _ = self.check(dict(GOOD, summary='PM2.5 hit 987 this morning.'))
        self.assertTrue(any('987' in r for r in reasons))

    def test_ordinal_time_counts_as_number(self):
        _, reasons, _ = self.check(dict(GOOD, summary='Better since 9.47am.'))
        self.assertTrue(any('number 47 ' in r for r in reasons))

    def test_twelve_hour_times_checked_as_24_hour(self):
        self.assertEqual(publish.numbers('since 3pm'), {'15', '0'})
        self.assertEqual(publish.numbers('at 12am and 12:30pm'), {'0', '12', '30'})
        _, reasons, _ = publish.validate(json.dumps(dict(GOOD, summary='Worse since 3pm.')), 'end_turn', '[S1] x 14:00 [S2]')
        self.assertTrue(any('number 15 ' in r for r in reasons))

    def test_hero_lines_kept_or_dropped_with_warnings(self):
        hero = {'north': 'Expect haze to taper off later today', 'south': 'Smoke drifting in from Sumatra',
                'east': 'Air is clear today', 'west': 'PM2.5 at 70 and rising',
                'central': 'Smoky but easing slowly over here later this afternoon'}
        card, reasons, warnings = self.check(dict(GOOD, hero=hero))
        self.assertEqual(reasons, [])
        self.assertEqual(card['hero'], {'north': 'Expect haze to taper off later today', 'south': 'Smoke drifting in from Sumatra'})
        self.assertEqual(sorted(w.split(':')[0] for w in warnings if 'dropped' in w),
                         ['hero central dropped (54 chars, 9 words)', 'hero east dropped (clear/clean)', 'hero west dropped (digits)'])
        card, reasons, warnings = self.check(dict(GOOD, hero='nope'))
        self.assertEqual((reasons, card['hero']), ([], None))

    def test_advice_variants_warn(self):
        _, reasons, warnings = self.check(dict(GOOD, summary='The app advises avoiding strenuous outdoor activity.'))
        self.assertEqual(reasons, [])
        self.assertTrue(any('avoiding' in w for w in warnings) and any('advises' in w for w in warnings))

    def test_missing_key_rejected(self):
        bad = {k: v for k, v in GOOD.items() if k != 'ahead'}
        _, reasons, _ = self.check(bad)
        self.assertIn('ahead missing or empty', reasons)

    def test_bad_lead_rejected(self):
        for lead in ('S99', 'L1', '', None):
            _, reasons, _ = self.check(dict(GOOD, lead=lead))
            self.assertTrue(any('lead' in r for r in reasons), lead)

    def test_bracketed_lead_accepted(self):
        _, reasons, _ = self.check(dict(GOOD, lead='[S2]'))
        self.assertEqual(reasons, [])

    def test_fenced_json_parses(self):
        card, reasons, _ = self.check(None, text='```json\n' + json.dumps(GOOD) + '\n```')
        self.assertEqual(reasons, [])
        self.assertIsNotNone(card)

    def test_not_json_rejected(self):
        card, reasons, _ = self.check(None, text='Sorry, I cannot.')
        self.assertIsNone(card)
        self.assertTrue(reasons)

    def test_refusal_and_max_tokens_recorded(self):
        for stop in ('refusal', 'max_tokens'):
            _, reasons, _ = self.check(GOOD, stop=stop)
            self.assertIn(f'stop_reason {stop}', reasons)

    def test_advice_and_clear_are_warnings_only(self):
        card, reasons, warnings = self.check(dict(GOOD, ahead='You should stay indoors; the air is clear.'))
        self.assertEqual(reasons, [])
        self.assertTrue(any('should' in w for w in warnings))
        self.assertTrue(any('stay indoors' in w for w in warnings))
        self.assertTrue(any("'clear'" in w for w in warnings))


class SqlTests(unittest.TestCase):
    def row(self, **kw):
        base = {'created_at': '2026-10-08T03:40:00Z', 'status': 'ok', 'model': publish.MODEL, 'prompt_sha': 'a' * 12,
                'weights_sha': 'b' * 12, 'card_json': None, 'raw_text': "it's a \"quote\"\nline two; DROP TABLE x;--",
                'reasons': ["can't"], 'warnings': [], 'digest_md': 'd\n', 'sources_json': {'a': 'ok'},
                'input_tokens': 5, 'output_tokens': 6, 'cost_usd': 0.1, 'seconds': 1.5}
        base.update(kw)
        return base

    def test_escaping_round_trips_through_sqlite(self):
        db = sqlite3.connect(':memory:')
        db.executescript((ROOT / 'migrations' / '0013_haze_briefing.sql').read_text())
        row = self.row()
        db.executescript(publish.to_sql(row))
        got = db.execute('SELECT raw_text, reasons, card_json, status FROM haze_briefing').fetchone()
        self.assertEqual(got, (row['raw_text'], json.dumps(["can't"]), None, 'ok'))

    def test_keeps_newest_rows_only(self):
        db = sqlite3.connect(':memory:')
        db.executescript((ROOT / 'migrations' / '0013_haze_briefing.sql').read_text())
        for _ in range(publish.KEEP_ROWS + 5):
            db.executescript(publish.to_sql(self.row()))
        self.assertEqual(db.execute('SELECT count(*), min(id) FROM haze_briefing').fetchone(), (publish.KEEP_ROWS, 6))

    def test_literal_is_hex_and_round_trips(self):
        db = sqlite3.connect(':memory:')
        tricky = "a'b; DROP TABLE x;\n-- c \"d\" µg/m³ 🙂"
        self.assertTrue(publish.lit(tricky).startswith("CAST(X'"))
        self.assertEqual(db.execute(f'SELECT {publish.lit(tricky)}').fetchone()[0], tricky)
        self.assertEqual(publish.lit(None), 'NULL')


class PipelineTests(Base):
    def test_ok_row_with_fake_client_and_cost(self):
        client = FakeClient(Reply(json.dumps(GOOD)))
        row = self.produce(client=client)
        self.assertEqual(row['status'], 'ok')
        self.assertAlmostEqual(row['cost_usd'], 10000 * 0.10 / 1e6 + 200 * 0.50 / 1e6)
        call = client.calls[0]
        self.assertEqual(call['model'], 'claude-haiku-5-5')
        self.assertNotIn('temperature', call)
        self.assertEqual(call['output_config'], {'effort': 'low'})
        self.assertTrue(call['messages'][0]['content'].endswith('Write the briefing now.'))

    def test_rejected_row_keeps_raw_text(self):
        row = self.produce(client=FakeClient(Reply(json.dumps(dict(GOOD, lead='S99')))))
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('S99', row['raw_text'])

    def test_stale_readings_skip_without_calling_model(self):
        manifest = json.loads((self.folder / 'manifest.json').read_text())
        manifest['collectedAt'] = '2026-10-08T09:14:17+00:00'  # readings now ~6 h old
        (self.folder / 'manifest.json').write_text(json.dumps(manifest))
        client = FakeClient(Reply('{}'))
        row = self.produce(client=client)
        self.assertEqual(row['status'], 'skipped')
        self.assertIn('min old', row['reasons'][0])
        self.assertEqual(client.calls, [])

    def test_missing_outlook_skips(self):
        manifest = json.loads((self.folder / 'manifest.json').read_text())
        manifest['sources']['haze_outlook'] = {'status': 'failed', 'error': 'x'}
        (self.folder / 'manifest.json').write_text(json.dumps(manifest))
        client = FakeClient(Reply('{}'))
        row = self.produce(client=client)
        self.assertEqual(row['status'], 'skipped')
        self.assertIn('outlook', row['reasons'][0])
        self.assertEqual(client.calls, [])

    def test_sdk_error_becomes_error_row(self):
        class APIConnectionError(Exception):
            pass

        fake = types.ModuleType('anthropic')
        fake.APIStatusError, fake.APIConnectionError = type('APIStatusError', (Exception,), {}), APIConnectionError
        sys.modules['anthropic'] = fake
        self.addCleanup(sys.modules.pop, 'anthropic', None)
        row = self.produce(client=FakeClient(error=APIConnectionError('boom')))
        self.assertEqual(row['status'], 'error')
        self.assertIn('boom', row['reasons'][0])

    def test_cli_dry_run_and_sql_output(self):
        out = self.tmp / 'out.sql'
        answer = FIXTURES / 'answer-good.json'
        args = ['--snapshot-dir', str(SNAPSHOT), '--fake-answer', str(answer)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish.main(args + ['--dry-run']), 0)
            self.assertEqual(publish.main(args + ['--output', str(out)]), 0)
        self.assertIn('INSERT INTO haze_briefing', out.read_text())
        self.assertFalse((SNAPSHOT / 'digest.md').exists(), 'fixture must not be modified')


if __name__ == '__main__':
    unittest.main()
