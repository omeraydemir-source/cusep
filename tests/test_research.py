"""Synthetic fixtures exercise pipelines; these are not Başkent OSB company data."""
import io
import json
import shutil
import sqlite3
import time
from unittest.mock import Mock

import pytest
from openpyxl import Workbook, load_workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from cusep import crawl
from cusep.crawl import FetchError, Fetcher, crawl_company, import_catalogue, import_directory, validate_url
from cusep.extract import (canonical, classify, directory_entries, document_candidates,
                           ocr_image, pdf_pages, product_page, spreadsheet_rows)

DIRECTORY = '''<main><table><tr><th>Company</th></tr>
<tr><td>TEST METAL SANAYİ LTD.</td><td><a href="https://manufacturer.example/">Web</a></td></tr>
<tr><td><a href="/tr/firma/test-makine/">TEST MAKİNE SANAYİ LTD.</a></td></tr>
</table><a href="?page=2">2</a></main>'''
PRODUCT = '''<main><h1>Hydraulic press P100</h1><table>
<tr><th>Capacity</th><td>100 t</td></tr><tr><td>Power</td><td>15 kW</td></tr>
</table></main>'''


def pdf_fixture(text='Model: TEST-P100\nCapacity: 100 t\nMaterial: Steel'):
    writer = PdfWriter(); page = writer.add_blank_page(600, 800)
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    commands = 'BT /F1 16 Tf 40 740 Td 22 TL ' + ' T* '.join('(' + line + ') Tj' for line in text.splitlines()) + ' ET'
    stream=DecodedStreamObject();stream.set_data(commands.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output=io.BytesIO();writer.write(output);return output.getvalue()


class FakeFetcher:
    def __init__(self, pages): self.pages=pages;self.seen=[]
    def get(self,url):
        self.seen.append(url)
        value=self.pages[url]
        if isinstance(value,Exception): raise value
        return value.encode(), 'text/html', url, 'utf-8'


def wait_job(app,jid):
    for _ in range(500):
        row=app.extensions['db'].query('SELECT * FROM jobs WHERE id=?',(jid,))[0]
        if row['state'] not in ('queued','running'):return row
        time.sleep(.01)
    raise AssertionError('Job did not complete')


def test_classification_turkish_and_word_boundaries():
    sectors,evidence=classify('TEST ÇELİK MAKİNE SANAYİ')
    assert 'Metal & fabrication' in sectors and 'Machinery & equipment' in sectors
    assert 'çelik' in evidence
    assert not classify('email marketing')[0]


def test_directory_adapter_and_pagination():
    rows,pages=directory_entries(DIRECTORY,'https://directory.example/tr/firma-listesi/')
    assert len(rows)==2
    assert rows[0]['website']=='https://manufacturer.example/'
    assert rows[1]['directory_url'].endswith('/firma/test-makine/')
    assert pages==['https://directory.example/tr/firma-listesi/?page=2']


def test_directory_refresh_profiles_and_dedup(db):
    url='https://directory.example/tr/firma-listesi/'
    f=FakeFetcher({url:DIRECTORY,url+'?page=2':'<p>End</p>',
      'https://directory.example/tr/firma/test-makine/':'<main><a href="https://machine.example/">Website</a><p>Makine imalatı</p></main>'})
    assert '2 company candidates' in import_directory(db,url,lambda _:None,f)
    import_directory(db,url,lambda _:None,f)
    assert len(db.query('SELECT * FROM companies'))==2
    assert len(db.query('SELECT * FROM documents'))==3
    assert db.query("SELECT website FROM companies WHERE name LIKE 'TEST MAK%'")[0]['website']=='https://machine.example/'


def test_empty_directory_is_failure(db):
    with pytest.raises(ValueError,match='No company records'):
        import_directory(db,'https://directory.example/',lambda _:None,FakeFetcher({'https://directory.example/':'<main>JS required</main>'}))
    assert not db.query('SELECT * FROM companies')


def test_partial_directory_is_not_success(db):
    url='https://directory.example/tr/firma-listesi/'
    f=FakeFetcher({url:DIRECTORY,url+'?page=2':FetchError('HTTP 403 from source'),
      'https://directory.example/tr/firma/test-makine/':FetchError('HTTP 403 from source')})
    with pytest.raises(ValueError,match='Partial directory import'):
        import_directory(db,url,lambda _:None,f)
    assert len(db.query('SELECT * FROM companies'))==2


def test_product_specs_preserve_units():
    products,text=product_page(PRODUCT,'https://manufacturer.example/products/p100')
    assert products[0]['specs']=={'Capacity':'100 t','Power':'15 kW'}
    assert '100 t' in products[0]['evidence']
    assert not product_page(PRODUCT.replace('Hydraulic press P100','Products'),'https://manufacturer.example/products/')[0]


def test_jsonld_products():
    html='<script type="application/ld+json">'+json.dumps({'@graph':[{'@type':'Product','name':'TEST-PRESS','model':'P1','additionalProperty':[{'name':'Weight','value':120,'unitText':'kg'}]}]})+'</script>'
    rows,_=product_page(html,'https://manufacturer.example/p1')
    assert rows[0]['specs']['Weight']=='120 kg'
    assert rows[0]['name']=='TEST-PRESS'


def test_company_crawl_dedup_and_document_versions(db):
    cid=db.company('TEST LTD.',website='https://manufacturer.example/')
    f=FakeFetcher({'https://manufacturer.example/':'<a href="/products/p100">Product</a>',
      'https://manufacturer.example/products/p100':PRODUCT})
    crawl_company(db,cid,lambda _:None,f)
    crawl_company(db,cid,lambda _:None,f)
    assert len(db.query('SELECT * FROM products'))==1
    f.pages['https://manufacturer.example/products/p100']=PRODUCT.replace('100 t','120 t')
    crawl_company(db,cid,lambda _:None,f)
    assert len(db.query('SELECT * FROM documents'))==3
    p=db.query('SELECT * FROM products')[0]
    assert json.loads(p['specs'])['Capacity']=='120 t' and p['status']=='needs_review'


def test_reviewed_changes_are_protected(db):
    cid=db.company('TEST LTD.',website='https://manufacturer.example/')
    pid=db.product(cid,'P1',{'Power':'10 kW'},'https://manufacturer.example/p1','old')
    db.execute("UPDATE companies SET name='Reviewed TEST LTD.',locked=1,status='reviewed' WHERE id=?",(cid,))
    db.execute("UPDATE products SET locked=1,status='reviewed' WHERE id=?",(pid,))
    assert db.company('TEST LTD.',website='https://changed.example/')==cid
    db.product(cid,'P1',{'Power':'20 kW'},'https://manufacturer.example/p1','new')
    assert db.query('SELECT name FROM companies')[0]['name']=='Reviewed TEST LTD.'
    assert json.loads(db.query('SELECT specs FROM products')[0]['specs'])['Power']=='10 kW'


def test_catalogue_text_and_multiple_products(db):
    cid=db.company('TEST LTD.')
    raw=pdf_fixture('Model: TEST-P100\nCapacity: 100 t\nModel: TEST-P200\nCapacity: 200 t')
    pages,warnings=pdf_pages(raw)
    assert '100 t' in pages[0] and not warnings
    count,_=import_catalogue(db,raw,'https://manufacturer.example/catalogue.pdf','Test catalogue',cid,lambda _:None)
    assert count==2
    rows=db.query('SELECT * FROM products ORDER BY id')
    assert json.loads(rows[0]['specs'])['Capacity']=='100 t'
    assert json.loads(rows[1]['specs'])['Capacity']=='200 t'
    assert rows[0]['source_url'].endswith('#page=1')
    assert db.query('SELECT raw FROM documents')[0]['raw']==raw
    assert not document_candidates('Some marketing about the company.\nContact: hello@example.com')


def test_spreadsheet_turkish_headers_and_specs():
    raw='firma adı;web sitesi;sektör;ürün adı;Ağırlık\nTEST LTD.;https://manufacturer.example;Metal;TEST P1;12 kg\n'.encode()
    rows=list(spreadsheet_rows(raw,'test.csv'))
    assert rows[0]['company']=='TEST LTD.' and rows[0]['specs']['Ağırlık']=='12 kg'
    book=Workbook();book.active.append(['company','product','Power']);book.active.append(['TEST LTD.','P1','12 kW'])
    stream=io.BytesIO();book.save(stream)
    assert list(spreadsheet_rows(stream.getvalue(),'test.xlsx'))[0]['specs']['Power']=='12 kW'


def test_missing_spreadsheet_company_is_rejected():
    with pytest.raises(ValueError,match='Row 3'):
        list(spreadsheet_rows(b'company,product\nTEST LTD.,P1\n,P2\n','test.csv'))


@pytest.mark.parametrize('url',['http://127.0.0.1/','http://169.254.169.254/','http://[::1]/','file:///etc/passwd','https://name:secret@example.com/'])
def test_private_and_invalid_urls_blocked(url):
    with pytest.raises(ValueError):validate_url(url)


def test_robot_disallow_and_network_error(monkeypatch):
    f=Fetcher(delay=0)
    monkeypatch.setattr(f,'_request',lambda url:(b'User-agent: *\nDisallow: /private', 'text/plain',url,'utf-8'))
    with pytest.raises(FetchError,match='robots.txt disallows'):f.allowed('https://manufacturer.example/private')
    f=Fetcher(delay=0)
    monkeypatch.setattr(f,'_request',Mock(side_effect=FetchError('HTTP 403 from source')))
    with pytest.raises(FetchError,match='Could not verify robots'):f.allowed('https://manufacturer.example/')


def test_redirect_checked_before_download(monkeypatch):
    f=Fetcher(delay=0)
    monkeypatch.setattr(crawl,'validate_url',lambda url:url)
    permitted=[]
    def allowed(url):
        permitted.append(url)
        if 'blocked' in url:raise FetchError('robots.txt disallows target')
    monkeypatch.setattr(f,'allowed',allowed)
    response=Mock();response.status_code=302;response.headers={'Location':'https://blocked.example/'}
    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    monkeypatch.setattr(f.session,'get',Mock(return_value=response))
    with pytest.raises(FetchError,match='robots.txt disallows target'):f.get('https://manufacturer.example/')
    assert f.session.get.call_count==1


def test_csrf_and_roundtrip_edit(client,token):
    assert client.post('/api/companies',json={'name':'TEST'}).status_code==403
    response=client.post('/api/companies',json={'name':'TEST ÇELİK LTD.','website':'https://manufacturer.example/'},headers=token)
    cid=response.json['id']
    edited=client.patch(f'/api/companies/{cid}',json={'name':'TEST ÇELİK LTD.','website':'https://manufacturer.example/','sectors':['Metal & fabrication'],'status':'reviewed'},headers=token)
    assert edited.status_code==200
    assert client.get('/api/companies?q=celik').json['total']==1
    assert client.get('/api/companies?sector=Metal%20%26%20fabrication').json['total']==1
    assert client.get('/api/companies?status=needs_review').json['total']==0


def test_file_job_validation_dedup_and_download(client,token,app):
    raw=b'company,product,Capacity\nTEST LTD.,TEST P1,100 t\n'
    for _ in range(2):
        r=client.post('/api/import/file',data={'file':(io.BytesIO(raw),'test.csv')},headers=token)
        assert r.status_code==200
        job=wait_job(app,r.json['job_id'])
        assert job['state']=='completed',job
    assert client.get('/api/stats').json['companies']==1
    assert client.get('/api/stats').json['products']==1
    document=client.get('/api/documents').json['items'][0]
    assert client.get(f'/api/documents/{document["id"]}/file').data==raw
    assert client.get(f'/api/documents/{document["id"]}').json['sha256']


def test_pdf_upload_and_manual_product_from_document(client,token,app):
    cid=client.post('/api/companies',json={'name':'TEST LTD.'},headers=token).json['id']
    r=client.post('/api/import/file',data={'company_id':str(cid),'file':(io.BytesIO(pdf_fixture()),'test.pdf')},headers=token)
    assert wait_job(app,r.json['job_id'])['state']=='completed'
    doc=client.get('/api/documents').json['items'][0]
    p=client.post('/api/products',json={'company_id':cid,'name':'Manually captured model','specs':{'Weight':'120 kg'},'source_url':doc['source_url']},headers=token)
    assert p.status_code==201,p.json
    assert client.get('/api/products').json['total']==2


def test_export_formulas_and_consistent_backup(client,token,app):
    cid=app.extensions['db'].company('=HYPERLINK("bad")')
    app.extensions['db'].product(cid,'P1',{'Weight':'12 kg'},'https://manufacturer.example/p1','Test evidence')
    exported=client.get('/api/export/xlsx')
    book=load_workbook(io.BytesIO(exported.data))
    assert book['Companies']['A2'].value.startswith("'=")
    assert book['Specifications']['D2'].value=='12 kg'
    assert client.get('/api/export/companies.csv').data.startswith(b'\xef\xbb\xbf')
    raw=client.get('/api/export/backup').data
    with sqlite3.connect(':memory:') as connection:
        connection.deserialize(raw)
        assert connection.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1
    assert client.get('/health').json['status']=='ok'


def test_ocr_image_real_engine():
    if not shutil.which('tesseract'):pytest.skip('Tesseract not installed')
    image=Image.new('RGB',(1300,350),'white');draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',44)
    draw.multiline_text((35,35),'Model: TEST-P100\nCapacity: 100 t',font=font,fill='black',spacing=20)
    stream=io.BytesIO();image.save(stream,format='PNG')
    text=ocr_image(stream.getvalue())
    assert '100 t' in text
    assert document_candidates(text)[0]['name']=='TEST-P100'


def test_managed_proxy_can_resolve_target_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror('DNS is delegated')))
    monkeypatch.setattr(crawl.requests.utils,'get_environ_proxies',lambda _: {'https':'http://managed-proxy.example:8080'})
    assert validate_url('https://manufacturer.example/')=='https://manufacturer.example/'
    monkeypatch.setattr(crawl.requests.utils,'get_environ_proxies',lambda _: {})
    with pytest.raises(FetchError,match='Cannot resolve'):validate_url('https://manufacturer.example/')


