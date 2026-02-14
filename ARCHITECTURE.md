# Architecture Overview

## System Design

```
┌─────────────────────────────────────────────────────────────┐
│                     Discord Bot (bot.py)                     │
│                                                               │
│  ┌──────────────────┐         ┌────────────────────────┐   │
│  │  Slash Commands  │         │   Polling Loop         │   │
│  │                  │         │   (every 90 seconds)   │   │
│  │  /addstreamer    │         │                        │   │
│  │  /removestreamer │         │  1. Get all streamers  │   │
│  │  /streamers      │         │  2. Batch check Twitch │   │
│  │  /setchannel     │         │  3. Send notifications │   │
│  │  /live           │         │  4. Track live status  │   │
│  └──────────────────┘         └────────────────────────┘   │
│           │                              │                   │
│           ▼                              ▼                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │            Database (database.py)                     │  │
│  │            SQLite - twitch_bot.db                     │  │
│  │                                                        │  │
│  │  Tables:                                              │  │
│  │  • server_settings (guild_id, channel_id)            │  │
│  │  • monitored_streamers (guild, streamer, channel)    │  │
│  └──────────────────────────────────────────────────────┘  │
│                              │                               │
└──────────────────────────────┼───────────────────────────────┘
                               │
                               ▼
                    ┌────────────────────┐
                    │   Twitch API       │
                    │  (twitch_api.py)   │
                    │                    │
                    │  • OAuth token     │
                    │  • User lookup     │
                    │  • Stream status   │
                    │  • Profile images  │
                    └────────────────────┘
```

## Data Flow

### Adding a Streamer
```
User: /addstreamer shroud
    ↓
1. Check permissions (Manage Server)
    ↓
2. Query Twitch API → Verify "shroud" exists
    ↓
3. Add to database (guild_id, "shroud", channel_id)
    ↓
4. Send confirmation to user
```

### Notification Flow
```
Polling Loop (every 90s)
    ↓
1. Database → Get all unique streamers
    ↓
2. Twitch API → Batch check (up to 100 at once)
    ↓
3. Filter new live streams (not already notified)
    ↓
4. For each live stream:
   - Find all guilds monitoring this streamer
   - Send notification embed to each guild's channel
   - Mark as notified
    ↓
5. Remove offline streamers from tracking
```

## File Structure

```
twitch-notifier-bot/
│
├── bot.py                    # Main bot logic
│   ├── TwitchNotifierBot class
│   ├── Slash command handlers
│   └── Polling loop (check_streams)
│
├── database.py               # Database operations
│   ├── Database class
│   ├── CRUD operations
│   └── SQLite connection management
│
├── twitch_api.py            # Twitch API integration
│   ├── TwitchAPI class
│   ├── OAuth token management
│   ├── User lookup
│   └── Stream status checking
│
├── config.py                # Configuration
│   ├── Load environment variables
│   └── Validate required settings
│
├── requirements.txt         # Python dependencies
├── .env                     # Environment variables (not committed)
├── .gitignore              # Git ignore rules
│
├── README.md               # Main documentation
├── TESTING.md              # Testing guide
├── DEPLOYMENT.md           # Fly.io deployment guide
│
├── fly.toml                # Fly.io configuration
├── Procfile                # Process definition
└── quickstart.sh           # Setup script
```

## Key Components

### 1. Discord Bot (bot.py)
- **Purpose:** Main entry point, handles Discord interactions
- **Key Features:**
  - Slash command registration and handling
  - Background polling loop
  - Notification sending
  - Live stream tracking

### 2. Database Layer (database.py)
- **Purpose:** Data persistence and management
- **Key Features:**
  - SQLite for simplicity
  - Server settings storage
  - Streamer monitoring lists
  - Efficient queries with indexes

### 3. Twitch API Client (twitch_api.py)
- **Purpose:** Interface with Twitch
- **Key Features:**
  - OAuth app access token
  - User verification
  - Batch stream checking (up to 100)
  - Automatic token refresh

