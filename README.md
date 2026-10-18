# Cusep — company and product research

A local web app for building an evidence-backed database from industrial directories, company websites, PDF catalogues, images, CSV, and Excel files. Python 3.12+, Flask, SQLite, and a browser; no paid API or AI key is required.

## Current Başkent OSB dataset

The live public directory at <https://www.baskentosb.org/tr/firma-listesi/> was retrieved on **18 October 2026**. All **490 company location cards** in the returned HTML were parsed into **489 normalized company names**, retaining all 490 locations. One exact normalized name appears at two addresses. Similar but differently spelled legal names remain separate; the count is not a verified count of distinct legal entities.

- 364 company records have a published website; 125 do not.
- 350 company records have an operation/manufacturing description.
- Original directory sectors, addresses, telephone numbers, operation descriptions, websites, and source URLs are preserved in location records.
- The directory has 20 populated sector labels. Companies can also be filtered by separate, unreviewed keyword-based expertise suggestions.
- The directory HTML and its original bytes are retained with a SHA-256 checksum and capture time.
- **Product records are currently empty.** Initial requests to company sites were blocked by the cloud egress proxy. The required website domains have been saved to the environment network draft. Apply that configuration before continuing company research. Directory operation descriptions are not presented as verified product specifications.

The GUI provides Excel/CSV downloads. Initial exports are in `data/exports/baskent-osb-companies.xlsx`, `data/exports/baskent-osb-companies.csv`, and `data/exports/baskent-osb-locations.csv`. The workbook has Companies, Locations, Products, and Specifications sheets, sorted by original directory sector and company name. Exports are snapshots; use **Export database** after subsequent updates.

## Run

```bash
cd /workspace/cusep
bash scripts/setup.sh
.venv/bin/python -m cusep serve --port 8000
```

Open port 8000 in your own browser on the machine running the app. The default bind address is loopback. A trusted deployment can override `--host`; this is a single-user research workspace with no multiuser authentication, and should not be exposed directly to the public Internet. The Flask server is suitable for local development. Production hosting needs a WSGI server and access control.

The data directory defaults to the repository's `data/`. Override with `CUSEP_DATA_DIR` or `--data-dir`. Application startup creates the SQLite schema and a persistent session key. Run one application process; import workers are in-process threads and are not shared between multiple WSGI processes.

## Research workflow

1. **Import company directory.** Use **Add a source → Company directory**. The Başkent OSB adapter reads the live `.gdlr-core-course-item-list` cards, including their source sector and operation description. Generic HTML table/card directories have a fallback adapter and recognized pagination support; validate their coverage separately.
2. **Review companies.** Search, filter by original directory sector or suggested expertise, inspect locations, and edit a company. Correct inaccurate website links in the source before crawling. Manual edits are protected by default; clear **Protect this record from automated updates** when you want the importer to update it again.
3. **Research company websites.** Select companies and choose **Research selected websites**, or open a company and choose **Research website**. Each company starts at its published website and follows same-domain product/catalogue links, up to 30 pages by default. Logs show blocked domains, robots restrictions, page caps, and partial results. Product JSON-LD, technical tables, definition lists and explicit specification labels create candidates. External catalogue hosts require direct upload or a reviewed source URL, not an unrestricted crawl.
4. **Import catalogues.** From a company, choose **Upload catalogue**. PDF and image imports require a company association; the original file and extracted text are saved. Explicit `Model:`, `Product name:` or `Ürün adı:` markers create candidates, with page references for PDFs. Text not recognized as a product remains searchable in **Source library**. Add products manually using the source URL or uploaded source identifier when necessary.
5. **Review products.** Verify that the company actually manufactures the item, rather than distributing it or showing a customer project. Check numbers, units and material grades. Edit specifications as a JSON object and mark the product reviewed or rejected. Missing specifications remain missing; no data is invented.
6. **Refresh whenever needed.** Reimport the directory, rerun selected website crawls, or upload a revised file. Stable source URLs, company names, product names and upload filenames avoid repeat-run duplicates. Activity records results and failures. Cancellation takes effect between requests/document pages; completed records remain saved.
7. **Export and back up.** Excel includes companies, every directory location, products and one row per specification. CSV is UTF-8 with BOM. SQLite backup includes source versions and original file bytes, and uses SQLite's backup API so WAL data is included consistently.

## Spreadsheet format

`cusep/static/import-template.csv` contains the headers:

```csv
company,website,sector,description,product,source_url,Capacity,Material
```

`company` is required on every populated row. `website`, `sector`, `description`, `product`, and `source_url` are optional. Semicolons separate sector labels. Extra columns become specification names/values when a product is present. A `specs` column containing a JSON object is also supported, including product CSV exports. Turkish aliases such as `firma adı`, `web sitesi`, `sektör`, and `ürün adı` are recognized. CSV must be UTF-8; Excel must be `.xlsx`. Excel import reads the **first worksheet**. To import another sheet, save it as a separate workbook/CSV. The database backup is the complete restore format, not an Excel round-trip.

