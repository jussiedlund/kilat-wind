#!/usr/bin/env python3
"""Hourly haze briefing publisher (shadow mode): snapshot -> digest -> Claude -> checks -> one SQL INSERT.

  python backend/briefing/publish.py --output OUT.sql [--snapshot-dir DIR] [--dry-run] [--fake-answer FILE]

Every outcome ('ok', 'rejected', 'skipped', 'error') becomes a row in D1 table haze_briefing so a week of
results can be reviewed; only a crash of this script is a failure. Pre-checks run before any paid call.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import digest as digest_mod  # noqa: E402

MODEL = 'claude-haiku-5-5'
BASE_URL = 'https://api.anthropic.com'
USD_PER_M_IN, USD_PER_M_OUT = 0.10, 0.50
MAX_READING_AGE = timedelta(minutes=120)
KEEP_ROWS = 2000
LIMITS = {'headline': 60, 'summary': 400, 'ahead': 220}  # over these: a warning, worth reviewing
HARD_LIMITS = {'headline': 90, 'summary': 650, 'ahead': 320}  # over these: something went wrong, reject
ADVICE_RE = re.compile(r'\b(should|wear|stay (?:indoors|inside|home)|avoid|masks?|exercise|n95|close (?:your|the) windows)\b', re.I)
CLEAR_RE = re.compile(r'\b(clear|clearer|clean|cleaner)\b', re.I)
REGIONS = ('north', 'south', 'east', 'west', 'central')
HERO_MAX = 24
NUMBER_RE = re.compile(r'[0-9]+(?:\.[0-9]+)?')


def sha12(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()[:12]


def to_24h(match):
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'pm' else 0)
    return f'{hour}:{match.group(2) or "00"}'


def numbers(text):
    """Numbers as normalised strings: thousands commas and leading zeros ignored ('08' == '8').
    12-hour times become 24-hour hours first, so '3pm' must match a 15 in the digest."""
    text = re.sub(r'\b([0-9]{1,2})(?:[:.]([0-9]{2}))?\s*(am|pm)\b', to_24h, text, flags=re.I)
    text = re.sub(r'(?<=[0-9]),(?=[0-9]{3}\b)', '', text)
    found = set()
    for n in NUMBER_RE.findall(text):
        whole, _, frac = n.partition('.')
        found.add((whole.lstrip('0') or '0') + ('.' + frac if frac else ''))
    return found


def precheck(manifest, raw):
    """Reasons not to spend money on a model call; empty list means go."""
    reasons = []
    if manifest['sources'].get('haze_outlook', {}).get('status') != 'ok':
        reasons.append('NEA haze outlook source did not load')
    stamps = [pts[-1][0] for r in digest_mod.REGIONS if (pts := digest_mod.series(raw, 'pm25', r))]
    collected = digest_mod.t(manifest['collectedAt'])
    if not stamps:
        reasons.append('no Singapore 1-hr PM2.5 readings')
    elif collected - max(stamps) > MAX_READING_AGE:
        minutes = round((collected - max(stamps)).total_seconds() / 60)
        reasons.append(f'latest Singapore 1-hr PM2.5 is {minutes} min old (limit {MAX_READING_AGE.seconds // 60})')
    return reasons


def parse_card(text):
    body = text.strip()
    fence = re.match(r'^```(?:json)?\s*\n(.*?)\n?```\s*$', body, re.S | re.I)
    if fence:
        body = fence.group(1)
    try:
        card = json.loads(body)
    except ValueError:
        return None
    return card if isinstance(card, dict) else None


def validate(text, stop_reason, digest_text):
    """Returns (card or None, reasons, warnings). Any reason means the row is 'rejected'."""
    reasons, warnings = [], []
    if stop_reason != 'end_turn':
        reasons.append(f'stop_reason {stop_reason}')
    card = parse_card(text)
    if card is None:
        reasons.append('answer is not a JSON object')
        return None, reasons, warnings
    known = numbers(digest_text)
    prose = []
    for key, limit in LIMITS.items():
        value = card.get(key)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f'{key} missing or empty')
            continue
        prose.append(value)
        if len(value) > HARD_LIMITS[key]:
            reasons.append(f'{key} is {len(value)} chars (hard limit {HARD_LIMITS[key]})')
        elif len(value) > limit:
            warnings.append(f'{key} is {len(value)} chars (target {limit})')
        for n in sorted(numbers(value) - known):
            reasons.append(f'{key} contains number {n} not in the digest')
    # Hero lines are optional extras: a bad one is dropped with a warning and never rejects the card.
    hero = card.get('hero')
    if hero is not None:
        kept = {}
        if not isinstance(hero, dict):
            warnings.append('hero is not an object; dropped')
        else:
            for region in REGIONS:
                line = hero.get(region)
                if not isinstance(line, str) or not line.strip():
                    warnings.append(f'hero {region} missing')
                elif len(line) > HERO_MAX or not 1 <= len(line.split()) <= 4 or re.search(r'[0-9]', line) or CLEAR_RE.search(line):
                    warnings.append(f'hero {region} dropped: {line!r}')
                else:
                    kept[region] = line.strip().rstrip('.')
        card['hero'] = kept or None
    lead = card.get('lead')
    lead_id = lead.strip().strip('[]') if isinstance(lead, str) else ''
    if not re.fullmatch(r'S[0-9]+', lead_id) or f'[{lead_id}]' not in digest_text:
        reasons.append(f'lead {lead!r} is not an S-id in the digest')
    joined = ' '.join(prose)
    for hit in sorted({m.group(0).lower() for m in ADVICE_RE.finditer(joined)}):
        warnings.append(f'advice-like phrasing: {hit}')
    for hit in sorted({m.group(0).lower() for m in CLEAR_RE.finditer(joined)}):
        warnings.append(f"uses '{hit}' (air described as clear/clean?)")
    return card, reasons, warnings


def call_model(client, system, digest_text):
    """Returns (text, stop_reason, input_tokens, output_tokens)."""
    response = client.messages.create(
        model=MODEL, max_tokens=4000, system=system,
        messages=[{'role': 'user', 'content': digest_text + '\nWrite the briefing now.'}],
        output_config={'effort': 'low'})
    text = ''.join(b.text for b in response.content if getattr(b, 'type', None) == 'text')
    return text, response.stop_reason, response.usage.input_tokens, response.usage.output_tokens


def sdk_errors():
    try:
        import anthropic
        return (anthropic.APIStatusError, anthropic.APIConnectionError)
    except ImportError:
        return ()


def make_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'], base_url=BASE_URL)


def produce(folder, prompt, weights_sha, client=None, fake=None, now=None):
    """Run the whole pipeline on a snapshot folder; returns the row as a dict. `fake` = (text, stop_reason)."""
    started = time.monotonic()
    manifest, raw = digest_mod.load_raw(folder)
    digest_text = digest_mod.build(folder)
    row = {'created_at': (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'status': None,
           'model': MODEL, 'prompt_sha': sha12(prompt), 'weights_sha': weights_sha, 'card_json': None,
           'raw_text': None, 'reasons': [], 'warnings': [], 'digest_md': digest_text,
           'sources_json': {n: i['status'] for n, i in manifest['sources'].items()},
           'input_tokens': None, 'output_tokens': None, 'cost_usd': None}
    skip = precheck(manifest, raw)
    if skip:
        row.update(status='skipped', reasons=skip)
    else:
        try:
            if fake is not None:
                text, stop, tin, tout = fake[0], fake[1], 0, 0
            else:
                text, stop, tin, tout = call_model(client or make_client(), prompt, digest_text)
            card, reasons, warnings = validate(text, stop, digest_text)
            row.update(raw_text=text, input_tokens=tin, output_tokens=tout,
                       cost_usd=round(tin * USD_PER_M_IN / 1e6 + tout * USD_PER_M_OUT / 1e6, 6),
                       reasons=reasons, warnings=warnings,
                       status='rejected' if reasons else 'ok')
            if card is not None:
                row['card_json'] = json.dumps(card, ensure_ascii=False)
        except sdk_errors() as error:
            row.update(status='error', reasons=[f'{type(error).__name__}: {str(error)[:300]}'])
    row['seconds'] = round(time.monotonic() - started, 1)
    return row


def lit(value):
    """Text goes in as hex so quotes, semicolons and newlines can never confuse wrangler's file splitter."""
    if value is None:
        return 'NULL'
    if isinstance(value, (int, float)):
        return repr(value)
    return f"CAST(X'{str(value).replace(chr(0), '').encode().hex()}' AS TEXT)"


