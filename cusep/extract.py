"""Deterministic extraction. Every result is a candidate requiring human review."""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from PIL import Image, ImageOps
from pypdf import PdfReader

from .db import folded, key

SECTORS = {
    'Machinery & equipment': ['makina', 'makine', 'machinery', 'machine', 'hidrolik', 'pnömatik'],
    'Metal & fabrication': ['metal', 'çelik', 'steel', 'döküm', 'casting', 'kaynak', 'alüminyum'],
    'Defence & aerospace': ['savunma', 'defence', 'defense', 'havacılık', 'aerospace'],
    'Electrical & electronics': ['elektrik', 'elektronik', 'electrical', 'electronics', 'kablo'],
    'Construction & materials': ['inşaat', 'yapı', 'construction', 'beton', 'cement', 'çimento'],
    'Plastics & chemicals': ['plastik', 'plastic', 'kimya', 'chemical', 'kauçuk', 'rubber', 'boya'],
    'Food & agriculture': ['gıda', 'food', 'tarım', 'agricultur', 'yem', 'un sanayi'],
    'Logistics & services': ['lojistik', 'logistics', 'nakliyat', 'transport', 'danışmanlık'],
    'Furniture & wood': ['mobilya', 'furniture', 'ahşap', 'wood', 'orman ürün'],
    'Automotive & transport': ['otomotiv', 'automotive', 'treyler', 'trailer', 'otobüs'],
    'Energy & environment': ['enerji', 'energy', 'güneş', 'solar', 'arıtma', 'recycling'],
    'Medical & pharmaceuticals': ['medikal', 'ilaç', 'tıbbi', 'medical', 'pharmaceutical'],
    'Mining & natural stone': ['maden', 'mermer', 'granit', 'mining', 'marble', 'stone'],
    'Textiles & packaging': ['tekstil', 'textile', 'ambalaj', 'packaging', 'kağıt'],
}
PRODUCT_WORDS = ('urun', 'product', 'imalat', 'manufactur', 'katalog', 'catalog', 'model', 'teknik', 'technical')
COMPANY_WORDS = re.compile(r'\b(sanayi|san\.?|ticaret|tic\.?|ltd\.?|limited|anonim|a\.?\s?ş\.?|a\.?s\.?|şti\.?|şirketi)\b', re.I)
SOCIAL = ('facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'linkedin.com', 'youtube.com', 'google.com', 'whatsapp.com')


def canonical(url, base=''):
    p = urlsplit(urljoin(base, str(url).strip()))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or re.search(r'[\s;\\]', p.netloc):
        return ''
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    try:
        hostname = p.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        netloc = ('['+hostname+']') if ':' in hostname else hostname
        if p.port: netloc += ':'+str(p.port)
    except (ValueError,UnicodeError): return ''
    return urlunsplit((p.scheme, netloc, p.path or '/', urlencode(sorted(pairs)), ''))


def host(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def classify(text):
    text = ' ' + key(text) + ' '
    found, evidence = [], []
    for sector, words in SECTORS.items():
        matches = [w for w in words if ' ' + key(w) + ' ' in text]
        if matches:
            found.append(sector)
            evidence.append(f'{sector}: {", ".join(matches)}')
    return found, '; '.join(evidence)


def soup_for(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, noscript, svg'):
        node.decompose()
    return soup


def text_lines(soup):
    return '\n'.join(line.strip() for line in soup.get_text('\n').splitlines() if line.strip())


def baskent_entries(soup, base):
    """Adapter for the live Başkent OSB GoodLayers directory (all cards in HTML)."""
    cards = soup.select('.gdlr-core-course-item-list')
    if not cards: return None
    grouped = {}
    for card in cards:
        title = card.select_one('.gdlr-core-course-item-title')
        if not title or not title.get_text(strip=True):
            raise ValueError('A Başkent OSB card has no company name; adapter needs review.')
        name = re.sub(r'\s+', ' ', title.get_text(' ', strip=True))
        record = {'name': name}
        for icon, field in [('fa-map-marker','address'),('fa-phone','phone'),('fa-envelope','email'),
                            ('fa-building','source_sector'),('fa-asterisk','operations'),('fa-globe','website')]:
            node = card.select_one('.'+icon)
            info = node.find_parent(class_='gdlr-core-course-item-info') if node else None
            tail = info.select_one('.gdlr-core-tail') if info else None
            value = re.sub(r'\s+', ' ', tail.get_text(' ',strip=True)) if tail else ''
            if field == 'website' and tail:
                link = tail.select_one('a[href]')
                raw_site = link['href'] if link else value
                candidates = [canonical(x if x.startswith(('http://','https://')) else 'https://'+x) for x in re.split(r'[;,\s]+',raw_site.strip()) if x]
                value = next((x for x in candidates if x),'')
                if host(value)==host(base): value=''
            record[field] = value
        ident = key(name)
        if ident not in grouped:
            grouped[ident] = {'name':name,'website':record['website'],'directory_url':base,
                              'source_sector':record['source_sector'],'description':'','listings':[]}
        entry=grouped[ident]
        entry['website']=entry['website'] or record['website']
        entry['listings'].append(record)
        entry['description']=' | '.join(dict.fromkeys(r['operations'] for r in entry['listings'] if r['operations']))
    return list(grouped.values()), []


def directory_entries(html, base):
    """Table/card directory adapters plus company-profile link fallback; no invented rows."""
    soup = soup_for(html)
    specific = baskent_entries(soup, base)
    if specific is not None: return specific
    entries = []
    seen = set()
    containers = soup.select('tr, .firma, .firma-item, .firma-kart, .company, .company-card, .company-item, [data-company]')
    for node in containers:
        if node.select('th') and not node.select('td'):
            continue
        links = [(a, canonical(a.get('href', ''), base)) for a in node.select('a[href]')]
        external = [(a, u) for a, u in links if u and host(u) != host(base) and not any(host(u).endswith(h) for h in SOCIAL)]
        profiles = [(a, u) for a, u in links if u and host(u) == host(base) and profile_link(u, base)]
        title = node.select_one('[data-company-name], .firma-adi, .company-name, h2, h3, h4, h5')
        if not title:
            cells = node.select('td')
            title = next((c for c in cells if COMPANY_WORDS.search(c.get_text(' ', strip=True))), None)
            if not title and profiles:
                title = profiles[0][0]
        name = title.get_text(' ', strip=True) if title else ''
        if not name or len(name) > 500 or (not external and not profiles and not COMPANY_WORDS.search(name)):
            continue
        website = external[0][1] if external else ''
        detail = profiles[0][1] if profiles else base
        description = node.get_text(' ', strip=True)[:4000]
        if key(name) not in seen:
            entries.append({'name': name, 'website': website, 'directory_url': detail, 'description': description})
            seen.add(key(name))
    for a in soup.select('a[href]'):
        u = canonical(a['href'], base)
        name = a.get_text(' ', strip=True)
        if not u or not name or key(name) in seen or len(name) > 500:
            continue
        if (host(u) == host(base) and profile_link(u, base)) or (COMPANY_WORDS.search(name) and host(u) != host(base) and not any(host(u).endswith(h) for h in SOCIAL)):
            entries.append({'name': name, 'website': u if host(u) != host(base) else '',
                            'directory_url': u if host(u) == host(base) else base, 'description': name})
            seen.add(key(name))
    pages = set()
    for a in soup.select('a[href]'):
        u = canonical(a['href'], base)
        if not u or host(u) != host(base):
            continue
        p, b = urlsplit(u), urlsplit(base)
        label = folded(a.get_text(' ', strip=True))
        if (p.path.rstrip('/') == b.path.rstrip('/') and any(k.lower() in ('page', 'sayfa', 'paged', 'p') for k, v in parse_qsl(p.query))) or re.search(r'/(page|sayfa)/\d+/?$', p.path) or (a.get('rel') and 'next' in a['rel']) or label in ('sonraki', 'next', '›', '»'):
            pages.add(u)
    return entries, sorted(pages)


def profile_link(url, base):
    p = folded(urlsplit(url).path)
    b = folded(urlsplit(base).path)
    return p.rstrip('/') != b.rstrip('/') and bool(re.search(r'/(firma|firmalar|company|companies|uye|uyeler|katilimci)(?:/|-detay/|-detayi/)[^/]+', p))


def profile_data(html, base):
    soup = soup_for(html)
    site = ''
    for a in soup.select('a[href]'):
        url = canonical(a['href'], base)
        if url and host(url) != host(base) and not any(host(url).endswith(h) for h in SOCIAL):
            site = url
            break
    root = soup.select_one('main, article, .firma-detay, .company-detail') or soup
    return site, text_lines(root)[:12000]


def specs_from(soup):
    specs = {}
    for row in soup.select('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) == 2:
            k, v = (c.get_text(' ', strip=True) for c in cells)
            if 1 < len(k) < 100 and v and len(v) < 500:
                specs[k] = v
    for dt in soup.select('dt'):
        dd = dt.find_next_sibling('dd')
        if dd:
            specs[dt.get_text(' ', strip=True)] = dd.get_text(' ', strip=True)[:500]
    for line in text_lines(soup).splitlines():
        m = re.match(r'^([^:：]{2,70})[:：]\s*(.{1,250})$', line)
        if m:
            specs.setdefault(m[1].strip(), m[2].strip())
    return dict(list(specs.items())[:100])


def product_page(html, base):
    # JSON-LD Product is the strongest generic signal; visible tables remain source evidence.
    original = BeautifulSoup(html, 'html.parser')
    soup = soup_for(html)
    root = soup.select_one('main, article, [itemtype*="Product"], .product-detail, .urun-detay') or soup
    specs = specs_from(root)
    text = text_lines(root)
    products = []
    def visit(obj):
        if isinstance(obj, list):
            for x in obj: visit(x)
        elif isinstance(obj, dict):
            types = obj.get('@type', [])
            if isinstance(types, str): types = [types]
            if 'Product' in types and isinstance(obj.get('name'), str):
                attributes = dict(specs)
                for prop in obj.get('additionalProperty', []) if isinstance(obj.get('additionalProperty'), list) else []:
                    if isinstance(prop, dict) and prop.get('name') and prop.get('value') is not None:
                        attributes[str(prop['name'])] = str(prop['value']) + (' ' + str(prop['unitText']) if prop.get('unitText') else '')
                for k in ('model', 'sku', 'mpn', 'material', 'weight', 'width', 'height', 'depth'):
                    if k in obj: attributes[k] = str(obj[k])
                products.append({'name': obj['name'], 'specs': attributes, 'evidence': str(obj.get('description', ''))[:3000] + '\n' + text[:6000]})
            for k in ('@graph', 'mainEntity', 'itemListElement', 'item'):
                if k in obj: visit(obj[k])
    for script in original.select('script[type="application/ld+json"]'):
        try: visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError): pass
    heading = root.select_one('h1') or soup.select_one('h1')
    title = heading.get_text(' ', strip=True) if heading else ''
    generic = key(title) in ('urunler', 'urunlerimiz', 'products', 'our products', 'product', 'urun', 'home', 'ana sayfa', 'katalog', 'catalogue', 'catalog')
    if not products and title and not generic and specs and any(w in folded(urlsplit(base).path + ' ' + text[:2000]) for w in PRODUCT_WORDS):
        products.append({'name': title, 'specs': specs, 'evidence': text[:9000]})
    return products, text


def relevant_links(html, base, site):
    soup = soup_for(html)
    result = []
    for a in soup.select('a[href]'):
        url = canonical(a['href'], base)
        if not url: continue
        label = folded(a.get_text(' ', strip=True) + ' ' + urlsplit(url).path)
        # External catalogue hosts need explicit user import, rather than unrestricted traversal.
        if host(url) == host(site) and (any(w in label for w in PRODUCT_WORDS) or urlsplit(url).path.lower().endswith('.pdf')):
            result.append(url)
    return list(dict.fromkeys(result))


def ocr_image(raw):
    if not shutil.which('tesseract'):
        raise ValueError('OCR needs Tesseract installed; text PDFs and spreadsheets still work.')
    with tempfile.TemporaryDirectory(prefix='cusep-ocr-') as directory:
        path = Path(directory) / 'page.png'
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image.convert('RGB').save(path)
        # eng is always present in standard Tesseract; prefer Turkish if installed.
        model_dir=Path(os.environ.get('CUSEP_TESSDATA_DIR',Path(__file__).resolve().parent.parent/'data'/'tessdata'))
        model_args=['--tessdata-dir',str(model_dir)] if (model_dir/'eng.traineddata').is_file() else []
        languages = subprocess.run(['tesseract', '--list-langs', *model_args], capture_output=True, text=True, timeout=15).stdout.splitlines()
        lang = 'tur+eng' if 'tur' in languages else 'eng'
        p = subprocess.run(['tesseract', str(path), 'stdout', '-l', lang, *model_args], capture_output=True, text=True, timeout=90)
        if p.returncode: raise ValueError('OCR failed: ' + p.stderr[-500:])
        return p.stdout


def pdf_pages(raw):
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError('Encrypted PDFs must be unlocked before import.')
    if len(reader.pages) > 100:
        raise ValueError('PDF exceeds 100 pages. Split it into smaller catalogues before import.')
    pages, warnings = [], []
    scanned = [i for i, p in enumerate(reader.pages) if len((p.extract_text() or '').strip()) < 30]
    if len(scanned) > 30:
        raise ValueError('More than 30 scanned pages; split this catalogue for OCR.')
    with tempfile.TemporaryDirectory(prefix='cusep-pdf-') as directory:
        path = Path(directory) / 'catalogue.pdf'
        path.write_bytes(raw)
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ''
            if i in scanned:
                if not shutil.which('pdftoppm') or not shutil.which('tesseract'):
                    warnings.append(f'Page {i+1}: no searchable text; install poppler and Tesseract for OCR.')
                else:
                    out = Path(directory) / 'render'
                    result = subprocess.run(['pdftoppm', '-f', str(i+1), '-l', str(i+1), '-scale-to', '2400', '-png', '-singlefile', str(path), str(out)], capture_output=True, timeout=90)
                    if result.returncode: raise ValueError(f'PDF render failed on page {i+1}')
                    text = ocr_image(out.with_suffix('.png').read_bytes())
                    warnings.append(f'Page {i+1}: OCR text, verify characters and units.')
            pages.append(text)
    return pages, warnings


def document_candidates(text):
    """Conservative candidates from explicit model/product labels or a specs-bearing page."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    specs = {}
    for line in lines:
        m = re.match(r'^([^:：]{2,70})[:：]\s*(.{1,250})$', line)
        if m: specs[m[1].strip()] = m[2].strip()
    markers = [(i, m.group(1).strip()) for i, line in enumerate(lines)
               if (m := re.match(r'^(?:product name|ürün adı|urun adi|model)\s*[:：]\s*(.{2,120})$', line, re.I))]
    if not markers:
        return []
    candidates = []
    for idx, (start, name) in enumerate(markers):
        stop = markers[idx+1][0] if idx+1 < len(markers) else len(lines)
        snippet = '\n'.join(lines[start:stop])
        local_specs = {}
        for line in lines[start:stop]:
            match = re.match(r'^([^:：]{2,70})[:：]\s*(.{1,250})$', line)
            if match: local_specs[match[1].strip()] = match[2].strip()
        candidates.append({'name': name, 'specs': local_specs, 'evidence': snippet[:9000]})
    return candidates


ALIASES = {
    'company': ('company', 'company name', 'firma', 'firma adi', 'sirket', 'sirket adi', 'name', 'unvan'),
    'website': ('website', 'web', 'web sitesi', 'url', 'site'),
    'sector': ('sector', 'sectors', 'sektor', 'faaliyet alani', 'area of expertise'),
    'description': ('description', 'aciklama', 'faaliyet', 'operation'),
    'product': ('product', 'product name', 'urun', 'urun adi'),
    'source_url': ('source url', 'source', 'kaynak', 'kaynak url'),
    'directory_url': ('directory url', 'directory source'),
    'source_sector': ('source sector', 'directory sector'),
}


def spreadsheet_rows(raw, filename):
    if filename.lower().endswith('.csv'):
        text = raw.decode('utf-8-sig')
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t') if text.strip() else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    else:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > 100_000_000:
                raise ValueError('Spreadsheet expands beyond the 100 MB limit.')
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = []
        for row in book.active.iter_rows(values_only=True):
            rows.append([str(v) if v is not None else '' for v in row])
            if len(rows) > 10001: break
        book.close()
    if not rows: raise ValueError('Spreadsheet is empty')
    if len(rows) > 10001: raise ValueError('Import up to 10,000 rows at a time.')
    headers = [key(h) for h in rows[0]]
    mapping = {field: next((headers.index(a) for a in aliases if a in headers), None) for field, aliases in ALIASES.items()}
    if mapping['company'] is None:
        raise ValueError('A company / company name / firma adı column is required.')
    for number, row in enumerate(rows[1:], 2):
        if not any(str(v).strip() for v in row): continue
        def val(field):
            i = mapping[field]
            return str(row[i]).strip() if i is not None and i < len(row) else ''
        company = val('company')
        if not company: raise ValueError(f'Row {number}: company name is missing.')
        extras = {str(rows[0][i]): str(v) for i, v in enumerate(row) if i < len(headers) and i not in [j for j in mapping.values() if j is not None] and str(v).strip()}
        specs = {}
        for header, value in extras.items():
            normalized = key(header)
            if normalized in ('specs', 'specifications'):
                try:
                    parsed = json.loads(value)
                    if not isinstance(parsed, dict): raise ValueError()
                    specs.update(parsed)
                except ValueError as exc:
                    raise ValueError(f'Row {number}: specs must be a JSON object.') from exc
            elif normalized not in ('status', 'updated at', 'last seen', 'evidence'):
                specs[header] = value
        yield {field: val(field) for field in mapping} | {'specs': specs, 'row': number}
