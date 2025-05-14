# 1) Use a public IB Gateway image that runs headlessly for you
#    (this one bundles IB Gateway 10.30.1v and auto‑starts it)
FROM ghcr.io/gnzsnz/ib-gateway:10.30.1v  #  [oai_citation:0‡GitHub](https://github.com/gnzsnz/ib-gateway-docker/pkgs/container/ib-gateway?utm_source=chatgpt.com)

# 2) Switch to root so we can install Python dependencies
USER root

# 3) Install pip if it’s not already present
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/*

# 4) Copy your bot code in and install its requirements
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

# 5) Launch your bot. The base image’s ENTRYPOINT already started IB Gateway for you.
CMD ["python3", "covered call strategy.py"]
