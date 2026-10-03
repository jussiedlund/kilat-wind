"""Synthetic invariants and compatibility with explicitly dated NEA responses."""
import base64
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import struct
import sys
import unittest

SPEC = importlib.util.spec_from_file_location('wind_contract', Path(__file__).parents[1] / 'scripts/wind_contract.py')
w = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = w
SPEC.loader.exec_module(w)
RUN = '2026-10-02T18:00:00Z'
NOW = '2026-10-02T22:00:00Z'


def feed(unit, rows):
    return {'code': 0, 'data': {'readingUnit': unit, 'stations': [
        {'id': 'A', 'name': 'Station A', 'location': {'latitude': 1.3, 'longitude': 103.8}},
        {'id': 'B', 'name': 'Station B', 'location': {'latitude': 1.4, 'longitude': 103.9}}],
        'readings': [{'timestamp': at, 'data': [{'stationId': sid, 'value': value} for sid, value in values]}
                     for at, values in rows]}}


def encoded(values):
    return base64.b64encode(struct.pack('<8181h', *values)).decode('ascii')


def document():
    # Distinct signs and x/y gradients make orientation/endian errors observable.
    return {'schemaVersion': 1, 'source': 'NOAA/NCEP GFS 0.25 degree, 10 m wind',
            'modelRunAt': RUN, 'generatedAt': '2026-10-02T21:00:00Z',
            'expiresAt': '2026-10-03T06:00:00Z',
            'encoding': w.ENCODING, 'grid': {'south': -8, 'north': 12, 'west': 95, 'east': 120,
                                            'stepDegrees': .25, 'width': 101, 'height': 81, 'order': w.ORDER},
            'frames': [{'forecastHour': hour,
                        'validAt': (w.instant(RUN) + dt.timedelta(hours=hour)).isoformat(),
                        'u': encoded([-500 + x + hour * 10 for y in range(81) for x in range(101)]),
                        'v': encoded([200 - y - hour * 10 for y in range(81) for x in range(101)])}
                       for hour in (0, 3, 6, 9, 12)]}


