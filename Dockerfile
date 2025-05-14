# Use a slim Python image
FROM python:3.10-slim

# 1) Set working dir
WORKDIR /app

# 2) Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the rest of your code
COPY . .

# 4) Run your bot
CMD ["python", "covered call strategy.py"]