COLUMNS = ['created_at', 'status', 'model', 'prompt_sha', 'weights_sha', 'card_json', 'raw_text', 'reasons',
           'warnings', 'digest_md', 'sources_json', 'input_tokens', 'output_tokens', 'cost_usd', 'seconds']


def to_sql(row):
    values = [json.dumps(row[c], ensure_ascii=False) if c in ('reasons', 'warnings', 'sources_json') else row[c] for c in COLUMNS]
    return (f"INSERT INTO haze_briefing ({', '.join(COLUMNS)}) VALUES ({', '.join(lit(v) for v in values)});\n"
            f"DELETE FROM haze_briefing WHERE id NOT IN (SELECT id FROM haze_briefing ORDER BY id DESC LIMIT {KEEP_ROWS});\n")


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--output', help='SQL file to write (required unless --dry-run)')
    ap.add_argument('--snapshot-dir', help='reuse an existing snapshot folder instead of collecting live data')
    ap.add_argument('--dry-run', action='store_true', help='print the card and checks; write no SQL')
    ap.add_argument('--fake-answer', help='file holding a model answer to use instead of calling the API')
    ap.add_argument('--fake-stop-reason', default='end_turn')
    args = ap.parse_args(argv)
    if not args.output and not args.dry_run:
        ap.error('--output is required unless --dry-run')
    prompt = (HERE / 'prompt.md').read_text()
    weights_sha = sha12((HERE / 'weights.json').read_bytes())
    fake = (Path(args.fake_answer).read_text(), args.fake_stop_reason) if args.fake_answer else None
    with tempfile.TemporaryDirectory() as tmp:
        if args.snapshot_dir:  # work on a copy: digest.py writes into the folder
            folder = Path(tmp) / Path(args.snapshot_dir).name
            shutil.copytree(args.snapshot_dir, folder)
        else:
            import sources
            folder = sources.collect(Path(tmp))
        row = produce(folder, prompt, weights_sha, client=client, fake=fake)
    print(f"status={row['status']} reasons={row['reasons']} warnings={row['warnings']} tokens={row['input_tokens']}/{row['output_tokens']} cost=${row['cost_usd']}")
    if row['card_json']:
        print(json.dumps(json.loads(row['card_json']), indent=1, ensure_ascii=False))
    if not args.dry_run:
        Path(args.output).write_text(to_sql(row))
    return 0


if __name__ == '__main__':
    sys.exit(main())
