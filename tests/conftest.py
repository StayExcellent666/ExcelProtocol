"""Shared pytest fixtures.

Sets the env vars that config.py and dashboard_server.py expect at import
time. Without these, importing the modules raises before any test runs.
"""
import os
import sys
import tempfile
import pathlib

# Make sure tests can import project modules
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Set fake env vars BEFORE importing project modules. These satisfy the
# import-time `os.getenv(...)` reads in config.py and dashboard_server.py.
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("TWITCH_CLIENT_ID", "test-twitch-client")
os.environ.setdefault("TWITCH_CLIENT_SECRET", "test-twitch-secret")
os.environ.setdefault("EVENTSUB_SECRET", "test-eventsub-secret")
os.environ.setdefault("DISCORD_CLIENT_ID", "test-discord-client")
os.environ.setdefault("DISCORD_CLIENT_SECRET", "test-discord-clientsecret")
os.environ.setdefault("DISCORD_REDIRECT_URI", "http://localhost:8080/auth/callback")

import pytest


@pytest.fixture
def tmp_db_path():
    """A path to a fresh temp DB file. Removed after the test."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="excelprotocol-test-")
    os.close(fd)
    os.unlink(path)  # Database() will recreate
    yield path
    if os.path.exists(path):
        os.unlink(path)
    # Also clean up wal/shm sidecars
    for ext in ("-wal", "-shm"):
        side = path + ext
        if os.path.exists(side):
            os.unlink(side)


@pytest.fixture
def db(tmp_db_path):
    """A freshly-initialized Database pointing at a temp file."""
    from database import Database
    return Database(db_path=tmp_db_path)
