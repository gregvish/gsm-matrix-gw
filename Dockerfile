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

RUN pip install --upgrade pip
RUN pip install av==8.0.3 --no-binary av
RUN pip install aiortc==1.2.1
RUN pip install "matrix-nio[e2e]"==0.18.6
RUN pip install pyserial-asyncio==0.5

RUN useradd -ms /bin/bash user
RUN addgroup user dialout
RUN addgroup user audio

USER user
WORKDIR /home/user/

COPY *.py ./
COPY asoundrc ./.asoundrc
