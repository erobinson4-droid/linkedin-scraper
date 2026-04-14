FROM python:3.11-slim

WORKDIR /app

# Install system dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its OS-level dependencies
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

EXPOSE 5050

CMD ["python", "app.py"]
