FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install pinned Bandit version. Bandit is static analysis, so the oracle gate
# needs nothing from the target repo's dependency tree - which is what keeps
# this image small. The tests gate is the part that would need that tree, and
# it is off by default; see config.RUN_TESTS_GATE.
RUN pip install --no-cache-dir bandit==1.7.9

# Create non-root user
RUN useradd -m -u 1000 appuser

# Create data directory
RUN mkdir -p /data && chown -R appuser:appuser /data

# Copy application code
COPY --chown=appuser:appuser . /app

# Set working directory
WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "main.py"]