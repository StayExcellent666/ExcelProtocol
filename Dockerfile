# Use Python slim (Debian-based but smaller than full)
FROM python:3.11-slim-bullseye

# Install SQLite runtime and build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    libsqlite3-dev \
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
COPY bot.py database.py twitch_api.py config.py twitch_bot.py twitch_chat_cog.py reaction_roles.py setchannel_cog.py birthday_cog.py ./

# Run the bot
CMD ["python", "bot.py"]
