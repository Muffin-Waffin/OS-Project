FROM python:3.12-slim

# Set environment variables to prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Install curl for health checking (optional but good practice)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Create a non-privileged user and group for runtime isolation and security
RUN groupadd -g 10001 shellgroup && \
    useradd -u 10001 -g shellgroup -d /app -s /bin/bash shelluser && \
    chown -R shelluser:shellgroup /app

# Switch to the non-root user
USER shelluser

# Expose the application port
EXPOSE 5000

# Start the application using Gunicorn with threaded workers to support Server-Sent Events (SSE)
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "app:app"]
