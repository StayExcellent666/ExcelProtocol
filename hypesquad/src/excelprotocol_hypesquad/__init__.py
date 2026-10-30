"""Standalone ExcelProtocol Hypesquad service.

Nothing in the main ExcelProtocol bot imports this package yet.  The public
surface is intentionally small so the eventual Discord adapter can be added
without moving the domain or persistence code into the bot process.
"""

from .config import Config
from .database import Database
from .service import HypeError, HypeSquadService

__all__ = ["Config", "Database", "HypeError", "HypeSquadService"]

