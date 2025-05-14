# Use slim Python build
FROM python:3.10-slim

# Install Java & unzip
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless wget unzip \
 && rm -rf /var/lib/apt/lists/*

# Download & unpack IB Gateway ZIP (no wizard)
WORKDIR /opt/ibgw
RUN wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.zip \
 && unzip ibgateway-stable-standalone-linux-x64.zip \
 && rm ibgateway-stable-standalone-linux-x64.zip

# Copy in your bot code
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Copy the entrypoint and make it executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Kick off the gateway + bot
ENTRYPOINT ["/entrypoint.sh"]
CMD ["covered call strategy.py"]
