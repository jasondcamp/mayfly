FROM python:3.12-alpine
LABEL org.opencontainers.image.source="https://github.com/jasondcamp/mayfly" \
      org.opencontainers.image.description="mayfly-caddis: sample full-stack app — Flask API + kafka worker exercising S3, postgres, redis, kafka, and Secrets Manager" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

WORKDIR /app
RUN pip install --no-cache-dir flask gunicorn boto3 pg8000 redis kafka-python-ng

COPY app.py worker.py ./
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]
