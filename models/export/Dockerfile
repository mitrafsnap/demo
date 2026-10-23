# Prepares ONNX models that need a step beyond a download: here, pinning a
# model's dynamic input shapes so CoreML and TensorRT can run it. Built by the
# controller at prepare time:
#   buildctl build ... --opt build-arg:MODEL=<name> --output type=local,dest=<dir>
FROM python:3.12-slim AS export
RUN pip install --no-cache-dir onnx==1.18.0 onnxruntime==1.22.1
COPY export.py /export.py
ARG MODEL
RUN python3 /export.py "$MODEL" /out

FROM scratch
COPY --from=export /out/ /
