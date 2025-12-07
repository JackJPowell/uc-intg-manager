FROM python:3.11-slim-bullseye

WORKDIR /app

COPY ./requirements.txt requirements.txt
RUN pip3 install --no-cache-dir --upgrade -r requirements.txt
RUN mkdir /config

ADD . .

# Network configuration
ENV UC_DISABLE_MDNS_PUBLISH="false"
ENV UC_MDNS_LOCAL_HOSTNAME=""
ENV UC_INTEGRATION_INTERFACE="0.0.0.0"
ENV UC_INTEGRATION_HTTP_PORT="9090"

# Configuration path
ENV UC_CONFIG_HOME="/config"

# TODO: Update the image source URL
LABEL org.opencontainers.image.source https://github.com/yourusername/uc-intg-yourdevice

# TODO: Update the path if you rename the intg-template folder
CMD ["python3", "-u", "intg-template/driver.py"]
