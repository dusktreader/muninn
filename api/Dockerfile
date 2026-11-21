FROM python:3.13-slim

WORKDIR /app/api

COPY api ./

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ffmpeg tzdata util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && uv pip install --system --no-cache .

EXPOSE 5000

CMD ["uvicorn", "muninn.api:app", "--host=0.0.0.0", "--port=5000"]
