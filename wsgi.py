"""gunicorn 진입점."""

from app import create_app

app = create_app()
