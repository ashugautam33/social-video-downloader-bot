FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y \
        ffmpeg \
        curl \
        ca-certificates \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -U -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
