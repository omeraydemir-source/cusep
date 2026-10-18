import ipaddress
import socket
import re
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .db import now
from .extract import (canonical, classify, directory_entries, document_candidates, host,
                      pdf_pages, product_page, profile_data, relevant_links)

USER_AGENT = 'CusepResearch/1.0 (public company and catalogue research; respects robots.txt)'
MAX_BYTES = 20 * 1024 * 1024


class FetchError(ValueError):
    pass


def validate_url(url):
    value = canonical(url)
    if not value:
        raise FetchError('Use a public HTTP(S) URL without embedded credentials.')
    parsed = urlsplit(value)
    if parsed.port and parsed.port not in (80, 443):
        raise FetchError('Only public web ports 80 and 443 are supported.')
    hostname = parsed.hostname.lower().rstrip('.')
    if hostname in ('localhost', 'metadata.google.internal') or hostname.endswith(('.localhost', '.local', '.internal', '.lan', '.home')):
        raise FetchError('Private/local hostnames are blocked.')
    try:
        literal = ipaddress.ip_address(hostname)
        if not literal.is_global: raise FetchError('Private, loopback, and reserved network destinations are blocked.')
    except ValueError as exc:
        if isinstance(exc, FetchError): raise
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', hostname) or '.' not in hostname:
            raise FetchError('Use a valid public hostname.') from exc
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # Managed cloud environments resolve target DNS inside their egress proxy.
        # Only defer resolution when Requests will actually use a configured proxy.
        proxies = requests.utils.get_environ_proxies(value)
        proxy = requests.utils.select_proxy(value, proxies)
        if proxy and urlsplit(proxy).scheme in ('http', 'https'):
            return value
        raise FetchError(f'Cannot resolve {parsed.hostname}. Check network/domain access.') from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FetchError('Private, loopback, and reserved network destinations are blocked.')
    return value


