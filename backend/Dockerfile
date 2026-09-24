FROM python:3.14-slim

# The container targets Linux on Python 3.12; local development may use a newer Python.

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

# Explicit copies only, so a new top-level file must be added deliberately.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Bake the pinned embedding model into the image so first use never depends on
# a runtime download. The pin is read from app/config.py, the single source of truth.
ENV HF_HOME=/opt/hf-cache
RUN mkdir -p /opt/hf-cache && python -c "from app.config import get_settings; from sentence_transformers import SentenceTransformer; s = get_settings(); SentenceTransformer(s.embedding_model, revision=s.embedding_model_revision); print('Baked embedding model', s.embedding_model, s.embedding_model_revision)"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data /opt/hf-cache
USER appuser

ENV TESSERACT_CMD=/usr/bin/tesseract
ENV PYTHONUNBUFFERED=1
# The model is baked in above; a pin/cache mismatch fails loudly (see
# embeddings.py) instead of reaching out to the network.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

# Keep-alive above the fronting proxy's idle timeout (Cloud Run / Google
# frontend hold backend connections ~10 minutes): with uvicorn's 5s default the
# container closes idle connections first, and the next request reusing one
# fails with a connection reset instead of being retried.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "650"]
