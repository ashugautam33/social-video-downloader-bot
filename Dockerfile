FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/root/.deno/bin:${PATH}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install Deno, the recommended JavaScript runtime for yt-dlp.
RUN curl -fsSL https://deno.land/install.sh | sh

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir --upgrade -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
