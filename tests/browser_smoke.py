"""Real browser smoke test in an isolated temporary database.
Run with a Python that has playwright, and point --python at the app's venv.
"""
import argparse
import io
import json
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--python',default='.venv/bin/python')
    parser.add_argument('--browser',default='/usr/bin/chromium')
    parser.add_argument('--artifacts',default='/tmp/cusep-browser-artifacts')
    args=parser.parse_args()
    artifacts=Path(args.artifacts);artifacts.mkdir(parents=True,exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='cusep-browser-') as data_dir:
        server=subprocess.Popen([args.python,'-m','cusep','serve','--port',str(port),'--data-dir',data_dir],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base+'/health',timeout=1) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise AssertionError('App did not start')
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1050})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base)
                expect(page.get_by_role('heading',name='Build your company directory')).to_be_visible()
                page.screenshot(path=str(artifacts/'empty.png'),full_page=True)
                csv=b'company,website,sector,product,source_url,Capacity\nTEST METAL LTD.,https://manufacturer.example/,Metal & fabrication,TEST PRESS,https://manufacturer.example/products/press,100 t\nTEST MACHINE LTD.,https://machine.example/,Machinery & equipment,,,\n'
                def upload():
                    page.locator('#import-button').click()
                    page.get_by_role('button',name='Catalogue / spreadsheet',exact=True).click()
                    page.locator('input[name="file"]').set_input_files({'name':'test.csv','mimeType':'text/csv','buffer':csv})
                    page.get_by_role('button',name='Upload & extract →').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    for _ in range(100):
                        jobs=page.request.get(base+'/api/jobs').json()['items']
                        if jobs and jobs[0]['state'] not in ('queued','running'):
                            assert jobs[0]['state']=='completed',jobs[0];break
                        time.sleep(.05)
                    else:raise AssertionError('Upload did not complete')
                    page.reload()
                upload()
                expect(page.locator('#stat-companies')).to_have_text('2')
                expect(page.locator('#stat-products')).to_have_text('1')
                page.get_by_role('button',name='TEST METAL LTD.',exact=True).click()
                page.get_by_role('button',name='Edit & review').click()
                page.locator('input[name="name"]').fill('TEST METAL REVIEWED LTD.')
                page.locator('select[name="status"]').select_option('reviewed')
                page.get_by_role('button',name='Save company',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                upload()
                expect(page.get_by_role('button',name='TEST METAL REVIEWED LTD.',exact=True)).to_be_visible()
                expect(page.locator('#stat-companies')).to_have_text('2')
                expect(page.locator('#stat-products')).to_have_text('1')
                page.locator('#search').fill('MACHINE')
                expect(page.locator('#total-pill')).to_have_text('1')
                expect(page.get_by_role('button',name='TEST MACHINE LTD.',exact=True)).to_be_visible()
                page.locator('#search').fill('')
                page.locator('.nav[data-view="products"]').click()
                page.get_by_role('button',name='TEST PRESS',exact=True).click()
                expect(page.locator('textarea[name="specs"]')).to_have_value('{\n  "Capacity": "100 t"\n}')
                page.locator('select[name="status"]').select_option('reviewed')
                page.get_by_role('button',name='Save review',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                page.locator('.nav[data-view="sources"]').click()
                page.get_by_role('button',name='test.csv',exact=True).click()
                expect(page.locator('.source-text')).to_contain_text('100 t')
                with page.expect_download() as download:
                    page.get_by_role('link',name='↓ Download original file').click()
                assert Path(download.value.path()).read_bytes()==csv
                page.get_by_role('button',name='Close dialog').click()
                page.locator('#export-button').click()
                with page.expect_download() as download:
                    page.get_by_role('link',name='↓ Excel workbook',exact=False).click()
                assert download.value.suggested_filename=='cusep-database.xlsx'
                assert Path(download.value.path()).read_bytes().startswith(b'PK')
                page.get_by_role('button',name='Close dialog').click()
                page.locator('.nav[data-view="companies"]').click()
                expect(page.locator('#total-pill')).to_have_text('2')
                page.screenshot(path=str(artifacts/'companies-test-data.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert not page.evaluate('document.body.scrollWidth > window.innerWidth')
                page.screenshot(path=str(artifacts/'mobile-test-data.png'),full_page=True)
                assert not errors,errors
                browser.close()
                print('Browser smoke passed: CSV import, repeat import, protected edits, search, product review, original-file download, Excel export, mobile layout; no JavaScript errors.')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__=='__main__':main()
