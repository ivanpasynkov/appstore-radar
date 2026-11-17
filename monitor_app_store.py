"""Monitor Apple's new-app sitemap across storefronts."""
import argparse
import gzip
import io
import json
import logging
import re
import sqlite3
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as XML

INDEX = 'https://apps.apple.com/sitemaps_apps_index_new-app_1.xml'
INTERVAL = 3 * 3600
APP_URL = re.compile(r'https://apps\.apple\.com/([a-z]{2})/app/(?:[^/?]+/)?id(\d+)(?:\?.*)?$')
SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldown (until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    url TEXT PRIMARY KEY, due REAL NOT NULL DEFAULT 0, etag TEXT, modified TEXT
);
CREATE INDEX IF NOT EXISTS source_queue ON sources(due);
CREATE TABLE IF NOT EXISTS apps (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, modified TEXT, discovered TEXT NOT NULL,
    metadata TEXT, lookup_due REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_regions (
    app_id TEXT NOT NULL, country TEXT NOT NULL, url TEXT NOT NULL,
    PRIMARY KEY(app_id,country)
);
CREATE INDEX IF NOT EXISTS lookup_queue ON apps(lookup_due);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fetch(url, headers=None):
    request = Request(url, headers={'User-Agent': 'AppStoreNewAppMonitor/1.0', **(headers or {})})
    try:
        with urlopen(request, timeout=45) as response:
            return response.read(), response.headers
    except HTTPError as exc:
        if exc.code == 304:
            return None, exc.headers
        raise


def app_entries(data):
    stream = io.BytesIO(data)
    if data.startswith(b'\x1f\x8b'):
        stream = gzip.GzipFile(fileobj=stream)
    with stream:
        events = XML.iterparse(stream, events=('start', 'end'))
        _, root = next(events)
        if root.tag.split('}')[-1] != 'urlset':
            raise ValueError('Expected a sitemap urlset')
        for event, entry in events:
            if event != 'end' or entry.tag.split('}')[-1] != 'url':
                continue
            urls = [entry.findtext('{*}loc', '')]
            urls.extend(link.get('href', '') for link in entry.findall('{*}link'))
            apps = {}
            for url in urls:
                match = APP_URL.fullmatch(url)
                if match:
                    country, app_id = match.groups()
                    apps.setdefault(app_id, {})[country] = url.split('?')[0]
            for app_id, regions in apps.items():
                country = 'us' if 'us' in regions else min(regions)
                yield app_id, regions[country], entry.findtext('{*}lastmod'), regions
            root.clear()


def read_sitemap(source):
    url, etag, modified = source
    headers = {}
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified
    raw, response = fetch(url, headers)
    entries = None
    if raw is not None:
        if url == INDEX:
            root = XML.fromstring(raw)
            entries = [node.text for node in root.findall('{*}sitemap/{*}loc') if node.text]
            if not entries:
                raise ValueError('Empty or invalid Apple sitemap index')
        else:
            entries = list(app_entries(raw))
    return entries, response.get('ETag', etag), response.get('Last-Modified', modified)


def save_sitemap(db, source, result):
    url = source[0]
    entries, etag, modified = result
    if entries is not None:
        if url == INDEX:
            existing = {row[0] for row in db.execute('SELECT url FROM sources')}
            db.executemany('INSERT OR IGNORE INTO sources(url) VALUES (?)', ((u,) for u in entries))
            db.executemany('DELETE FROM sources WHERE url=?',
                           ((u,) for u in existing.difference(entries, {INDEX})))
            logging.info('Index contains %s sitemaps', len(entries))
        else:
            stamp = utcnow()
            for app_id, app_url, lastmod, regions in entries:
                existing = db.execute('SELECT url FROM apps WHERE id=?', (app_id,)).fetchone()
                if existing:
                    country = APP_URL.fullmatch(existing[0])[1]
                    app_url = regions.get(country, existing[0])
                db.execute("""
                INSERT INTO apps(id,url,modified,discovered) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET url=excluded.url,
                    modified=coalesce(excluded.modified,apps.modified),lookup_due=0
                WHERE apps.url != excluded.url OR (excluded.modified IS NOT NULL AND excluded.modified > coalesce(apps.modified,''))
                """, (app_id, app_url, lastmod, stamp))
                db.executemany("""
                    INSERT INTO app_regions(app_id,country,url) VALUES (?,?,?)
                    ON CONFLICT(app_id,country) DO UPDATE SET url=excluded.url
                """, ((app_id, country, region_url) for country, region_url in regions.items()))
    db.execute('UPDATE sources SET due=?,etag=?,modified=? WHERE url=?',
               (time.time()+INTERVAL, etag, modified, url))


def lookup(db, ids, country='us'):
    query = urlencode({'country': country, 'id': ','.join(ids), 'limit': len(ids)})
    raw, _ = fetch('https://itunes.apple.com/lookup?' + query)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        raise ValueError('Invalid Lookup results')
    if any(not isinstance(app, dict) or
           (app.get('wrapperType') == 'software' and 'trackId' not in app)
           for app in payload['results']):
        raise ValueError('Invalid Lookup record')
    records = {str(app['trackId']): app for app in payload['results']
               if app.get('wrapperType') == 'software'}
    for app_id in ids:
        if app_id in records:
            db.execute('UPDATE apps SET metadata=?,lookup_due=NULL WHERE id=?',
                       (json.dumps(records[app_id], ensure_ascii=False), app_id))
        else:
            db.execute('UPDATE apps SET lookup_due=? WHERE id=?', (time.time()+86400, app_id))


def retry_delay(exc):
    if isinstance(exc, HTTPError):
        value = exc.headers.get('Retry-After', '')
        if value.isdigit():
            return max(60, int(value))
        if value:
            try:
                return max(60, parsedate_to_datetime(value).timestamp()-time.time())
            except (TypeError, ValueError, OverflowError):
                pass
        if exc.code in (404, 410):
            return 86400
    return 300


def run_job(db, operation, retry_sql, keys, *, context):
    try:
        with db:
            operation()
    except (HTTPException, OSError, ValueError, KeyError, XML.ParseError, EOFError, zlib.error) as exc:
        delay = retry_delay(exc)
        with db:
            db.executemany(retry_sql, ((time.time()+delay, key) for key in keys))
            if isinstance(exc, HTTPError) and exc.code == 429:
                db.execute('DELETE FROM cooldown')
                db.execute('INSERT INTO cooldown VALUES (?)', (time.time()+delay,))
        logging.warning('%s deferred for %.0f seconds: %s', context, delay, exc)


def cooling_down(db):
    until = db.execute('SELECT max(until) FROM cooldown').fetchone()[0] or 0
    return until > time.time()


def monitor(db):
    last_lookup = 0
    pending = None
    with ThreadPoolExecutor(max_workers=1) as worker:
        while True:
            if pending is not None and pending.done():
                run_job(db, lambda: save_sitemap(db, source, pending.result()),
                        'UPDATE sources SET due=? WHERE url=?', [source[0]],
                        context=f'Sitemap {source[0]}')
                pending = None
            if cooling_down(db):
                time.sleep(1)
                continue
            if pending is None:
                source = db.execute(
                    'SELECT url,etag,modified FROM sources WHERE due<=? ORDER BY (url=?) DESC,due LIMIT 1',
                    (time.time(), INDEX)).fetchone()
                if source:
                    pending = worker.submit(read_sitemap, source)
            if time.monotonic() >= last_lookup:
                rows = db.execute(
                    'SELECT id,url FROM apps WHERE lookup_due<=? ORDER BY lookup_due LIMIT 200',
                    (time.time(),)).fetchall()
                if rows:
                    country = APP_URL.fullmatch(rows[0][1])[1]
                    ids = [app_id for app_id, url in rows if APP_URL.fullmatch(url)[1] == country]
                    run_job(db, lambda: lookup(db, ids, country),
                            'UPDATE apps SET lookup_due=? WHERE id=?', ids,
                            context=f'Lookup {country} IDs {",".join(ids)}')
                    last_lookup = time.monotonic()+5
            time.sleep(1)


def initialize(db):
    db.executescript(SCHEMA)
    if db.execute('PRAGMA user_version').fetchone()[0] < 1:
        with db:
            for app_id, url in db.execute('SELECT id,url FROM apps'):
                match = APP_URL.fullmatch(url)
                if match:
                    db.execute('INSERT OR IGNORE INTO app_regions VALUES (?,?,?)',
                               (app_id, match[1], url))
            # The previous version cached US-only parses. Fetch all shards once.
            db.execute('UPDATE sources SET due=0,etag=NULL,modified=NULL')
            db.execute('PRAGMA user_version=1')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='monitor', choices=['monitor', 'export'])
    parser.add_argument('--db', type=Path, default=Path('data/apps.sqlite3'))
    args = parser.parse_args()
    args.db = args.db.resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    if args.command == 'export':
        if not args.db.is_file():
            parser.error('Database does not exist. Run the monitor first.')
        db = sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro', uri=True)
        has_regions = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_regions'").fetchone()
        for app_id, url, modified, discovered, metadata in db.execute(
                'SELECT id,url,modified,discovered,metadata FROM apps ORDER BY rowid'):
            regions = [row[0] for row in db.execute(
                'SELECT country FROM app_regions WHERE app_id=? ORDER BY country', (app_id,))] if has_regions else [APP_URL.fullmatch(url)[1]]
            print(json.dumps({'app_id': app_id, 'url': url, 'regions': regions,
                              'country': APP_URL.fullmatch(url)[1], 'sitemap_lastmod': modified,
                              'first_seen_at': discovered, 'metadata': json.loads(metadata) if metadata else None}, ensure_ascii=False))
        return
    args.db.parent.mkdir(parents=True, exist_ok=True)
    # A separate SQLite lock lets exports read while enforcing one network worker.
    guard = sqlite3.connect(str(args.db)+'.lock', timeout=0)
    try:
        guard.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError as exc:
        parser.exit(1, f'Cannot lock the monitor database ({exc}).\n')
    db = sqlite3.connect(args.db)
    db.execute('PRAGMA journal_mode=WAL')
    initialize(db)
    with db:
        db.execute('INSERT OR IGNORE INTO sources(url) VALUES (?)', (INDEX,))
        db.execute('UPDATE sources SET due=-1 WHERE url=?', (INDEX,))
    monitor(db)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
