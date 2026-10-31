FROM python:3.12-slim

# Tesseract with Spanish data (accents matter: "Ramírez" != "Ramirez"), fonts for drawing marks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng fonts-dejavu-core libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agente ./agente

ENV PYTHONUNBUFFERED=1 \
    MEMORY_PATH=/app/data/memory.json
EXPOSE 8000
CMD ["sh", "-c", "uvicorn agente.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
