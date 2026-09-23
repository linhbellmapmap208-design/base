FROM python:3.11-slim

# Cai dat cac thu vien he thong ma librosa/yt-dlp/transkun can:
# ffmpeg      - xu ly/convert audio
# libsndfile1 - librosa doc file am thanh
# build-essential, git - can de build mot so goi Python co C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cai dependencies truoc de tan dung cache (chi cai lai khi requirements.txt doi)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code bot vao
COPY bot.py .

# Chay bot
CMD ["python", "bot.py"]
