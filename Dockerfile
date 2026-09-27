FROM alpine:3.22

RUN apk add --no-cache \
    python3 \
    ffmpeg

WORKDIR /app

COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/server.py"]
