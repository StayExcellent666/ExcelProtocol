# ExcelProtocol

![Status](https://img.shields.io/badge/status-online-00CC66?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![Discord](https://img.shields.io/badge/discord.py-2.x-5865F2?style=flat-square&logo=discord&logoColor=white)
![Hosted on Fly.io](https://img.shields.io/badge/hosted%20on-Fly.io-7B36ED?style=flat-square)

A Discord bot built for Twitch streaming communities. ExcelProtocol delivers instant stream notifications via Twitch EventSub, manages reaction roles, tracks server stats, and gives server admins a full web dashboard to configure everything without touching a command.

---

## Features

### 📺 Stream Notifications
- Instant live alerts powered by **Twitch EventSub webhooks** — no polling, no delay
- Per-streamer custom notification channels
- Configurable ping role per server
- Auto-delete notifications when a streamer goes offline
- 5h and 10h milestone messages for marathon streams
- Duplicate notification prevention across rolling deploys

### 🎭 Reaction Roles
- Create fully customisable reaction role panels
- Single-choice, multi-choice, and add-only modes
- Custom embed body text and role limit per panel
- Managed entirely from the dashboard or via slash commands

### 📊 Server Stats
- Live member count displayed in voice channel names
- Auto-updates every 15 minutes

### 🟣 Twitch Integration
- Broadcaster OAuth flow for Channel Point Rewards
- EventSub webhook integration for reward redemptions
- OBS browser source overlay for video triggers
- Full channel rewards management from the dashboard

### ⚙️ Server Management
- Web dashboard at [excelprotocol.fly.dev](https://excelprotocol.fly.dev/app) — no commands needed
- Permission checker with yellow warning banners for misconfigured channels
- Birthday tracking with automated announcements
- Twitch chat commands and custom chat bot integration

---

## Commands

| Command | Description |
|---|---|
| `/addstreamer` | Add a Twitch streamer to monitor |
| `/removestreamer` | Stop monitoring a streamer |
| `/setchannel` | Set the default notification channel |
| `/manualnotif` | Manually trigger a live notification |
| `/repostlive` | Repost notifications for all currently live streamers |
| `/testnotification` | Preview what a notification looks like |
| `/setembed` | Customise notification embed colour |
| `/setpingrole` | Set the role to ping in notifications |
| `/milestonenotifs` | Toggle 5h/10h milestone messages |
| `/autodelete` | Toggle auto-delete when streamers go offline |
| `/stats` | Show bot stats and uptime |
| `/help` | Full command reference |
| `/tip` | Support the bot's development |

---

## Dashboard

The web dashboard gives server admins full control without needing slash commands.

- **Stream Notifications** — add, edit, and remove monitored streamers with search and ⚠️ warnings for unresolvable accounts
- **Reaction Roles** — create and manage role panels with a visual editor
- **Server Stats** — configure live member count channels
- **Server Settings** — notification channel, embed colour, ping role, auto-delete, milestones
- **Twitch** — connect broadcaster accounts, manage channel point rewards and OBS overlays
- **Notif Log** — full history of sent notifications with timestamps

---

## Tech Stack

- **Bot** — Python 3.11, discord.py 2.x, aiohttp
- **Dashboard** — React (single-file), aiohttp backend
- **Database** — SQLite on a persistent Fly.io volume
- **Stream Detection** — Twitch EventSub webhooks (instant, no polling)
- **Hosting** — Fly.io (Frankfurt region), deployed via GitHub Actions

---

## Legal

- [Terms of Service](https://excelprotocol.fly.dev/terms)
- [Privacy Policy](https://excelprotocol.fly.dev/privacy)

---

## Support

Have a question or found a bug? Reach out over the dashboard via the **Contact** tab!

---

*ExcelProtocol is an independent project and is not affiliated with Discord Inc. or Twitch Interactive, Inc.*
