FROM python:3.12-slim

# unbuffered stdout/stderr so logs show up in `docker logs` immediately;
# no .pyc files in the image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# then the app (all modules live in the project root)
COPY *.py ./

# run unprivileged; /app must stay writable for the SQLite debounce db (state.db)
RUN useradd --system --no-create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# the service listens here; other containers reach it as http://sentry-telegram:8080
EXPOSE 8080

CMD ["python", "main.py"]
