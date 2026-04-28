FROM python:3.12-slim
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ ./
COPY tools/ ./tools/
COPY db/migrations/ ./db/migrations/
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
