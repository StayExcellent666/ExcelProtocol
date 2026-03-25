# ExcelProtocol 
since 21 Feb 2026

A Discord bot for monitoring Twitch streamers and managing your community, with a built-in Twitch chat bot.

---

## Features

### 🔔 Stream Notifications
Automatically notifies a configured Discord channel when monitored Twitch streamers go live. Notifications include the stream title, game, viewer count, and a live thumbnail with a Watch Stream button. Notifications can optionally be auto-deleted when the streamer goes offline.

### ⏱️ Stream Milestones
Optionally sends milestone notifications at 5 and 10 hours of continuous streaming to keep your community engaged.

### 🎨 Custom Embed Colors
Each server can set its own color for all bot embeds to match their community's branding.

### 🗑️ Channel Auto-Cleanup
Configure any channel to automatically delete messages older than a set interval. Useful for keeping announcement or clip channels tidy. Pinned messages can be preserved.

### 🤖 Twitch Chat Bot
A built-in Twitch chat bot that joins your channel and responds to commands. Managed entirely through Discord slash commands — no code editing required.

### 💬 Custom Chat Commands
Create and edit custom commands for your Twitch chat directly from Discord using a dropdown and modal interface. Supports permission levels, cooldowns, and dynamic variables like `$user`, `$game`, `$uptime`, `$viewers`, and `$count`.

### 📣 Built-in Chat Commands
- `!commands` — lists all available commands
- `!uptime` — how long the stream has been live
- `!game` — current game/category
- `!title` — current stream title
- `!viewers` — current viewer count
- `!so @user` — rich shoutout with last streamed game and date (mod only)

### 🏆 Monthly Leaderboards
Tracks how many times each streamer goes live throughout the month. Resets automatically on the 1st of each month.
- `/leaderboard` — top streamers in your server this month
- `/globalleaderboard` — top streamers across all servers

### 🔘 Reaction Roles
Create reaction role panels with buttons or dropdowns so members can self-assign roles. Supports max role limits, only-add mode, custom labels, and emojis.

### 🎂 Birthdays
Members can register their birthday. The bot announces it on the day in a configured channel. Mods can manage entries for anyone.

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
| `/setchannel` | Manage Server | Set notification or birthday channel |
| `/setcolor` | Manage Server | Set custom embed color |
| `/resetcolor` | Manage Server | Reset embed color to default |
| `/autodelete` | Manage Server | Toggle auto-delete when streamer goes offline |
| `/milestonetoggle` | Manage Server | Toggle 5h and 10h milestone notifications |
| `/live` | Everyone | Check which monitored streamers are live now |
| `/repostlive` | Manage Server | Re-send notifications for currently live streamers |
| `/manualnotif` | Manage Server | Manually send a stream notification |

### Channel Cleanup
| Command | Permission | Description |
|---|---|---|
| `/cleanupset` | Manage Server | Set a channel to auto-clean old messages |
| `/cleanupremove` | Manage Server | Remove auto-cleanup from a channel |
| `/cleanuplist` | Manage Server | View cleanup configurations |

### Twitch Chat Bot
| Command | Permission | Description |
|---|---|---|
| `/twitchset` | Manage Server | Link this server to a Twitch channel |
| `/twitchremove` | Manage Server | Unlink from Twitch channel |
| `/twitchstatus` | Everyone | Show linked Twitch channel and command count |
| `/twitchstats` | Bot Owner | View all channels using the Twitch bot |

### Custom Commands
| Command | Permission | Description |
|---|---|---|
| `/cmd` | Manage Server | Add or edit a custom Twitch chat command |
| `/cmdremove` | Manage Server | Remove a command via dropdown |
| `/cmdlist` | Everyone | List all custom commands |
| `/cmdinfo` | Everyone | Show details of a specific command |

### Reaction Roles
| Command | Permission | Description |
|---|---|---|
| `/rr create` | Manage Roles | Start creating a new reaction role panel |
| `/rr addrole` | Manage Roles | Add a role to your panel |
| `/rr publish` | Manage Roles | Post or update the panel |
| `/rr edit` | Manage Roles | Edit an existing panel |
| `/rr delete` | Manage Roles | Delete a panel |
| `/rr sort` | Manage Roles | Sort roles alphabetically |
| `/rr list` | Manage Roles | List all panels in this server |
| `/rr cancel` | Manage Roles | Cancel current session |

### Birthdays
| Command | Permission | Description |
|---|---|---|
| `/birthday` | Everyone | Set your birthday (or another user's if mod) |
| `/birthdayremove` | Everyone | Remove a birthday entry |
| `/birthdaylist` | Mod/Admin | View all birthdays in the server |

### Leaderboards
| Command | Permission | Description |
|---|---|---|
| `/leaderboard` | Everyone | Top streamers in this server this month |
| `/globalleaderboard` | Bot Owner | Top streamers across all servers this month |
| `/notiflog` | Manage Server | Check notification history for a streamer |

### Info
| Command | Permission | Description |
|---|---|---|
| `/help` | Everyone | Paginated setup and command guide |
| `/stats` | Everyone | Bot stats for this server |
| `/streamers` | Everyone | List monitored streamers |

### Owner Only
| Command | Permission | Description |
|---|---|---|
| `/botinfo` | Bot Owner | Bot stats, uptime, memory usage |
| `/serverdetails` | Bot Owner | Details for a specific server |
| `/globalleaderboard` | Bot Owner | Top streamers across all servers this month |
| `/dbstats` | Bot Owner | Live database snapshot |
| `/twitchstats` | Bot Owner | View all Twitch channels using the bot |

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

**Example:** `/cmd` → New Command → `!lurk` → `Thanks for lurking $user, we have $viewers viewers!`

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
- Fly.io
