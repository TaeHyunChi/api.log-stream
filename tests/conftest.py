import pytest

from app import create_app
from app.config import TestConfig


@pytest.fixture
def app():
    application = create_app(TestConfig())
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()
