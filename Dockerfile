FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite state lives here; mount a volume so it survives restarts.
ENV DATA_DIR=/data
VOLUME /data

CMD ["python", "bot.py"]
