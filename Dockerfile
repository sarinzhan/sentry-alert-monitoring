FROM python:3.12-slim

# unbuffered stdout/stderr so logs show up in `docker logs` immediately;
# no .pyc files in the image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# install deps first (better layer caching).
# The corporate proxy intercepts TLS with a self-signed CA, so pip can't verify
# pypi.org's cert — trust those hosts explicitly (mirrors TELEGRAM_SSL_INSECURE).
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --trusted-host pypi.org \
        --trusted-host files.pythonhosted.org \
        --trusted-host pypi.python.org \
        -r requirements.txt

# then the app (all modules live in the project root)
COPY *.py ./

# run unprivileged; /app/data holds the SQLite debounce db and must be writable
# (a named volume mounted here inherits this ownership on first creation)
RUN useradd --system --no-create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# the service listens here; other containers reach it as http://sentry-telegram:8080
EXPOSE 8080

CMD ["python", "main.py"]
