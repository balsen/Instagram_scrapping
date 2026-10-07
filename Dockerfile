FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    OUTPUT_DIR=/app/output

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py jobs.py ./
COPY scraper ./scraper
COPY templates ./templates
RUN mkdir -p /app/output && chown pwuser:pwuser /app/output

USER pwuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "app:create_app()"]
