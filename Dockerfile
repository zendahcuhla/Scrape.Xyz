FROM python:3.11-slim

# Install Chromium system dependencies
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 \
    fonts-liberation libappindicator3-1 \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
RUN pip install --no-cache-dir discord.py playwright aiohttp requests

# Install Chromium via Playwright
RUN playwright install chromium

COPY mercury_bot.py .
COPY mailhub.py .
COPY promo.png .

CMD ["python", "mercury_bot.py"]