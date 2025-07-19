# Use slim Python image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code
COPY . .

# Set env variables (optional default)
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "botTB.py"]
