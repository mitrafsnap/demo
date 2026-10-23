# Exports torchvision models to ONNX, for models no permissive ONNX download
# exists for. Built by the controller at prepare time:
#   buildctl build ... --opt build-arg:MODEL=<name> --output type=local,dest=<dir>
FROM python:3.12-slim AS export
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch==2.8.0 torchvision==0.23.0 && \
    pip install --no-cache-dir onnx==1.18.0
COPY export.py /export.py
ARG MODEL
RUN python3 /export.py "$MODEL" /out

FROM scratch
COPY --from=export /out/ /
