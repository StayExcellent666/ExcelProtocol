# Getting Started with Twitch Notifier Bot

Welcome! This guide will get you from zero to a fully functional Discord bot that monitors Twitch streamers.

## What You're Building

A Discord bot that:
- ✅ Monitors Twitch streamers across multiple servers
- ✅ Posts rich notifications when they go live
- ✅ Uses slash commands for easy management
- ✅ Runs 24/7 on Fly.io (free tier)
- ✅ Stores data in SQLite database

## Quick Start (5 Minutes)

### 1. Get Your Credentials

**Discord Bot Token:**
1. Visit https://discord.com/developers/applications
2. Create a new application
3. Go to Bot → Reset Token → Copy it
4. Enable "Server Members Intent" and "Message Content Intent"

**Twitch API Credentials:**
1. Visit https://dev.twitch.tv/console/apps
2. Register Your Application
3. OAuth Redirect URL: `http://localhost`
4. Copy your Client ID and Client Secret

### 2. Set Up the Project

```bash
# Clone or download the project
cd twitch-notifier-bot

# Run the quick start script
./quickstart.sh

# Or manually:
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

### 3. Run Locally

```bash
python bot.py
```

You should see:
```
INFO - Logged in as YourBot (ID: ...)
INFO - Command tree synced
INFO - Stream checking loop started
```

### 4. Test It

In Discord:
```
/addstreamer shroud
/live
```

### 5. Deploy to Fly.io

```bash
fly auth login
fly apps create your-bot-name
fly volumes create twitch_bot_data --size 1
fly secrets set DISCORD_TOKEN="your_token"
fly secrets set TWITCH_CLIENT_ID="your_id"
fly secrets set TWITCH_CLIENT_SECRET="your_secret"
fly deploy
```

Done! Your bot is now running 24/7.

## Project Structure

```
twitch-notifier-bot/
├── bot.py              # Main bot file - slash commands & polling
├── database.py         # SQLite operations
├── twitch_api.py       # Twitch API integration
├── config.py           # Configuration loader
│
├── README.md           # Full documentation
├── TESTING.md          # Testing guide
├── DEPLOYMENT.md       # Fly.io deployment
├── ARCHITECTURE.md     # Technical overview
│
├── requirements.txt    # Python dependencies
├── .env.example        # Template for credentials
├── fly.toml           # Fly.io configuration
└── Procfile           # Process definition
```

## How It Works

### 1. User adds a streamer
```
/addstreamer xqc
   ↓
Bot verifies "xqc" exists on Twitch
   ↓
Saves to database with guild ID and notification channel
```

### 2. Polling loop checks every 90 seconds
```
Get all unique streamers from database
   ↓
Batch request to Twitch API (up to 100 streamers)
   ↓
For each live stream:
  - Check if already notified
  - Find all guilds monitoring this streamer
  - Send notification embeds
  - Mark as notified
```

### 3. Notification sent
```
Rich embed with:
- Stream title
- Game/category
- Viewer count
- Thumbnail
- Direct link to stream
```

## Key Files Explained

### bot.py
The heart of the bot. Contains:
- `TwitchNotifierBot` class - Main bot logic
- Slash commands (`/addstreamer`, `/removestreamer`, etc.)
- `check_streams()` - The polling loop that runs every 90s
- `send_notification()` - Creates and sends Discord embeds

**Key Section:**
```python
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_streams(self):
    """This runs every 90 seconds"""
    streamers = self.db.get_all_streamers()
    live_streams = await self.twitch.get_live_streams(streamer_names)
    # ... send notifications
```

### database.py
Simple SQLite wrapper. Contains:
- `add_streamer()` - Save a new streamer to monitor
- `get_all_streamers()` - Get everyone we're monitoring
- `get_server_streamers()` - Get streamers for one guild
- Database schema with two tables

**Key Tables:**
```sql
-- Stores which streamers each guild monitors
monitored_streamers(guild_id, streamer_name, channel_id)

-- Stores notification channel per guild  
server_settings(guild_id, notification_channel_id)
```

### twitch_api.py
Handles all Twitch communication. Contains:
- `get_app_access_token()` - OAuth authentication
- `get_user()` - Verify a streamer exists
- `get_live_streams()` - Batch check up to 100 streamers
- Automatic token refresh

**Key Feature:**
```python
async def get_live_streams(self, usernames: List[str]):
    """Check up to 100 streamers in one API call"""
    # Returns list of currently live streams
```

### config.py
Loads environment variables and validates them:
```python
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '90'))
```

## Commands Reference

| Command | Description | Permission |
|---------|-------------|------------|
| `/addstreamer <username>` | Start monitoring a streamer | Manage Server |
| `/removestreamer <username>` | Stop monitoring a streamer | Manage Server |
| `/streamers` | List all monitored streamers | Everyone |
| `/setchannel <channel>` | Set notification channel | Manage Server |
| `/live` | Check who's live right now | Everyone |

## Configuration Options

In `.env` file:

```bash
# Required
DISCORD_TOKEN=your_discord_bot_token
TWITCH_CLIENT_ID=your_twitch_client_id
TWITCH_CLIENT_SECRET=your_twitch_client_secret

