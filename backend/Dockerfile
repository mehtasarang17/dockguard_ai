FROM python:3.11-slim

# Force Python to flush stdout/stderr immediately (critical for Docker log visibility)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# System dependencies (antiword for legacy .doc support)
RUN apt-get update && apt-get install -y antiword && rm -rf /var/lib/apt/lists/*
COPY . .

RUN mkdir -p /app/uploads

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "300", "--preload", "app:app"]
