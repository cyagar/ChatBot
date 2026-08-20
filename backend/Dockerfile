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
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TESSERACT_CMD=/usr/bin/tesseract
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