# Optional (defaults shown)
CHECK_INTERVAL_SECONDS=90  # How often to check (60-120 recommended)
```

## Database Schema

### monitored_streamers
```
id               INTEGER PRIMARY KEY
guild_id         INTEGER (Discord server ID)
streamer_name    TEXT (lowercase Twitch username)
channel_id       INTEGER (Discord channel ID for notifications)
added_at         TIMESTAMP
```

### server_settings
```
guild_id                INTEGER PRIMARY KEY
notification_channel_id INTEGER
created_at              TIMESTAMP
```

## API Usage

### Twitch API
- **Endpoint:** `https://api.twitch.tv/helix/streams`
- **Rate Limit:** 800 requests/minute
- **Our Usage:** ~40 requests/hour (well within limits)
- **Batch Size:** Up to 100 streamers per request

### Discord API
- **Slash Commands:** Uses Discord's interaction API
- **Embeds:** Rich message formatting
- **Rate Limits:** Generous, no issues expected

## Common Workflows

### Adding Your First Streamer
```
1. User: /addstreamer ninja
2. Bot: Checks if "ninja" exists on Twitch ✓
3. Bot: Saves to database
4. Bot: Confirms with user
5. Next polling loop: Includes "ninja" in batch check
6. If ninja is live: Sends notification
```

### Multi-Server Support
```
Server A: /addstreamer xqc → Notifies in Server A's channel
Server B: /addstreamer xqc → Notifies in Server B's channel
Bot: Makes single API call for "xqc", sends to both servers
```

### Preventing Duplicate Notifications
```
1. Loop 1: xqc is live → Send notification, mark as notified
2. Loop 2: xqc still live → Skip (already notified)
3. Loop 3: xqc offline → Remove from tracking
4. Loop 4: xqc goes live again → Send new notification
```

## Testing Checklist

- [ ] Bot comes online in Discord
- [ ] Slash commands appear and work
- [ ] Can add a streamer successfully
- [ ] Can see monitored streamers with `/streamers`
- [ ] Can set notification channel
- [ ] Receives notification when streamer goes live
- [ ] No duplicate notifications for same stream
- [ ] Can remove a streamer
- [ ] Multi-server support works

## Deployment Checklist

- [ ] Created Discord bot with proper intents
- [ ] Created Twitch application
- [ ] Tested locally and commands work
- [ ] Created Fly.io account
- [ ] Created Fly.io app
- [ ] Created persistent volume
- [ ] Set all secrets
- [ ] Deployed successfully
- [ ] Bot shows online in Discord
- [ ] Logs show "Stream checking loop started"

## Troubleshooting

### Bot offline in Discord
```bash
fly logs  # Check for errors
fly status  # Verify app is running
```

### Commands not showing
- Wait 2-3 minutes for sync
- Check bot has `applications.commands` scope
- Re-invite bot with correct URL

### No notifications
- Use `/live` to verify streamers are actually live
- Check bot has Send Messages permission in the channel
- Review logs: `fly logs` or local console

### Database errors
```bash
# Reset database (CAUTION: Deletes all data)
fly ssh console
rm /data/twitch_bot.db
exit
fly apps restart your-app-name
```

## Next Steps

Once your bot is running:

1. **Monitor it:** Check logs occasionally with `fly logs`
2. **Invite to servers:** Share your bot with friends
3. **Add features:** See ARCHITECTURE.md for enhancement ideas
4. **Customize:** Modify notification format, add role mentions, etc.

## Documentation Quick Links

- **Full Setup:** README.md
- **Local Testing:** TESTING.md
- **Deployment:** DEPLOYMENT.md
- **Technical Details:** ARCHITECTURE.md

## Learning Resources

### Discord.py
- Docs: https://discordpy.readthedocs.io/
- Interactions: https://discordpy.readthedocs.io/en/stable/interactions/

### Twitch API
- Docs: https://dev.twitch.tv/docs/api/
- Reference: https://dev.twitch.tv/docs/api/reference

### Fly.io
- Docs: https://fly.io/docs/
- Python Guide: https://fly.io/docs/languages-and-frameworks/python/

## Support

Having issues? Check:
1. **Logs** - Most errors show up here
2. **TESTING.md** - Common issues and solutions
3. **README.md** - Full documentation

## Success Criteria

You'll know it's working when:
- ✅ Bot shows online in Discord
- ✅ Slash commands appear in the command menu
- ✅ Can add/remove streamers without errors
- ✅ Receives notification when monitored streamer goes live
- ✅ Notifications include stream title, game, and thumbnail
- ✅ No duplicate notifications
- ✅ Works across multiple Discord servers

## Project Stats

- **Lines of Code:** ~800 (Python)
- **Dependencies:** 3 (discord.py, aiohttp, python-dotenv)
- **Database Tables:** 2
- **Slash Commands:** 5
- **Average Memory:** ~100 MB
- **API Calls:** ~40/hour (with default settings)
- **Cost:** Free (Fly.io free tier)

---

**Ready to get started?** Run `./quickstart.sh` or follow TESTING.md!