### 4. Configuration (config.py)
- **Purpose:** Centralized settings
- **Key Features:**
  - Environment variable loading
  - Validation
  - Default values

## Database Schema

### server_settings
```sql
CREATE TABLE server_settings (
    guild_id INTEGER PRIMARY KEY,
    notification_channel_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### monitored_streamers
```sql
CREATE TABLE monitored_streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    streamer_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, streamer_name)
);

-- Indexes for performance
CREATE INDEX idx_guild_id ON monitored_streamers(guild_id);
CREATE INDEX idx_streamer_name ON monitored_streamers(streamer_name);
```

## API Usage Patterns

### Twitch API Rate Limits
- **Limit:** 800 requests per minute
- **Our Usage:** ~40 requests per hour (with 90s interval)
- **Batch Size:** Up to 100 streamers per request
- **Safety Margin:** Excellent - far below limits

### Discord API
- **Rate Limits:** Generous for our use case
- **Our Usage:** 1 message per live stream
- **No Issues:** Expected with typical monitoring

## Deployment Architecture (Fly.io)

```
┌─────────────────────────────────┐
│         Fly.io Platform         │
│                                 │
│  ┌───────────────────────────┐ │
│  │   Docker Container        │ │
│  │                           │ │
│  │   Python Bot Process      │ │
│  │   ├── bot.py              │ │
│  │   ├── database.py         │ │
│  │   └── twitch_api.py       │ │
│  │                           │ │
│  │   /data/ (Persistent)     │ │
│  │   └── twitch_bot.db       │ │
│  └───────────────────────────┘ │
│                                 │
│  Environment Variables:         │
│  • DISCORD_TOKEN (secret)       │
│  • TWITCH_CLIENT_ID (secret)    │
│  • TWITCH_CLIENT_SECRET (secret)│
│  • CHECK_INTERVAL_SECONDS       │
└─────────────────────────────────┘
```

## Performance Considerations

### Memory Usage
- **Base:** ~50-100 MB
- **Per Streamer:** Negligible
- **Recommendation:** 256 MB allocated (plenty of headroom)

### CPU Usage
- **Idle:** Minimal
- **During Checks:** Brief spike every 90s
- **Recommendation:** Shared CPU is fine

### Network Usage
- **Twitch API:** Small JSON responses
- **Discord:** Embed messages
- **Total:** Very light

### Database Size
- **Per Server:** ~1 KB
- **Per Streamer:** ~100 bytes
- **1000 Streamers:** ~100 KB
- **Recommendation:** 1 GB volume (massive overkill)

## Scalability

Current design supports:
- ✅ **Unlimited Discord servers**
- ✅ **~10,000 unique streamers** (100 batches × 100 per batch)
- ✅ **Sub-2-minute notification latency**
- ✅ **24/7 operation**

Bottlenecks (theoretical):
- Twitch API: 800 requests/min → 80,000 streamers/min possible
- Discord API: ~50 messages/sec → Non-issue for our use case
- SQLite: Handles millions of rows → Non-issue

## Security Considerations

### Credentials
- ✅ Stored as environment variables
- ✅ Never committed to git (.gitignore)
- ✅ Encrypted in Fly.io secrets

### Permissions
- ✅ Slash commands require "Manage Server"
- ✅ Bot permissions scoped to minimum needed
- ✅ No privileged operations

### Data Privacy
- ✅ Only stores public Twitch usernames
- ✅ No user data collection
- ✅ Per-server data isolation

## Future Enhancements

### Potential Improvements
1. **Webhooks:** Replace polling with Twitch EventSub
2. **Custom Messages:** Per-server notification templates
3. **Role Mentions:** Tag specific roles on notifications
4. **Filtering:** Only notify for specific games/categories
5. **Statistics:** Track stream durations, viewer counts
6. **Web Dashboard:** Browser-based configuration
7. **Multi-Language:** i18n support

### Migration Path
Current architecture is designed for easy enhancement:
- Modular structure allows adding features independently
- Database schema can be extended without breaking changes
- Polling → Webhook migration is straightforward
