FROM python:3.12-slim
WORKDIR /app
COPY server/ server/
ENV C2D_DATA=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "server/app.py"]
