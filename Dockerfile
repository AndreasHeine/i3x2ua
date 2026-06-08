FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build tooling for dependencies that may require native compilation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cargo \
        libssl-dev \
        pkg-config \
        rustc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY . /app

RUN uv sync --no-dev

EXPOSE 8000

ENTRYPOINT ["python", "run.py"]
