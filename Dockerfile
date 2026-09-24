FROM python:3.11-slim

# ffmpeg is needed to merge separate video+audio streams into one file
# curl + unzip are needed to install Deno below
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp now needs a JS runtime to solve YouTube's signature/challenge
# scripts. Without this, extraction fails with odd errors like
# "The page needs to be reloaded."
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U yt-dlp

COPY app.py .

# Koyeb sets $PORT; default to 8000 for local testing
ENV PORT=8000
EXPOSE 8000

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
