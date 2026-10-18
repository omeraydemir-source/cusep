import pytest
from cusep.app import create_app
from cusep.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / 'test.sqlite3')


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app):
    client = app.test_client()
    client.get('/')
    return client


@pytest.fixture
def token(client):
    with client.session_transaction() as s:
        return {'X-CSRF-Token': s['csrf']}
