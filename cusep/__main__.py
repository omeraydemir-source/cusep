import argparse
import json
import os
from pathlib import Path

from .app import DEFAULT_SOURCE, create_app
from .crawl import import_directory


def main():
    parser = argparse.ArgumentParser(description='Cusep company and product research database')
    parser.add_argument('command', choices=['serve', 'init', 'import-directory'], nargs='?', default='serve')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--url', default=DEFAULT_SOURCE)
    parser.add_argument('--data-dir', default=os.environ.get('CUSEP_DATA_DIR'))
    args = parser.parse_args()
    app = create_app(args.data_dir)
    if args.command == 'init':
        print('Database initialized:', app.extensions['db'].path)
    elif args.command == 'import-directory':
        print(import_directory(app.extensions['db'], args.url, print))
    else:
        app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == '__main__': main()
