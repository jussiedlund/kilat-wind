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


class ValidatorTests(Base):
    def check(self, card, stop='end_turn', text=None):
        return publish.validate(text if text is not None else json.dumps(card), stop, self.digest())

    def test_good_card_passes(self):
        card, reasons, warnings = self.check(GOOD)
        self.assertEqual((reasons, warnings), ([], []))
        self.assertEqual(card['lead'], 'S1')

    def test_long_headline_rejected(self):
        _, reasons, _ = self.check(dict(GOOD, headline='x' * 61))
        self.assertTrue(any('headline is 61' in r for r in reasons))

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
