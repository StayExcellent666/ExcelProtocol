# Testing Guide

Quick guide to test your bot locally before deploying.

## Initial Setup

1. **Install Python dependencies:**
```bash
cd twitch-notifier-bot
pip install -r requirements.txt
```

2. **Create `.env` file:**
```bash
cp .env.example .env
```

3. **Edit `.env` with your credentials:**
```bash
# Use your favorite editor
nano .env
# or
code .env
```

Fill in:
```
DISCORD_TOKEN=your_discord_bot_token_here
TWITCH_CLIENT_ID=your_twitch_client_id_here
TWITCH_CLIENT_SECRET=your_twitch_client_secret_here
CHECK_INTERVAL_SECONDS=90
```

## Get Your Credentials

### Discord Bot Token

1. Go to https://discord.com/developers/applications
2. Create new application (or select existing)
3. Go to "Bot" section
4. Reset Token → Copy the token
5. Enable these intents:
   - Server Members Intent ✅
   - Message Content Intent ✅
6. Go to OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: 
     - Send Messages ✅
     - Embed Links ✅
     - Attach Files ✅
     - Read Message History ✅
7. Copy the URL and invite bot to your test server

### Twitch API Credentials

1. Go to https://dev.twitch.tv/console/apps
2. Register Your Application
3. Name: "Twitch Notifier Bot Test" (or anything)
4. OAuth Redirect: `http://localhost`
5. Category: Select any
6. Create → Manage
7. Copy Client ID
8. Generate New Secret → Copy Client Secret

## Run the Bot

```bash
python bot.py
```

You should see:
```
INFO - Logged in as YourBotName (ID: 1234567890)
INFO - ------
INFO - Command tree synced
INFO - Stream checking loop started
INFO - Checking 0 streamers...
```

## Test Commands

In your Discord server:

### 1. Test `/addstreamer`
```
/addstreamer shroud
```

Expected response:
```
✅ Now monitoring **Shroud** (twitch.tv/shroud)
Notifications will be sent to #your-channel
```

### 2. Test `/streamers`
```
/streamers
```

Expected response: List of monitored streamers

### 3. Test `/setchannel`
```
/setchannel #live-streams
```

Expected response:
```
✅ Stream notifications will now be sent to #live-streams
```

### 4. Test `/live`
```
/live
```

This will check if any monitored streamers are currently live.

### 5. Test `/removestreamer`
```
/removestreamer shroud
```

Expected response:
```
✅ No longer monitoring **shroud**
```

## Testing the Notification System

To test if notifications work:

1. Add a streamer who streams frequently:
```
/addstreamer xqc
```

2. Check the logs - you should see:
```
INFO - Checking 1 streamers...
```

3. Wait for them to go live (or add multiple streamers to increase chances)

4. When someone goes live, you should see:
```
INFO - Sent notification for StreamerName to YourServerName
```

## Quick Test with Always-Live Streamers

Some streamers are almost always live (24/7 streams):

```
/addstreamer lofi
/addstreamer relaxbeats
```

These should trigger notifications quickly.

## Check Logs

The bot logs helpful information:

```
INFO - Database initialized at twitch_bot.db
INFO - Logged in as YourBot (ID: ...)
INFO - Command tree synced
INFO - Stream checking loop started
INFO - Checking 3 streamers...
INFO - Added streamer xqc for guild 123456789
INFO - Sent notification for xQc to TestServer
INFO - xqc went offline
```

## Common Issues

### Commands not showing up
- Wait 1-2 minutes after starting the bot
- Make sure bot has `applications.commands` scope
- Try kicking and re-inviting the bot

### "Twitch user not found"
- Check spelling
- Make sure using the login name (lowercase)
- Try visiting twitch.tv/username to verify

### No notifications
- Use `/live` to check if anyone is live
- Check bot has Send Messages permission
- Verify notification channel with `/setchannel`
- Check logs for errors

### "401 Unauthorized" in logs
- Twitch credentials are wrong
- Check your Client ID and Client Secret
- Make sure you copied them correctly

### Database locked
- Only run one instance at a time
- Close any other running instances

## Testing the Polling Loop

You can reduce the check interval for testing:

In `.env`:
```
CHECK_INTERVAL_SECONDS=30
```

This checks every 30 seconds instead of 90. **Don't use this in production** - it wastes API calls.

## Stop the Bot

Press `Ctrl+C` to stop the bot gracefully.

## Testing Multiple Servers

To test multi-server support:

1. Invite bot to 2+ servers
2. In Server A: `/addstreamer shroud`
3. In Server B: `/addstreamer shroud`
4. Both should get notifications when shroud goes live
5. Each server can have different notification channels

## Database Inspection

To see what's in the database:

```bash
# Install sqlite3 if needed
sqlite3 twitch_bot.db

# List all monitored streamers
SELECT * FROM monitored_streamers;

# List server settings
SELECT * FROM server_settings;

# Exit
.quit
```

## Next Steps

Once everything works locally:
1. Commit your code (don't commit `.env`!)
2. Follow DEPLOYMENT.md to deploy to Fly.io
3. Your bot will run 24/7

## Pro Tips

- Test with streamers you know will go live soon
- Use the logs to debug issues
- `/live` command is great for manual testing
- Keep CHECK_INTERVAL_SECONDS at 90+ in production
