FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# Run unprivileged. The app only ever writes to /srv/data (device registry,
# logo cache, metadata cache, standing-data DB), which docker-compose mounts
# from the host - so that directory has to be owned by this user, and nothing
# else needs to be writable.
RUN useradd --system --create-home --uid 10001 flightinfo \
 && mkdir -p /srv/data \
 && chown -R flightinfo:flightinfo /srv/data
USER flightinfo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
