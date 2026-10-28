FROM python:3.12-alpine
WORKDIR /app
COPY server.py widgets.py sysstats.py monitor.py discover.py default-config.json ./
COPY public ./public
ENV PORT=3000 CONFIG_DIR=/app/config PYTHONUNBUFFERED=1
RUN adduser -D -u 1000 app && mkdir -p /app/config && chown app /app/config
USER app
VOLUME /app/config
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:3000/api/meta || exit 1
CMD ["python", "server.py"]
