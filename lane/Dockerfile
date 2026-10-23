# Runtime base shared by every lane image. Model files are added on top by each
# method's builder (Docker layers or 2DFS allotments), never baked in here.
# DEVICE=cuda installs CUDA-enabled ONNX Runtime with its CUDA and cuDNN wheels.
FROM python:3.12-slim
ARG DEVICE=cpu
RUN if [ "$DEVICE" = cuda ]; then ort="onnxruntime-gpu[cuda,cudnn]==1.31.0"; \
    else ort="onnxruntime==1.22.1"; fi && \
    pip install --no-cache-dir "$ort" opencv-python-headless==4.12.0.88 numpy==2.2.6
COPY app.py vision.py /app/
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DEVICE=$DEVICE
ENTRYPOINT ["python3", "/app/app.py"]
