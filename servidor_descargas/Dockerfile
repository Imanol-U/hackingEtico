FROM python:3.12-alpine
WORKDIR /app
COPY server.py .
ENV PYTHONUNBUFFERED=1 FILES_DIR=/data PORT=8000
RUN adduser -D app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
CMD ["python", "server.py"]
