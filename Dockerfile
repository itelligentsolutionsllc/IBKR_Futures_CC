# 1) Use a public, headless IB Gateway image
FROM ghcr.io/gnzsnz/ib-gateway:10.30.1v

# 2) Switch to root so we can install Python3 & pip
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

# 3) Copy your bot & install its deps
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

# 4) Launch your Python bot.
#    The base image already starts IB Gateway for you on port 7496.
CMD ["python3", "covered call strategy.py"]
