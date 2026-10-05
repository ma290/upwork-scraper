FROM python:3.12-slim

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HEADLESS=true \
    PORT=8000

WORKDIR /app

# Install system dependencies required for Headless Chrome and Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo-gobject2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required by Botasaurus)
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome Stable
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm -f google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scraper source code and API
COPY src/ ./src/
COPY api.py .

# Create persistent storage directories
RUN mkdir -p /app/storage /app/browser_data

EXPOSE 8000

# Launch Uvicorn with 1 worker process to strictly honor the 512MB RAM budget
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
