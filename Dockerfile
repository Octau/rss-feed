FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite state lives here; mount a volume so it survives restarts.
ENV DATA_DIR=/data
VOLUME /data

# Daily log files live here; mount a volume to keep them across restarts.
ENV LOG_DIR=/logs
VOLUME /logs

CMD ["python", "bot.py"]
