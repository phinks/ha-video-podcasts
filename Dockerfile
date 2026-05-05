# build v2
FROM alpine:3.21

RUN apk update && apk add --no-cache python3 py3-pip ffmpeg && rm -rf /var/cache/apk/*

COPY app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/bin/sh", "/run.sh"]
