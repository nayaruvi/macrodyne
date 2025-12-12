# Use slim Python base
FROM python:3.11-slim

# Install system dependencies + Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtiff5 \
    libjpeg62-turbo \
    zlib1g \
    libfreetype6 \
    liblcms2-2 \
    libwebp7 \
    libharfbuzz0b \
    libopenjp2-7 \
    libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside container
WORKDIR /app

# Copy requirements file first
COPY requirements.txt .

# Install Python libraries
RUN pip install --no-cache-dir -r requirements.txt

# Copy all remaining project files
COPY . .

# Expose port (Render will use $PORT automatically)
EXPOSE 5000

# Start server with Gunicorn (Render sets $PORT env var)
CMD gunicorn server:app --bind 0.0.0.0:$PORT --workers 1
