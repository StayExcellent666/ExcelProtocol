# ExcelProtocol

A Discord bot for monitoring Twitch streamers and managing your community, with a built-in Twitch chat bot.

---

## Features

### 🔔 Stream Notifications
Automatically notifies a configured Discord channel when monitored Twitch streamers go live. Notifications include the stream title, game, viewer count, and a live thumbnail with a Watch Stream button. Notifications can optionally be auto-deleted when the streamer goes offline.

### 🎨 Custom Embed Colors
Each server can set its own color for stream notification embeds to match their community's branding.

### 🗑️ Channel Auto-Cleanup
Configure any channel to automatically delete messages older than a set interval. Useful for keeping announcement or clip channels tidy. Pinned messages can be preserved.

### 🤖 Twitch Chat Bot
A built-in Twitch chat bot that joins your channel and responds to commands. Managed entirely through Discord slash commands — no code editing required.

### 💬 Custom Chat Commands
Create custom commands for your Twitch chat directly from Discord. Supports permission levels, cooldowns, and dynamic variables like `$user`, `$game`, `$uptime`, `$viewers`, and `$count`.

### 📣 Built-in Chat Commands
- `!uptime` — how long the stream has been live
- `!game` — current game/category
- `!title` — current stream title
- `!viewers` — current viewer count
- `!commands` — lists all available commands
- `!so @user` — rich shoutout with last streamed game and date (mod only)

### 🏆 Monthly Leaderboards
Tracks how many times each streamer goes live throughout the month. Resets automatically on the 1st of each month.
- `/leaderboard` — top streamers in your server this month
- `/globalleaderboard` — top streamers across all servers (owner only, counts unique stream sessions)

### 🗄️ Database Stats
Owner-only command that shows a live snapshot of everything stored in the database — servers, streamers, commands, leaderboard data, and more.

---

## Discord Commands

### Stream Notifications
| Command | Permission | Description |
|---|---|---|
| `/addstreamer` | Manage Server | Add a Twitch streamer to monitor |
| `/removestreamer` | Manage Server | Stop monitoring a streamer |
| `/streamers` | Everyone | List all monitored streamers |
| `/setchannel` | Manage Server | Set the notification channel |
| `/setcolor` | Manage Server | Set embed color for notifications |
| `/autodelete` | Manage Server | Toggle auto-delete when streamer goes offline |
| `/live` | Everyone | Check which monitored streamers are live now |
| `/manualnotif` | Manage Server | Manually send a stream notification |

### Channel Cleanup
| Command | Permission | Description |
|---|---|---|
| `/cleanup add` | Manage Server | Set a channel to auto-clean old messages |
| `/cleanup remove` | Manage Server | Remove auto-cleanup from a channel |
| `/cleanup list` | Manage Server | View cleanup configurations |

### Twitch Chat Bot
| Command | Permission | Description |
|---|---|---|
| `/twitchset` | Manage Server | Link this server to a Twitch channel |
| `/twitchremove` | Manage Server | Unlink from Twitch channel |
| `/twitchstatus` | Everyone | Show linked Twitch channel |
| `/twitchstats` | Bot Owner | View all channels using the Twitch bot |

### Custom Commands
| Command | Permission | Description |
|---|---|---|
| `/cmdadd` | Manage Server | Add a custom Twitch chat command |
| `/cmdremove` | Manage Server | Remove a command |
| `/cmdedit` | Manage Server | Edit an existing command |
| `/cmdlist` | Everyone | List all custom commands |
| `/cmdinfo` | Everyone | Show details of a specific command |

### Leaderboards
| Command | Permission | Description |
|---|---|---|
| `/leaderboard` | Everyone | Top streamers in this server this month |
| `/globalleaderboard` | Bot Owner | Top streamers across all servers this month |

### Owner Only
| Command | Permission | Description |
|---|---|---|
| `/botinfo` | Bot Owner | Bot stats, uptime, memory usage |
| `/serverdetails` | Bot Owner | Details for a specific server |
| `/dbstats` | Bot Owner | Live database snapshot |

---

## Custom Command Variables

Use these placeholders in your command responses and they'll be filled in automatically:

| Variable | Description |
|---|---|
| `$user` | The name of the user who triggered the command |
| `$channel` | The Twitch channel name |
| `$count` | How many times this command has been used |
| `$game` | Current game/category |
| `$uptime` | How long the stream has been live |
| `$viewers` | Current viewer count |

**Example:** `/cmdadd command:!lurk response:Thanks for lurking $user, we have $viewers viewers!`

---

## Permission Levels for Custom Commands

| Level | Who can trigger |
|---|---|
| `everyone` | Anyone in chat |
| `subscriber` | Subscribers, mods, and broadcaster |
| `mod` | Mods and broadcaster only |
| `broadcaster` | Broadcaster only |

---

## Setup

### Required Environment Variables
```
DISCORD_TOKEN=
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
BOT_OWNER_ID=
```

### Optional (for Twitch chat bot)
```
TWITCH_BOT_USERNAME=
TWITCH_BOT_TOKEN=
```

Get your Twitch bot OAuth token from https://twitchapps.com/tmi/ while logged into your bot account.

### Deployment
The bot is designed to run on [Fly.io](https://fly.io) with a persistent volume for the SQLite database. See `fly.toml` for configuration.

```
fly deploy
```

---

## Tech Stack
- Python 3.11
- discord.py
- twitchio 2.x
- SQLite
- aiohttp
- Fly.io