For updates, keep the **same original source URL** or **same upload filename**. With no original URL, upload identity is based on the filename (and the associated company for catalogues). A changed file under that identity creates a new document version, while matching product candidates update. Renaming the source or product may create a new record. Company matching uses normalized names, not tax IDs; review ambiguous names and name changes manually. Stale/missing source records are retained with last-seen dates rather than deleted automatically. Uploading different sources under the same filename can merge their refresh identity, so use distinct descriptive filenames.

Excel/CSV exports escape formula-leading text. Excel cells are limited to 32,000 characters for compatibility and illegal spreadsheet control characters are removed from exports. Full evidence and original bytes remain in SQLite.

## OCR and supported limits

Text PDFs work without external executables. Scanned PDFs require **Poppler** (`pdftoppm`) and **Tesseract**; image OCR requires Tesseract. They are installed in this cloud instance. The setup script also prepares a checksum-verified, pinned official Turkish OCR model alongside the system English model in `data/tessdata/`. OCR prefers `tur+eng` if both language models are available, otherwise English; Turkish diacritics and all measurements require review. Set `CUSEP_TESSDATA_DIR` to a directory containing your Tesseract language models if needed.

Limits: 20 MB/file or download; 100 PDF pages; up to 30 OCR pages per PDF; 10,000 spreadsheet rows; 100 directory pages; 30 website pages per company through the GUI (API accepts 1–200). Split larger catalogues. Images are treated as a single page. Drawings, visual-only specifications, charts, complex layouts, handwritten text and unlabelled catalogue products may require manual entry. The app retains evidence and does not claim automatic extraction of every product from arbitrary websites or layouts.

## Networking and credentials

The setup script needs `raw.githubusercontent.com` for its pinned Turkish model (covered by the package-manager network preset). The directory needs `baskentosb.org` and `www.baskentosb.org`. Company crawling needs the hostname in each published website plus its bare/`www` redirect counterpart. The initial 353 distinct published website hosts and their counterparts were added to the network configuration draft (706 custom domain entries including Başkent OSB). No broad `*` access is required. Other redirects and catalogue storage hosts can need additional explicit destinations, which appear in Activity errors. Saving a draft does not prove the current runtime can access those destinations; retry after applying environment settings.

Crawling identifies itself as `CusepResearch/1.0`, observes robots.txt and crawl delay, follows at most five redirects, checks redirect destinations, preserves TLS verification and uses bounded downloads. A robots lookup failure stops the crawl, except for 404/410. It does not bypass logins, CAPTCHAs or robots restrictions. Private/loopback IPs and local hostnames are blocked. Where local DNS is unavailable, the app uses the configured cloud egress proxy for DNS; that proxy must enforce destination/network policy. No website credentials are required or requested for the public directory.

## Database and architecture

- `cusep/db.py`: SQLite schema, upserts, source versions, stable identity, timestamps, protected edits.
- `cusep/extract.py`: source adapters, classification suggestions, structured products/specifications, PDF/OCR and spreadsheet readers.
- `cusep/crawl.py`: network policy, robots, crawl budgets, background jobs and import orchestration.
- `cusep/app.py`: API, exports, CSRF protection, source/file inspection and health check.
- `cusep/static/` and `templates/`: responsive GUI; no frontend build or external CDN needed.
- `tests/`: synthetic pipeline/API checks and small public live-layout excerpts. Test data runs in isolated temporary databases.

Tables: `companies`, `listings`, `products`, `documents`, and `jobs`. Specifications are JSON objects preserving original values and units. Products reference a company and optional captured document. Document versions retain extracted text, original bytes, hashes and capture dates. Jobs interrupted by a process restart are marked interrupted; rerun them to resume collection through idempotent upserts. There is no unattended scheduler; GUI imports and CLI commands run on demand.

For a consistent backup, use **Export database → Complete database backup**. To restore, stop the app, preserve the current `data/` directory, put the backup at `data/cusep.sqlite3`, and move aside any old `cusep.sqlite3-wal` and `cusep.sqlite3-shm` files before restarting. Keep your session key private. Dependencies and local data are ignored by Git; source code and tests are in the checkout. Each cloud task is already isolated: use the existing checkout, and do not create a Git worktree unless explicitly requested.

## Validation

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
node --check cusep/static/app.js
```

Backend tests exercise source-layout parsing, grouped locations, repeated/revised imports, pagination, partial failures, source versions, edit protection, Turkish search, specification units, text PDFs, real OCR when installed, network restrictions/robots/redirects, CSRF, exports and backup restoration.

The browser smoke test starts a temporary server and database, imports synthetic records, reviews products, verifies edits survive reimport, searches records, downloads original files and Excel, and checks mobile overflow and JavaScript errors. It needs Playwright in the invoking Python and an installed Chromium browser; neither a browser download nor a frontend build is required here:

```bash
python tests/browser_smoke.py --python .venv/bin/python --browser /usr/bin/chromium
```

The initial directory was validated separately against all 490 live source cards. Company-site product collection remains blocked by egress policy until the saved domain settings are applied. Automated classification, manufacturer attribution, OCR, and source completeness remain review tasks.
