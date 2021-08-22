#!/bin/bash
docker build -t gsm-matrix-gw .

docker kill --signal=SIGINT gsm-matrix-gw-container
docker wait gsm-matrix-gw-container
docker rm -f gsm-matrix-gw-container

docker run -d \
    --name gsm-matrix-gw-container \
    --net host \
    --privileged \
    -v /dev:/dev \
    -v /proc:/porc \
    -v /sys:/sys \
    --restart unless-stopped \
    gsm-matrix-gw:latest \
    python3 gw.py "$@"

docker logs -f gsm-matrix-gw-container
