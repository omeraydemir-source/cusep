import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS companies (
 id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 website TEXT NOT NULL DEFAULT '', directory_url TEXT NOT NULL DEFAULT '',
 source_sector TEXT NOT NULL DEFAULT '',
 sectors TEXT NOT NULL DEFAULT '[]', description TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'needs_review',
 locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
 id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL REFERENCES companies(id),source_url TEXT NOT NULL,
 name TEXT NOT NULL,address TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',email TEXT NOT NULL DEFAULT '',
 source_sector TEXT NOT NULL DEFAULT '',operations TEXT NOT NULL DEFAULT '',website TEXT NOT NULL DEFAULT '',last_seen TEXT NOT NULL,
 UNIQUE(source_url,name,address)
);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, company_id INTEGER REFERENCES companies(id),
 source_url TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 sha256 TEXT NOT NULL, content TEXT NOT NULL, raw BLOB, fetched_at TEXT NOT NULL,
 UNIQUE(source_url,sha256)
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
 identity TEXT NOT NULL, name TEXT NOT NULL, specs TEXT NOT NULL DEFAULT '{}',
 source_url TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
 document_id INTEGER REFERENCES documents(id),
 status TEXT NOT NULL DEFAULT 'needs_review', locked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(company_id, identity)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
 message TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, finished_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS products_company ON products(company_id);
CREATE INDEX IF NOT EXISTS docs_company ON documents(company_id);
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def folded(value):
    value = str(value).replace('ı', 'i').replace('İ', 'I')
    return ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c)).lower()


def key(value):
    return re.sub(r'[^a-z0-9]+', ' ', folded(value)).strip()


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {r[1] for r in conn.execute('PRAGMA table_info(companies)')}
            if 'source_sector' not in columns:
                conn.execute("ALTER TABLE companies ADD COLUMN source_sector TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.create_function('fold', 1, folded)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args).lastrowid

    def company(self, name, website='', directory_url='', sectors=None, description='', evidence='', source_sector=''):
        name = re.sub(r'\s+', ' ', name).strip()[:500]
        if not name:
            raise ValueError('Company name is required')
        identity = key(name)
        ts = now()
        with self.connect() as conn:
            existing = conn.execute('SELECT * FROM companies WHERE identity=?', (identity,)).fetchone()
            if existing:
                conn.execute('UPDATE companies SET last_seen=? WHERE id=?', (ts, existing['id']))
                if not existing['locked']:
                    values = {
                        'website': website or existing['website'],
                        'source_sector': source_sector or existing['source_sector'],
                        'directory_url': directory_url or existing['directory_url'],
                        'sectors': json.dumps(sectors or json.loads(existing['sectors']), ensure_ascii=False),
                        'description': description or existing['description'],
                        'evidence': evidence or existing['evidence'],
                    }
                    changed = any(values[k] != existing[k] for k in values)
                    if changed:
                        conn.execute('''UPDATE companies SET website=:website,directory_url=:directory_url,
                          sectors=:sectors,source_sector=:source_sector,description=:description,evidence=:evidence,status='needs_review',
                          updated_at=:ts WHERE id=:id''', {**values, 'ts': ts, 'id': existing['id']})
                return existing['id']
            return conn.execute('''INSERT INTO companies
              (identity,name,website,directory_url,sectors,description,evidence,source_sector,created_at,updated_at,last_seen)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
              (identity, name, website, directory_url, json.dumps(sectors or [], ensure_ascii=False),
               description, evidence, source_sector, ts, ts, ts)).lastrowid

    def listing(self, company_id, url, item):
        with self.connect() as conn:
            conn.execute("""INSERT INTO listings (company_id,source_url,name,address,phone,email,source_sector,operations,website,last_seen)
              VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_url,name,address) DO UPDATE SET
              phone=excluded.phone,email=excluded.email,source_sector=excluded.source_sector,operations=excluded.operations,
              website=excluded.website,last_seen=excluded.last_seen""",
              (company_id,url,item['name'],item.get('address',''),item.get('phone',''),item.get('email',''),
               item.get('source_sector',''),item.get('operations',''),item.get('website',''),now()))

    def document(self, url, content, kind, title, company_id=None, raw=None):
        digest = hashlib.sha256(raw if raw is not None else content.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute('''INSERT INTO documents (company_id,source_url,kind,title,sha256,content,raw,fetched_at)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_url,sha256) DO UPDATE SET fetched_at=excluded.fetched_at''',
              (company_id, url, kind, title, digest, content, raw, now()))
            return conn.execute('SELECT id FROM documents WHERE source_url=? AND sha256=?', (url, digest)).fetchone()[0]

    def product(self, company_id, name, specs, url, evidence, document_id=None):
        identity = url + '|' + key(name)
        ts = now()
        data = json.dumps(specs, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            old = conn.execute('SELECT * FROM products WHERE company_id=? AND identity=?', (company_id, identity)).fetchone()
            if old:
                conn.execute('UPDATE products SET last_seen=? WHERE id=?', (ts, old['id']))
                if not old['locked'] and (old['specs'] != data or old['evidence'] != evidence):
                    conn.execute('''UPDATE products SET specs=?,evidence=?,document_id=?,status='needs_review',updated_at=?
                      WHERE id=?''', (data, evidence, document_id, ts, old['id']))
                return old['id']
            return conn.execute('''INSERT INTO products
              (company_id,identity,name,specs,source_url,evidence,document_id,created_at,updated_at,last_seen)
              VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (company_id, identity, name[:500], data, url, evidence[:12000], document_id, ts, ts, ts)).lastrowid
