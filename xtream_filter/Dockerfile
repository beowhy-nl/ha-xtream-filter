# WireGuard-client die volledig in de container draait (geen extra rechten nodig).
FROM golang:1.26-alpine AS wireproxy
RUN CGO_ENABLED=0 go install -ldflags="-s -w" \
    github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3

FROM python:3.13-alpine
COPY --from=wireproxy /go/bin/wireproxy /usr/local/bin/wireproxy
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY app /opt/app
WORKDIR /opt
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-m", "app"]
