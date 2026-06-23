FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV GST_DEBUG=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-rtsp-server-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-rtsp \
    v4l-utils \
    exfat-fuse \
    fuse \
    supervisor \
    udev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip3 install --break-system-packages \
    --extra-index-url https://www.piwheels.org/simple \
    -r /requirements.txt

COPY supervisord.conf /etc/supervisor/conf.d/stellar-recorder.conf
COPY supervisord_main.conf /etc/supervisord.conf
COPY app/ /app/
COPY config/ /config/

LABEL authors='[{"name": "Gavin Foster"}]' \
      company="African Robotics Unit" \
      description="Arm-triggered YUYV recorder and RTSP streamer for DWE StellarHD stereo cameras" \
      permissions='{"NetworkMode":"host",\
      "Privileged":true,\
      "HostConfig":{\
                    "Privileged":true,\
                    "NetworkMode":"host",\
                    "Binds":[\
                                "/dev:/dev","/media:/media",\
                                "/mnt:/mnt","/run/udev:/run/udev:ro"\
                                ]\
                    }\
       }' \
      type="tool" \
      tags='["camera","recording", "data-collection"]'

EXPOSE 7691 8554

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisord.conf"]
