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

# Validate required environment variables
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")

if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise ValueError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables are required")
