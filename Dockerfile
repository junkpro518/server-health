FROM python:3.12-alpine
RUN apk add --no-cache bash docker-cli curl
WORKDIR /app
COPY monitor.py .
CMD ["python", "-u", "monitor.py"]
