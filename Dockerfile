FROM python:3.12-slim

WORKDIR /app

# install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# then the app
COPY app.py .

# the service listens here; other containers reach it as http://sentry-telegram:8080
EXPOSE 8080

CMD ["python", "app.py"]