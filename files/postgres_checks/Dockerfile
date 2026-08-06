FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY postgres_check_example.py .

EXPOSE 8080

USER nobody

# Absolute path avoids PATH edge cases; python3 matches the slim image
ENTRYPOINT ["/usr/local/bin/python3", "/app/postgres_check_example.py"]
