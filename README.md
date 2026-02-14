# Twitch Notifier Discord Bot

A Discord bot that monitors Twitch streamers and sends notifications when they go live. Similar to Streamcord, but self-hosted and customizable.

## Features

- 🔴 Monitor multiple Twitch streamers per Discord server
- 📢 Automatic notifications when streamers go live
- ⚡ Batch API requests (up to 100 streamers per check)
- 🗄️ SQLite database for persistent storage
- 🎮 Slash commands for easy configuration
- 🖥️ Multi-server support with per-server notification channels
- 🔄 Efficient polling (default: every 90 seconds)

## Commands

All commands require "Manage Server" permission:

- `/addstreamer <username>` - Start monitoring a Twitch streamer
- `/removestreamer <username>` - Stop monitoring a streamer
- `/streamers` - List all monitored streamers
- `/setchannel <channel>` - Set notification channel
- `/live` - Check which monitored streamers are currently live

## Setup Instructions

### 1. Prerequisites

- Python 3.8 or higher
- Discord Bot Token
- Twitch API credentials

### 2. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and give it a name
3. Go to the "Bot" section
4. Click "Add Bot"
5. Under "Token", click "Reset Token" and copy it (you'll need this)
6. Enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
7. Go to "OAuth2" → "URL Generator"
8. Select scopes: `bot`, `applications.commands`
9. Select bot permissions: 
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
10. Copy the generated URL and use it to invite the bot to your server

### 3. Create Twitch Application

1. Go to [Twitch Developers Console](https://dev.twitch.tv/console/apps)
2. Click "Register Your Application"
3. Name: "Twitch Notifier Bot" (or anything you want)
4. OAuth Redirect URLs: `http://localhost` (required but not used)
5. Category: Choose any category
6. Click "Create"
7. Click "Manage" on your new application
8. Copy the **Client ID**
9. Click "New Secret" and copy the **Client Secret**

### 4. Install and Configure

```bash
# Clone or download this repository
cd twitch-notifier-bot

# Install dependencies
pip install -r requirements.txt

# Create .env file from template
cp .env.example .env

# Edit .env with your credentials
# Use nano, vim, or any text editor:
nano .env
```

Fill in your `.env` file:
```
DISCORD_TOKEN=your_discord_bot_token_here
TWITCH_CLIENT_ID=your_twitch_client_id_here
TWITCH_CLIENT_SECRET=your_twitch_client_secret_here
CHECK_INTERVAL_SECONDS=90
```

### 5. Run the Bot

```bash
python bot.py
```

You should see:
```
Logged in as YourBotName (ID: ...)
Command tree synced
Stream checking loop started
```

## Usage Example

1. Invite the bot to your Discord server
2. Use `/addstreamer shroud` to start monitoring shroud
3. Use `/setchannel #live-streams` to set where notifications go
4. When shroud goes live, you'll get a notification!

## Database Schema

### `server_settings`
- `guild_id` (PRIMARY KEY) - Discord server ID
- `notification_channel_id` - Channel for notifications
- `created_at` - When server was configured

### `monitored_streamers`
- `id` (PRIMARY KEY) - Auto-increment ID
- `guild_id` - Discord server ID
- `streamer_name` - Twitch username (lowercase)
- `channel_id` - Notification channel
- `added_at` - When streamer was added
- UNIQUE constraint on (guild_id, streamer_name)

## How It Works

1. **Polling Loop**: Every 90 seconds (configurable), the bot checks Twitch API
2. **Batch Requests**: Queries up to 100 streamers at once for efficiency
3. **Duplicate Prevention**: Tracks which streamers are live to avoid spam
4. **Multi-Server**: Same streamer can be monitored by multiple servers
5. **Notifications**: Rich embeds with stream title, game, viewers, and thumbnail

## Deployment on Fly.io

Since you're familiar with Fly.io, here's a quick deployment guide:

### Create `fly.toml`:

```toml
app = "your-twitch-bot-name"
primary_region = "iad"

[build]
  builder = "paketobuildpacks/builder:base"

[env]
  CHECK_INTERVAL_SECONDS = "90"

[deploy]
  release_command = "python -c 'from database import Database; Database()'"
```

### Create `Procfile`:

```
worker: python bot.py
```

### Deploy:

```bash
# Login to Fly.io
fly auth login

# Create app
fly apps create your-twitch-bot-name

# Set secrets
fly secrets set DISCORD_TOKEN=your_token_here
fly secrets set TWITCH_CLIENT_ID=your_client_id
fly secrets set TWITCH_CLIENT_SECRET=your_client_secret

# Deploy
fly deploy

# Check status
fly status
fly logs
```

## Configuration

### Adjust Check Interval

In `.env`, change `CHECK_INTERVAL_SECONDS`:
- Minimum recommended: 60 seconds (to respect API rate limits)
- Default: 90 seconds (good balance)
- Higher values use less API calls but slower notifications

### API Rate Limits

- Twitch API: 800 requests per minute (we're well within this)
- With 100 streamers and 90-second checks: ~40 requests/hour
- Discord: No issues with our notification rate

## Troubleshooting

### Bot not responding to commands
- Make sure you've enabled "Message Content Intent" in Discord Developer Portal
- Wait a few minutes after adding the bot for slash commands to sync
- Try kicking and re-inviting the bot

### "Twitch user not found"
- Check spelling of the Twitch username
- Use the login name (lowercase), not display name

### Notifications not sending
- Check bot has permission to send messages in the notification channel
- Use `/setchannel` to verify the notification channel is set
- Check bot logs for errors

### Database locked errors
- Usually happens when running multiple instances
- Make sure only one instance of the bot is running

## Future Enhancements

- [ ] Webhook support instead of polling (more efficient)
- [ ] Custom notification messages per server
- [ ] Role mentions (@everyone, @here, custom roles)
- [ ] Stream category/game filtering
- [ ] Statistics tracking (uptime, viewer counts)
- [ ] Web dashboard for configuration

## License

MIT License - Feel free to modify and use as you wish!

## Contributing

Contributions welcome! This is a learning project, so feel free to suggest improvements or submit pull requests.
