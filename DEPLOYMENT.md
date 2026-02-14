# Fly.io Deployment Guide

This guide will help you deploy the Twitch Notifier bot to Fly.io for 24/7 hosting.

## Prerequisites

- Fly.io account (free tier works fine)
- Fly CLI installed: https://fly.io/docs/hands-on/install-flyctl/
- Discord Bot Token and Twitch API credentials

## Step 1: Install Fly CLI

```bash
# macOS/Linux
curl -L https://fly.io/install.sh | sh

# Windows (PowerShell)
iwr https://fly.io/install.ps1 -useb | iex
```

## Step 2: Login to Fly.io

```bash
fly auth login
```

This will open your browser for authentication.

## Step 3: Create the App

```bash
cd twitch-notifier-bot
fly apps create twitch-notifier-bot
```

**Note:** The app name must be unique across all of Fly.io. If "twitch-notifier-bot" is taken, try adding your name or a random number, like "twitch-notifier-yourname" or "twitch-notifier-123".

## Step 4: Create Persistent Volume

The bot needs persistent storage for the SQLite database:

```bash
fly volumes create twitch_bot_data --region iad --size 1
```

- `iad` is the region (US East - Virginia). Change if you prefer another region.
- Size is 1GB (more than enough for the database)

## Step 5: Set Secrets

Set your credentials as secrets (these are encrypted):

```bash
fly secrets set DISCORD_TOKEN="your_discord_bot_token_here"
fly secrets set TWITCH_CLIENT_ID="your_twitch_client_id_here"
fly secrets set TWITCH_CLIENT_SECRET="your_twitch_client_secret_here"
```

**Important:** Replace the values with your actual credentials!

## Step 6: Update fly.toml

Edit `fly.toml` and change the app name on line 1 to match what you created in Step 3:

```toml
app = "your-unique-app-name"
```

## Step 7: Deploy

```bash
fly deploy
```

This will:
1. Build your application
2. Create a Docker image
3. Deploy to Fly.io
4. Initialize the database
5. Start the bot

## Step 8: Verify It's Running

```bash
# Check app status
fly status

# View logs
fly logs

# Watch logs in real-time
fly logs -f
```

You should see:
```
Logged in as YourBotName (ID: ...)
Command tree synced
Stream checking loop started
```

## Useful Commands

### View Logs
```bash
fly logs              # Recent logs
fly logs -f           # Follow logs (live)
```

### SSH into the Container
```bash
fly ssh console
```

### Check Status
```bash
fly status
```

### Restart the Bot
```bash
fly apps restart twitch-notifier-bot
```

### Scale (if needed)
```bash
# The free tier includes 1 shared CPU VM
fly scale count 1
```

### Update the Bot
After making code changes:
```bash
fly deploy
```

### Stop the Bot
```bash
fly scale count 0
```

### Start the Bot Again
```bash
fly scale count 1
```

### Delete the App
```bash
fly apps destroy twitch-notifier-bot
```

## Troubleshooting

### "App name already taken"
Use a different name in Step 3 and update `fly.toml`

### "Volume not found"
Make sure you created the volume in the same region as your app:
```bash
fly volumes list
```

### Bot not responding
Check logs for errors:
```bash
fly logs -f
```

### Database errors
The database should persist in the `/data` volume. If you need to reset it:
```bash
fly ssh console
rm /data/twitch_bot.db
exit
fly apps restart twitch-notifier-bot
```

### Out of memory
The free tier has limited memory. If you're monitoring hundreds of streamers, you might need to upgrade:
```bash
fly scale memory 512  # 512MB
```

## Costs

- **Free Tier:** Includes enough resources to run this bot 24/7 for free
- **Volume:** 1GB volume is free on the free tier
- If you exceed free tier limits, Fly.io will email you

## Monitoring

### Check if Bot is Responding
Test in Discord:
```
/streamers
```

### View Recent Activity
```bash
fly logs --app twitch-notifier-bot
```

## Updating Your Bot

When you make code changes:

1. Test locally first
2. Commit changes to git (optional but recommended)
3. Deploy:
```bash
fly deploy
```

The deployment preserves your database since it's on a persistent volume.

## Backup Database

To backup your database:

```bash
# SSH into the container
fly ssh console

# From inside the container
cat /data/twitch_bot.db > /tmp/backup.db
exit

# From your local machine
fly ssh sftp get /data/twitch_bot.db ./backup.db
```

## Environment Variables

To change the check interval:
```bash
fly secrets set CHECK_INTERVAL_SECONDS=120
```

Then restart:
```bash
fly apps restart twitch-notifier-bot
```

## Next Steps

Your bot is now running 24/7 on Fly.io! 

- Monitor the logs occasionally for any errors
- The database is persistent and will survive restarts
- Updates are as simple as `fly deploy`
- Fly.io has a generous free tier perfect for Discord bots
