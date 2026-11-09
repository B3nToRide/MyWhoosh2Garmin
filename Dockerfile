FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYWHOOSH_DATA_DIR=/data \
    LOG_TO_FILE=0 \
    TZ=Europe/Paris

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 syncuser \
    && useradd --uid 10001 --gid syncuser --no-create-home syncuser \
    && mkdir /data && chown syncuser:syncuser /data

# Shared with Termux: generated from the script's lock, including hashes.
COPY android/requirements.txt ./requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes -r requirements.txt

COPY myWhoosh2Garmin.py scheduler.py runtime.py ./
USER 10001:10001
ENTRYPOINT ["python", "/app/myWhoosh2Garmin.py"]
CMD ["--schedule"]
