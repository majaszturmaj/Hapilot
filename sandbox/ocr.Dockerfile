# Hapilot OCR image: Tesseract with the Polish and English models, for PDF pages without a text layer (src/ocr.py).
# Built once by `scripts/hapilotctl.py setup`; Hapilot runs it like the sandbox — a fresh container per document, no
# network, as a non-root user, on a read-only root filesystem, with only a temporary folder of page images at /work.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-pol \
        tesseract-ocr-eng \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# one thread per container (the container gets one CPU, see HAPILOT_SANDBOX_CPUS)
ENV OMP_THREAD_LIMIT=1
RUN mkdir -p /work
WORKDIR /work
CMD ["tesseract", "--version"]
