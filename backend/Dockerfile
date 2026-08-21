FROM python:3.12-slim

# python:3.12 rather than 3.14 here: this Dockerfile targets the documented
# production path (Linux container), independent of the 3.14 used for local
# Windows development in this repo's actual dev history. Pin whichever your
# CI has validated; both are supported by every dependency in requirements.txt.

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# torch's default index resolves large CUDA wheels even for this CPU-only
# service; install it from the CPU-only index first so the normal
# `pip install -r requirements.txt` below finds it already satisfied.
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Explicit copies only — .dockerignore also blocks .env/data/tests, but being
# explicit here means a future top-level file needs a deliberate opt-in
# instead of silently riding along on a `COPY . .`.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Bake the pinned embedding model into the image at build time so first use
# never depends on an unannounced runtime download from Hugging Face
# (independent review concern #18). Reads the pin from app/config.py (no
# .env present in the image) so there's one source of truth, not a value
# duplicated between config.py and this file.
ENV HF_HOME=/opt/hf-cache
RUN mkdir -p /opt/hf-cache && python -c "from app.config import get_settings; from sentence_transformers import SentenceTransformer; s = get_settings(); SentenceTransformer(s.embedding_model, revision=s.embedding_model_revision); print('Baked embedding model', s.embedding_model, s.embedding_model_revision)"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data /opt/hf-cache
USER appuser

ENV TESSERACT_CMD=/usr/bin/tesseract
ENV PYTHONUNBUFFERED=1
# The model is baked in above; if the pin and the cache ever disagree, fail
# loudly (see embeddings.py) instead of silently reaching out to the network.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
