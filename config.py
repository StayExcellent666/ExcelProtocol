import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Discord Bot Token
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Twitch API Credentials
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')

# Bot Settings
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '90'))  # Default: 90 seconds (1.5 minutes)
BOT_OWNER_ID = int(os.getenv('BOT_OWNER_ID', '0'))  # Your Discord user ID for owner-only commands

# Twitch Chat Bot (twitchio)
# Create a separate Twitch account for the bot, then get its OAuth token at:
# https://twitchapps.com/tmi/
TWITCH_BOT_USERNAME = os.getenv('TWITCH_BOT_USERNAME')   # e.g. "ExcelProtocolBot"
TWITCH_BOT_TOKEN = os.getenv('TWITCH_BOT_TOKEN')         # oauth:xxxxxxxxxxxxxxxxxxxxxxxxx

# Validate required environment variables
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")

if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise ValueError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables are required")
