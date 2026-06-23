FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app

WORKDIR /app

COPY requirements.runtime.txt /tmp/requirements.runtime.txt
COPY vendor /tmp/vendor
RUN pip install --no-cache-dir /tmp/vendor/quotemux-0.1.0-py3-none-any.whl \
    && pip install --no-cache-dir -r /tmp/requirements.runtime.txt

COPY vendor/markethub_packages /app/quotemux_packages
COPY services/markethub_api/src /app/services/markethub_api/src
COPY services/markethub_api/app.py /app/services/markethub_api/app.py
COPY services/markethub_api/runtime_paths.py /app/services/markethub_api/runtime_paths.py
COPY services/markethub_console/web /app/services/markethub_console/web
COPY services/markethub_console/dist /app/services/markethub_console/dist

WORKDIR /app/services/markethub_api

EXPOSE 8803

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "from urllib.request import urlopen; import json; payload=json.loads(urlopen('http://127.0.0.1:8803/api/health', timeout=4).read().decode('utf-8')); raise SystemExit(0 if payload.get('status') == 'ok' else 1)"]

CMD ["python", "app.py"]
