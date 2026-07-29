FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖:
# - gcc/libsqlite3-dev/libpq-dev: chromadb/aiosqlite/asyncpg 编译需要
# - tesseract-ocr: OCR 功能 (pytesseract Python 封装需要系统二进制, 默认未启用 OCR)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libsqlite3-dev libpq-dev tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建非 root 用户
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
