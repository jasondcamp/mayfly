FROM python:3.12-alpine
LABEL org.opencontainers.image.source="https://github.com/jasondcamp/mayfly" \
      org.opencontainers.image.description="mayfly-hello: minimal LB/ingress test app — shows serving pod and forwarded headers" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

WORKDIR /app
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
