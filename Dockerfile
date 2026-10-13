FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY radar ./radar
COPY config.yaml ./config.yaml
COPY sample_data ./sample_data

RUN useradd --create-home --uid 10001 radar && mkdir -p /app/data && chown -R radar /app/data
USER radar

VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)" || exit 1

CMD ["python", "-m", "radar", "run"]
