# Runtime base shared by every lane image. Model files are added on top by each
# method's builder (Docker layers or 2DFS allotments), never baked in here.
FROM python:3.12-slim
RUN pip install --no-cache-dir onnxruntime==1.22.1 opencv-python-headless==4.12.0.88 numpy==2.2.6
COPY app.py vision.py /app/
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/app.py"]
