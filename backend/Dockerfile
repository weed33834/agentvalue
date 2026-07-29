# ──────────────────────────────────────────────────────────
# Stage 1: builder — 安装依赖（含编译工具链）
# ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# 安装编译期依赖: gcc 编译 C 扩展, libsqlite3-dev/libpq-dev 编译 DB 驱动
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libsqlite3-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ──────────────────────────────────────────────────────────
# Stage 2: runtime — 仅包含运行时依赖（不含 gcc 等编译工具）
# ──────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# 仅安装运行期系统依赖:
# - libsqlite3-0/libpq5: SQLite/PostgreSQL 客户端库运行时
# - tesseract-ocr: OCR 功能 (pytesseract Python 封装需要系统二进制)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsqlite3-0 libpq5 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 阶段复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 复制应用源码
COPY . .

# 创建非 root 用户
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
