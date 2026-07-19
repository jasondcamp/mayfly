FROM python:3.12-alpine

WORKDIR /app
# pure-python clients only: no compiler needed in the image
RUN pip install --no-cache-dir boto3 pg8000 redis kafka-python-ng

COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
