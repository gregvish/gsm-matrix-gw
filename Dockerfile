FROM ubuntu:20.04

RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
    pkg-config \
    python3 \
    python3-pip \
    libpython3-dev \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    libqmi-utils \
    libolm-dev

RUN pip install pip==22.0.4
RUN pip install av==9.1.1 --no-binary av
RUN pip install aiortc==1.3.1
RUN pip install "matrix-nio[e2e]"==0.19.0
RUN pip install pyserial-asyncio==0.6

RUN useradd -ms /bin/bash user
RUN addgroup user dialout
RUN addgroup user audio

USER user
WORKDIR /home/user/

COPY *.py ./
COPY asoundrc ./.asoundrc
