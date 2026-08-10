# End-to-end test image: expecto + llama.cpp (llama-server) + a small
# GGUF model, with pytest running inside the container.
#
# Build from the repository root (the Makefile does this):
#   podman build -t expecto-e2e -f e2e-tests/Dockerfile .
#
# Layer order matters: llama.cpp compilation and the model download are
# expensive and change rarely, so they come first; the expecto source is
# copied last so code changes rebuild only the cheap layers.

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git curl ca-certificates \
        python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# llama.cpp, pinned to a release tag. GGML_NATIVE=OFF so the image runs
# on machines other than the one that built it; BUILD_SHARED_LIBS=OFF so
# the llama-server binary is self-contained.
ARG LLAMA_CPP_TAG=b10326
RUN git clone --depth 1 --branch ${LLAMA_CPP_TAG} \
        https://github.com/ggml-org/llama.cpp /tmp/llama.cpp \
    && cmake -S /tmp/llama.cpp -B /tmp/llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF \
    && cmake --build /tmp/llama.cpp/build --target llama-server -j"$(nproc)" \
    && install -m 0755 /tmp/llama.cpp/build/bin/llama-server /usr/local/bin/ \
    && rm -rf /tmp/llama.cpp

# The model. Small on purpose — the e2e tests need tool calling to work,
# not intelligence. Swap via --build-arg MODEL_URL=... when needed.
ARG MODEL_URL=https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q8_0.gguf
ENV MODEL_FILE=/opt/models/model.gguf
RUN mkdir -p /opt/models \
    && curl -fL --retry 3 -o "${MODEL_FILE}" "${MODEL_URL}"

# vim, for the scenario that drives a program which turns on
# modifyOtherKeys. Installed here rather than with the packages above so
# that adding it does not invalidate the two expensive layers.
RUN apt-get update && apt-get install -y --no-install-recommends vim \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pytest pytest-asyncio
ENV PATH=/opt/venv/bin:${PATH}

# expecto itself (copied last: see the note at the top).
COPY pyproject.toml /opt/expecto/
COPY src /opt/expecto/src
RUN /opt/venv/bin/pip install --no-cache-dir /opt/expecto
COPY e2e-tests /opt/expecto/e2e-tests

WORKDIR /opt/expecto
CMD ["pytest", "e2e-tests", "-v"]