class StationContract(unittest.TestCase):
    def pair(self, speed, direction):
        return w.pair_observations(feed('knots', speed), feed('degrees', direction))

    def test_captured_nea_pages_preserve_actual_coverage_and_source_time(self):
        captured = json.loads((Path(__file__).parent / 'fixtures/wind/nea-2026-10-03.json').read_text())
        pages = {(row['kind'], row['feed']): row['response'] for row in captured['requests']}
        latest = w.pair_observations(pages['latest', 'wind-speed'], pages['latest', 'wind-direction'])
        self.assertEqual(latest.status, 'available')
        self.assertEqual(len(latest.data), 17)
        self.assertEqual({o.speed_source_at for o in latest.data}, {'2026-10-03T13:04:00+08:00'})
        history = w.pair_observations(pages['dated', 'wind-speed'], pages['dated', 'wind-direction'])
        self.assertEqual(len(history.data), 45)
        counts = {at: len([o for o in history.data if o.speed_source_at == at])
                  for at in {o.speed_source_at for o in history.data}}
        self.assertEqual(counts, {'2026-10-03T13:03:00+08:00': 17,
                                 '2026-10-03T13:04:00+08:00': 17,
                                 '2026-10-03T13:05:00+08:00': 11})
        partial = w.sample_observations(history.data, '2026-10-03T13:05:00+08:00', max_age_seconds=0)
        self.assertEqual(len(partial.data), 11)  # A dated page is not a promise of complete station history.
        carried = w.sample_observations(history.data, '2026-10-03T13:05:00+08:00', max_age_seconds=60)
        self.assertEqual(len(carried.data), 17)
        self.assertEqual(len([o for o in carried.data if o.speed_source_at == '2026-10-03T13:04:00+08:00']), 6)

    def test_pairs_never_mix_latest_times_or_stations(self):
        result = self.pair([('2026-10-03T00:01:00+08:00', [('A', 8), ('B', 9)]),
                            ('2026-10-03T00:00:00+08:00', [('A', 4)])],
                           [('2026-10-03T00:00:00+08:00', [('A', 270), ('B', 90)])])
        self.assertEqual(result.status, 'available')
        self.assertEqual([(o.station.id, o.speed_knots, o.direction_degrees) for o in result.data], [('A', 4, 270)])
        self.assertEqual(result.data[0].speed_ms, 4 * 1852 / 3600)
        self.assertEqual(result.data[0].direction_unit, 'degrees')

    def test_calm_zero_present_but_missing_or_invalid_is_absent(self):
        at = '2026-10-03T00:00:00+08:00'
        for missing in (None, 'NA', '', -1, 201, True):
            with self.subTest(missing=missing):
                result = self.pair([(at, [('A', 0), ('B', missing)])], [(at, [('A', 0), ('B', 90)])])
                self.assertEqual([o.station.id for o in result.data], ['A'])
                self.assertEqual(result.data[0].speed_ms, 0)
        for angle in (-1, 361, None, '90', True):
            self.assertEqual(self.pair([(at, [('A', 1)])], [(at, [('A', angle)])]).status, 'missing')

    def test_midnight_sampling_uses_actual_date_and_inclusive_age(self):
        observations = self.pair([('2026-10-02T23:59:00+08:00', [('A', 1)]),
                                  ('2026-10-03T00:01:00+08:00', [('A', 9)])],
                                 [('2026-10-02T23:59:00+08:00', [('A', 90)]),
                                  ('2026-10-03T00:01:00+08:00', [('A', 180)])]).data
        target = '2026-10-03T00:00:00+08:00'
        self.assertEqual(w.sample_observations(observations, target, max_age_seconds=60).data[0].speed_knots, 1)
        self.assertEqual(w.sample_observations(observations, target, max_age_seconds=59).status, 'missing')
        self.assertEqual(w.sample_observations(observations, '2026-10-04T00:00:00+08:00', max_age_seconds=600).status, 'missing')
        self.assertEqual(w.sample_observations(observations, '00:00', max_age_seconds=600).status, 'invalid')
        self.assertEqual(w.sample_observations(observations, target, max_age_seconds=-1).status, 'invalid')

    def test_equivalent_instants_preserve_both_source_strings(self):
        result = self.pair([('2026-10-03T00:00:00+08:00', [('A', 1)])],
                           [('2026-10-02T16:00:00Z', [('A', 360)])])
        self.assertEqual(result.data[0].speed_source_at, '2026-10-03T00:00:00+08:00')
        self.assertEqual(result.data[0].direction_source_at, '2026-10-02T16:00:00Z')
        self.assertEqual(result.data[0].direction_degrees, 360)

    def test_conflicting_duplicates_invalidate_independent_of_order(self):
        at = '2026-10-03T00:00:00+08:00'
        for values in ([1, 2], [2, 1], [1, None], [None, 1], [1, 2, 1]):
            result = self.pair([(at, [('A', value) for value in values])], [(at, [('A', 90)])])
            self.assertEqual(result.status, 'missing')
        self.assertEqual(self.pair([(at, [('A', 1), ('A', 1)])], [(at, [('A', 90)])]).status, 'available')

    def test_coordinates_metadata_and_units_fail_closed(self):
        at = '2026-10-03T00:00:00+08:00'
        speed = feed('knots', [(at, [('A', 1), ('B', 2)])])
        direction = feed('degrees', [(at, [('A', 90), ('B', 180)])])
        speed['data']['stations'][0]['location']['latitude'] = 91
        result = w.pair_observations(speed, direction)
        self.assertEqual([o.station.id for o in result.data], ['B'])
        direction['data']['stations'][1]['location']['longitude'] = 104
        self.assertEqual(w.pair_observations(speed, direction).status, 'missing')
        speed['data']['readingUnit'] = 'km/h'
        self.assertEqual(w.pair_observations(speed, direction).status, 'invalid')

    def test_bounds_and_malformed_payloads(self):
        for payload in ('x' * (w.MAX_BYTES + 1), '{', [], {'data': {}}):
            self.assertEqual(w.pair_observations(payload, {}).status, 'invalid')
        huge = feed('knots', [])
        huge['data']['readings'] = [{}] * (w.MAX_ROWS + 1)
        self.assertEqual(w.pair_observations(huge, {}).status, 'invalid')
        huge = feed('knots', [])
        huge['data']['stations'] = huge['data']['stations'][:1] * (w.MAX_STATIONS + 1)
        self.assertEqual(w.pair_observations(huge, {}).status, 'invalid')


class ModelContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = document()
        cls.model = w.parse_model(cls.raw).data

    def sample(self, target=RUN, latitude=-8, longitude=95, now=NOW):
        return w.sample_model(self.model, target, latitude, longitude, now=now)

    def test_signed_units_and_exact_frame_and_all_grid_corners(self):
        for lat, lon, x, y in ((-8, 95, 0, 0), (-8, 120, 100, 0), (12, 95, 0, 80), (12, 120, 100, 80)):
            sample = self.sample(latitude=lat, longitude=lon).data
            self.assertAlmostEqual(sample.u_ms, (-500 + x) / 10)
            self.assertAlmostEqual(sample.v_ms, (200 - y) / 10)
            self.assertEqual(sample.valid_from, sample.valid_to)
            self.assertEqual(sample.identity, 'GFS model forecast')
        self.assertEqual(self.sample('2026-10-03T06:00:00Z').status, 'available')

    def test_temporal_interpolation_keeps_vector_direction_and_metadata(self):
        sample = self.sample('2026-10-02T19:30:00Z', -7.875, 95.125).data
        self.assertAlmostEqual(sample.u_ms, -48.45)
        self.assertAlmostEqual(sample.v_ms, 18.45)
        self.assertEqual(sample.valid_from, w.instant(RUN))
        self.assertEqual(sample.valid_to, w.instant('2026-10-02T21:00:00Z'))
        self.assertEqual(sample.model_run_at, w.instant(RUN))
        self.assertEqual(sample.generated_at, w.instant('2026-10-02T21:00:00Z'))
        # Opposite vectors cancel; averaging scalar speeds would invent motion.
        raw = copy.deepcopy(self.raw)
        raw['frames'][0]['u'] = encoded([100] * 8181)
        raw['frames'][1]['u'] = encoded([-100] * 8181)
        raw['frames'][0]['v'] = raw['frames'][1]['v'] = encoded([0] * 8181)
        model = w.parse_model(raw).data
        sample = w.sample_model(model, '2026-10-02T19:30:00Z', 1.3, 103.8, now=NOW).data
        self.assertEqual((sample.u_ms, sample.v_ms), (0, 0))

    def test_expiry_uses_actual_now_even_for_historical_target(self):
        self.assertEqual(self.sample(now='2026-10-03T05:59:59Z').status, 'available')
        self.assertEqual(self.sample(now='2026-10-03T06:00:00Z').status, 'expired')
        self.assertEqual(self.sample(now='2026-10-02T20:59:59Z').status, 'unavailable')

    def test_no_extrapolation_and_no_outside_grid_or_naive_times(self):
        for target in ('2026-10-02T17:59:59Z', '2026-10-03T06:00:01Z'):
            self.assertEqual(self.sample(target).status, 'unsupported')
        for lat, lon in ((12.001, 95), (-8.001, 95), (0, 94.99), (0, 120.001)):
            self.assertEqual(self.sample(latitude=lat, longitude=lon).status, 'unsupported')
        self.assertEqual(self.sample('2026-10-02T18:00:00').status, 'invalid')
        self.assertEqual(self.sample(latitude=float('nan')).status, 'invalid')

    def test_invalid_grid_encoding_and_times(self):
        mutations = [lambda r: r['grid'].update(width=100),
                     lambda r: r['grid'].update(width=101.0),
                     lambda r: r['grid'].update(north=12.25),
                     lambda r: r['grid'].update(stepDegrees=.5),
                     lambda r: r['grid'].update(order='north-to-south rows'),
                     lambda r: r.update(schemaVersion=True),
                     lambda r: r.update(encoding='big-endian'),
                     lambda r: r.update(modelRunAt='2026-10-02T19:00:00Z'),
                     lambda r: r.update(generatedAt='2026-10-02T17:00:00Z'),
                     lambda r: r.update(expiresAt='2026-10-03T09:00:00Z'),
                     lambda r: r['frames'][0].update(u='!' * 21816),
                     lambda r: r['frames'][0].update(u=encoded([1001] * 8181)),
                     lambda r: r['frames'][0].update(u=encoded([0] * 8181)[:-4]),
                     lambda r: r['frames'][0].update(u=base64.b64encode(bytes(16361)).decode('ascii')),
                     lambda r: r['frames'][1].update(validAt='2026-10-02T22:00:00Z'),
                     lambda r: r['frames'].reverse(),
                     lambda r: r['frames'].pop(),
                     lambda r: r['frames'].append(r['frames'][0])]
        for mutation in mutations:
            raw = copy.deepcopy(self.raw)
            mutation(raw)
            with self.subTest(mutation=mutation):
                self.assertEqual(w.parse_model(raw).status, 'invalid')
        self.assertEqual(w.parse_model(' ' * (w.MAX_BYTES + 1)).status, 'invalid')


if __name__ == '__main__':
    unittest.main()
