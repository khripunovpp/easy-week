# Easy Week backend (FastAPI + Uvicorn)
FROM python:3.12-slim

WORKDIR /app

# Шрифт DejaVu — для кириллицы в PDF (fpdf2)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