class Fetcher:
    def __init__(self, delay=1.2):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.delay = delay
        self.last = {}
        self.robots = {}

    def _request(self, url, enforce_robots=False):
        for redirect in range(6):
            url = validate_url(url)
            if enforce_robots: self.allowed(url)
            origin = urlsplit(url).netloc
            pause = max(0, self.last.get(origin, 0) + self.delay - time.monotonic())
            if pause: time.sleep(pause)
            self.last[origin] = time.monotonic()
            try:
                with self.session.get(url, timeout=(10, 35), stream=True, allow_redirects=False) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        if not r.headers.get('Location'): raise FetchError('Redirect without a destination')
                        next_url = canonical(r.headers['Location'], url)
                        if urlsplit(url).scheme == 'https' and urlsplit(next_url).scheme != 'https':
                            raise FetchError('Refused HTTPS-to-HTTP redirect.')
                        # The next iteration verifies destination robots policy before downloading.
                        url = next_url
                        continue
                    if r.status_code >= 400:
                        raise FetchError(f'HTTP {r.status_code} from {urlsplit(url).hostname}. Check domain access or site permissions.')
                    data = bytearray()
                    for chunk in r.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES: raise FetchError('Document exceeds 20 MB download limit.')
                    content_type = r.headers.get('Content-Type', '').lower()
                    encoding = r.encoding if r.encoding and 'charset=' in content_type else 'utf-8'
                    return bytes(data), content_type, url, encoding
            except requests.RequestException as exc:
                raise FetchError(f'Cannot fetch {urlsplit(url).hostname}: {type(exc).__name__}. Check network allowlist, TLS and site access.') from exc
        raise FetchError('Too many redirects')

    def allowed(self, url):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self.robots:
            robots_url = origin + '/robots.txt'
            try:
                raw, _, final, enc = self._request(robots_url)
                parser = RobotFileParser()
                parser.parse(raw.decode(enc, errors='replace').splitlines())
                delay = parser.crawl_delay('CusepResearch') or parser.crawl_delay('*')
                if delay:
                    if float(delay) > 60: raise FetchError('Site requires more than 60 seconds between requests; use a manual source import.')
                    self.delay = max(self.delay, float(delay))
                self.robots[origin] = parser
            except FetchError as exc:
                if 'HTTP 404 ' in str(exc) or 'HTTP 410 ' in str(exc):
                    self.robots[origin] = None
                else:
                    raise FetchError(f'Could not verify robots.txt for {p.hostname}: {exc}') from exc
        parser = self.robots[origin]
        if parser and not parser.can_fetch('CusepResearch', url):
            raise FetchError(f'robots.txt disallows this path on {p.hostname}. Use an authorized manual upload instead.')

    def get(self, url):
        url = validate_url(url)
        return self._request(url, enforce_robots=True)


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        db.execute("UPDATE jobs SET state='interrupted',message='Server restarted; rerun this import.',finished_at=? WHERE state IN ('queued','running')", (now(),))

    def log(self, job_id, message):
        import json
        with self.db.connect() as conn:
            row = conn.execute('SELECT details,cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row['cancel_requested']: raise Cancelled()
            details = json.loads(row['details'])
            details.append({'at': now(), 'message': str(message)[:2000]})
            conn.execute('UPDATE jobs SET message=?,details=? WHERE id=?', (str(message)[:2000], json.dumps(details[-1000:]), job_id))

    def start(self, kind, function):
        with self.lock:
            if self.db.query("SELECT id FROM jobs WHERE state IN ('queued','running')"):
                raise ValueError('An import is already running. Wait or cancel it in Activity.')
            job_id = self.db.execute('INSERT INTO jobs (kind,state,created_at) VALUES (?,\'queued\',?)', (kind, now()))
            thread = threading.Thread(target=self._run, args=(job_id, function), daemon=True)
            thread.start()
            return job_id

    def _run(self, job_id, function):
        self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        try:
            message = function(lambda msg: self.log(job_id, msg))
            self.log(job_id, message)
            self.db.execute("UPDATE jobs SET state='completed',finished_at=? WHERE id=?", (now(), job_id))
        except Cancelled:
            self.db.execute("UPDATE jobs SET state='cancelled',message='Cancelled; completed records were retained.',finished_at=? WHERE id=?", (now(), job_id))
        except Exception as exc:
            self.db.execute("UPDATE jobs SET state='failed',message=?,finished_at=? WHERE id=?", (str(exc)[:2000], now(), job_id))


def import_directory(db, url, log, fetcher=None, max_pages=100):
    fetcher = fetcher or Fetcher()
    url = canonical(url)
    if not url: raise ValueError('Enter a valid directory URL.')
    queue = deque([url])
    visited, profiles, companies = set(), set(), set()
    issues = []
    listing_count = 0
    while queue and len(visited) < max_pages:
        page = queue.popleft()
        if page in visited: continue
        visited.add(page)
        log(f'Directory page {len(visited)}: {page}')
        try:
            raw, content_type, final, enc = fetcher.get(page)
            if 'html' not in content_type and b'<' not in raw[:200]: raise ValueError('Expected an HTML directory.')
            html = raw.decode(enc, errors='replace')
            entries, next_pages = directory_entries(html, final)
            db.document(final, html, 'directory', 'Company directory', raw=raw)
            listing_count += sum(len(item.get('listings',[])) for item in entries)
            for item in entries:
                sectors, evidence = classify(item['description'] or item.get('source_sector') or item['name'])
                cid = db.company(**{k:v for k,v in item.items() if k != 'listings'}, sectors=sectors, evidence='Keyword suggestion, verify: ' + evidence if evidence else 'No reliable sector signal; review needed.')
                for listing in item.get('listings',[]): db.listing(cid, final, listing)
                companies.add(cid)
                profile = item['directory_url']
                if profile and profile != final and host(profile) == host(url) and profile not in profiles:
                    profiles.add(profile)
                    log(f'Company profile: {item["name"]}')
                    try:
                        body, _, target, encoding = fetcher.get(profile)
                        site, description = profile_data(body.decode(encoding, errors='replace'), target)
                        sectors, evidence = classify(item['name'] + ' ' + description)
                        db.document(target, description, 'profile', item['name'], cid, raw=body)
                        db.company(item['name'], site or item['website'], profile, sectors, description, 'Keyword suggestion, verify: ' + evidence)
                    except FetchError as exc:
                        issues.append(str(exc)); log(str(exc))
            for link in next_pages:
                if link not in visited and link not in queue: queue.append(link)
        except (FetchError, ValueError) as exc:
            if len(visited) == 1: raise
            issues.append(str(exc)); log(str(exc))
    if not companies:
        raise ValueError('No company records were recognized. The site may require JavaScript or a site-specific adapter. No completeness claim is possible; upload a company CSV/XLSX or inspect the saved source.')
    if queue: issues.append(f'Directory page cap ({max_pages}) reached; pagination remains.')
    if issues: raise ValueError(f'Partial directory import: {len(companies)} companies retained; {len(issues)} issues. ' + issues[0])
    if listing_count:
        return f'Imported/refreshed {len(companies)} company names from all {listing_count} recognized source listings across {len(visited)} pages. Exact normalized names are grouped; location records and original sector labels are preserved. Review suggested classifications.'
    return f'Imported/refreshed {len(companies)} company candidates across {len(visited)} directory pages. Review source coverage; generic parsing cannot guarantee every company was recognized.'


def import_catalogue(db, raw, url, title, company_id, log):
    pages, warnings = pdf_pages(raw)
    text = '\n\n'.join(f'--- Page {i+1} ---\n{p}' for i, p in enumerate(pages))
    doc_id = db.document(url, text, 'catalogue', title, company_id, raw=raw)
    count = 0
    for i, page in enumerate(pages):
        log(f'Catalogue page {i+1}/{len(pages)}')
        for product in document_candidates(page):
            db.product(company_id, product['name'], product['specs'], url + f'#page={i+1}', product['evidence'], doc_id)
            count += 1
    for warning in warnings: log(warning)
    if not text.strip() or all(not p.strip() for p in pages): raise ValueError('No readable catalogue text; no product extraction was possible.')
    return count, warnings


def crawl_company(db, company_id, log, fetcher=None, max_pages=30):
    fetcher = fetcher or Fetcher()
    company = db.query('SELECT * FROM companies WHERE id=?', (company_id,))
    if not company: raise ValueError('Company not found.')
    company = company[0]
    site = canonical(company['website'])
    if not site: raise ValueError(f'{company["name"]}: add a company website before crawling.')
    queue = deque([site]); visited = set(); product_ids = set(); catalogue_count = 0; issues = []
    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited: continue
        visited.add(url)
        log(f'{company["name"]} · page {len(visited)}/{max_pages}: {url}')
        try:
            raw, content_type, final, enc = fetcher.get(url)
            if host(final) != host(site):
                raise FetchError('Website redirected to a different domain. Review and update the website URL before crawling.')
            if 'application/pdf' in content_type or raw.startswith(b'%PDF'):
                count, warnings = import_catalogue(db, raw, final, final.rsplit('/', 1)[-1], company_id, log)
                catalogue_count += 1
                if warnings: issues.extend(warnings)
                continue
            if 'html' not in content_type and b'<' not in raw[:200]: continue
            html = raw.decode(enc, errors='replace')
            products, text = product_page(html, final)
            doc_id = db.document(final, text, 'webpage', final, company_id, raw=raw)
            for product in products:
                product_ids.add(db.product(company_id, product['name'], product['specs'], final, product['evidence'], doc_id))
            for link in relevant_links(html, final, site):
                if link not in visited and link not in queue: queue.append(link)
        except (FetchError, ValueError) as exc:
            issues.append(str(exc)); log(str(exc))
    if queue: issues.append(f'Page limit {max_pages} reached; more links remain. Increase the crawl limit or import a catalogue directly.')
    result = f'{company["name"]}: {len(visited)} pages checked; {len(product_ids)} web product candidates; {catalogue_count} catalogues processed.'
    if issues: raise ValueError(result + f' Partial run: {len(issues)} issues. ' + issues[0])
    return result + ' Review product/manufacturer attribution and catalogue candidates in the database.'
