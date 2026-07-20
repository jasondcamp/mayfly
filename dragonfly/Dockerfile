FROM python:3.12-alpine
LABEL org.opencontainers.image.source="https://github.com/jasondcamp/mayfly" \
      org.opencontainers.image.description="mayfly-dragonfly: auto-discovering connectivity verifier for mayfly environments" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

WORKDIR /app
# pure-python clients only: no compiler needed in the image
RUN pip install --no-cache-dir boto3 pg8000 redis pymemcache kafka-python-ng

COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
