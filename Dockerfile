# =============================================================================
# Stage 1 — build the React dashboard
# =============================================================================
# Uses a slim Node image. Only runs when dashboard/ source files change
# (Docker layer cache reuses npm install if package*.json hasn't changed).
FROM node:20-slim AS frontend-builder

WORKDIR /build

# Copy lockfile + package.json first so npm ci layer caches independently
# of source changes. Editing App.jsx skips this expensive step.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

# Copy the rest of the dashboard source and build
COPY dashboard/index.html ./
COPY dashboard/vite.config.js ./
COPY dashboard/src ./src
COPY dashboard/public ./public

RUN npm run build
# Output: /build/dist/

# =============================================================================
# Stage 2 — Python runtime (the actual deployed image)
# =============================================================================
FROM python:3.11-slim-bookworm

# Install SQLite runtime, image libs, fonts, and build dependencies.
# - libsqlite3-* : SQLite
# - libjpeg62-turbo, libpng16-16, libfreetype6 : Pillow runtime deps for
#                                                JPEG/PNG/text rendering
# - fonts-dejavu-core : provides /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
#                       used by welcome_banner.py for the banner text overlay
# - gcc : transient, removed below after pip install
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    libsqlite3-dev \
    libjpeg62-turbo \
    libpng16-16 \
    libfreetype6 \
    fonts-dejavu-core \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Remove build dependencies to reduce image size
RUN apt-get purge -y gcc && \
    apt-get autoremove -y && \
    apt-get clean

# Copy only necessary Python files
COPY utils.py bot.py database.py twitch_api.py config.py twitch_bot.py twitch_chat_cog.py reaction_roles.py setchannel_cog.py birthday_cog.py dashboard_server.py server_setup.py welcome_banner.py ./

# Banner template + (future) other static assets used by Python code.
# welcome_banner.py resolves the template path relative to its own
# location, so this needs to live alongside the Python files at /app/assets/.
COPY assets ./assets

# Copy the freshly-built React dashboard from stage 1.
# (No more committing dist/ to git — it's built fresh on every deploy.)
COPY --from=frontend-builder /build/dist ./dashboard/dist

# Run the bot
CMD ["python", "bot.py"]
