"""Install a pinned official Turkish model alongside the system English model."""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

import requests

ROOT = Path(__file__).resolve().parent.parent / 'data' / 'tessdata'
COMMIT = '87416418657359cb625c412a48b6e1d6d41c29bd'
EXPECTED = '7393381111e1152420fc4092cb44eef4237580d21b92bf30d7d221aad192c6b7'


def main():
    if not shutil.which('tesseract'):
        print('Tesseract is unavailable; install it to enable image/scanned-PDF OCR.')
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    model = ROOT / 'tur.traineddata'
    if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != EXPECTED:
        response = requests.get(f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{COMMIT}/tur.traineddata', timeout=60)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != EXPECTED:
            raise RuntimeError('Turkish OCR model checksum mismatch; refusing to use the download.')
        temporary = model.with_suffix('.download')
        temporary.write_bytes(response.content)
        temporary.replace(model)
    result = subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,check=True)
    match = re.search(r'in "([^"]+)"', result.stdout + result.stderr)
    system_model = Path(match[1]) / 'eng.traineddata' if match else None
    if not system_model or not system_model.is_file():
        raise RuntimeError('Install the system English Tesseract language model before preparing OCR.')
    shutil.copyfile(system_model,ROOT / 'eng.traineddata')
    print('Turkish and English OCR models prepared; Turkish SHA-256 verified.')


if __name__ == '__main__': main()
