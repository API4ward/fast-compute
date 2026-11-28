# Lambda image that behaves like the job image python:3.12-slim, plus the Lambda runtime
# interface client (kept out of the job's Python path). Build for the function's architecture:
#   docker build --platform linux/amd64 --provenance=false -t fast-compute-lambda .
# deploy.sh does this for you.
ARG BASE=python:3.12-slim
FROM ${BASE}
RUN pip install --no-cache-dir --target /opt/fc/rt awslambdaric==4.2.3
COPY handler.py /opt/fc/handler.py
WORKDIR /opt/fc
ENV PYTHONPATH=/opt/fc/rt:/opt/fc
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["handler.handler"]
