"""ASGI entry point: `uvicorn dockerserver.main:app --host 127.0.0.1`."""
import logging

from .api import create_app

logging.basicConfig(level=logging.INFO)
app = create_app()