def test_revised_upload_updates_product_instead_of_duplicating(client,token,app):
    for capacity in ('100 t','120 t'):
        raw=f'company,product,Capacity\nTEST LTD.,TEST P1,{capacity}\n'.encode()
        r=client.post('/api/import/file',data={'file':(io.BytesIO(raw),'same-source.csv')},headers=token)
        assert wait_job(app,r.json['job_id'])['state']=='completed'
    products=client.get('/api/products').json['items']
    assert len(products)==1 and json.loads(products[0]['specs'])['Capacity']=='120 t'
    assert len(client.get('/api/documents').json['items'])==2


def test_products_csv_reimport_keeps_specifications(client,token,app):
    cid=app.extensions['db'].company('TEST LTD.')
    app.extensions['db'].product(cid,'P1',{'Capacity':'100 t'},'https://manufacturer.example/p1','Evidence')
    raw=client.get('/api/export/products.csv').data
    r=client.post('/api/import/file',data={'file':(io.BytesIO(raw),'products.csv')},headers=token)
    assert wait_job(app,r.json['job_id'])['state']=='completed'
    products=client.get('/api/products').json['items']
    assert len(products)==1 and json.loads(products[0]['specs'])=={'Capacity':'100 t'}


def test_baskent_live_layout_preserves_duplicate_locations_and_source_sector(db):
    from pathlib import Path
    html=Path('tests/fixtures/baskent-sample.html').read_text()
    rows,pages=directory_entries(html,'https://www.baskentosb.org/tr/firma-listesi/')
    assert len(rows)==4 and sum(len(r['listings']) for r in rows)==5 and pages==[]
    first=rows[0]
    assert first['name']=='3DTİM ELEKTRONİK A.Ş.'
    assert first['source_sector']=='Metalurji ve Makina Sanayi'
    assert first['description']=='Depo Otomasyon Sistemleri İmalatı'
    bergamo=next(r for r in rows if r['name'].startswith('BERGAMO'))
    assert len(bergamo['listings'])==2
    assert len({l['address'] for l in bergamo['listings']})==2
    url='https://www.baskentosb.org/tr/firma-listesi/'
    import_directory(db,url,lambda _:None,FakeFetcher({url:html}))
    import_directory(db,url,lambda _:None,FakeFetcher({url:html}))
    assert len(db.query('SELECT * FROM companies'))==4
    assert len(db.query('SELECT * FROM listings'))==5


