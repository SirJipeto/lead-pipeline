FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory
RUN mkdir -p data

# Expose port
EXPOSE 5000

# Run with gunicorn for production
# Single worker is intentional — pipeline status is kept in process memory.
# Threads handle concurrency within the worker. Move to multi-worker only after
# moving pipeline status + credit state out of process memory (Redis/SQLite).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "300", "app:app"]
