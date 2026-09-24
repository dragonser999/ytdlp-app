FROM python:3.11-slim

# ffmpeg is needed to merge separate video+audio streams into one file
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U yt-dlp

COPY app.py .

# Koyeb sets $PORT; default to 8000 for local testing
ENV PORT=8000
EXPOSE 8000

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