def test_split_multiple_source_websites_and_idn():
    from cusep.extract import baskent_entries,soup_for
    html='''<div class="gdlr-core-course-item-list"><h3 class="gdlr-core-course-item-title">TEST</h3>
    <div class="gdlr-core-course-item-info"><i class="fa fa-globe"></i><span class="gdlr-core-tail"><a href="https://first.example; www.second.example">web</a></span></div></div>'''
    rows,_=baskent_entries(soup_for(html),'https://www.baskentosb.org/tr/firma-listesi/')
    assert rows[0]['website']=='https://first.example/'
    assert not canonical('https://first.example; www.second.example')
    assert canonical('https://www.abaygıda.com.tr/')=='https://www.xn--abaygda-vfb.com.tr/'


def test_source_sector_filters_and_location_export(client,app):
    db=app.extensions['db']
    cid=db.company('TEST LTD.',source_sector='Savunma')
    db.listing(cid,'https://directory.example/',{'name':'TEST LTD.','address':'Test address','source_sector':'Savunma','operations':'Test operation'})
    assert client.get('/api/companies?sector=source:Savunma').json['total']==1
    assert client.get('/api/companies?sector=source:GIDA').json['total']==0
    book=load_workbook(io.BytesIO(client.get('/api/export/xlsx').data))
    assert book['Locations']['B2'].value=='Test address'
    assert client.get(f'/api/companies/{cid}').json['listings'][0]['operations']=='Test operation'
