# Hapilot sandbox image: Python data tools + LaTeX with Polish support.
# Built once by `scripts/hapilotctl.py setup`; Hapilot starts a fresh container from it for every
# code run or PDF compilation, with no network, as a non-root user, on a read-only root filesystem.
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-lang-polish \
        texlive-fonts-recommended \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        pandas \
        numpy \
        matplotlib \
        seaborn \
        scikit-learn \
        openpyxl \
        reportlab \
        fpdf \
        docxtpl

RUN mkdir -p /work
WORKDIR /work
CMD ["python3", "--version"]
