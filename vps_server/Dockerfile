FROM python:3.11-slim
WORKDIR /app

# Зависимости
RUN pip install --no-cache-dir \
    fastapi>=0.110.0 \
    uvicorn[standard]>=0.29.0 \
    requests>=2.31.0 \
    beautifulsoup4>=4.12.0 \
    lxml>=5.1.0 \
    requests-toolbelt>=1.0.0

COPY server.py /app/main.py

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ping')"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
