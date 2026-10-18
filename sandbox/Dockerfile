# sandbox/Dockerfile
FROM python:3.10-slim

WORKDIR /workspace

# Opcja --no-install-recommends jest KLUCZOWA - nie instaluje śmieci (dokumentacji, zbędnych pakietów)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    # Podstawowy LaTeX
    texlive-latex-base \
    texlive-latex-recommended \
    # Dodatki (tabelki, grafika - niezbędne do raportów)
    texlive-latex-extra \
    # Polskie znaki i czcionki
    texlive-lang-polish \
    texlive-fonts-recommended \
    # Narzędzie do czyszczenia po instalacji (zmniejsza obraz)
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 3. Instalujemy popularne biblioteki Data Science / Utils
# To jest nasz "Cache", żeby kod wykonywał się natychmiast
RUN pip install --no-cache-dir \
    pandas \
    numpy \
    matplotlib \
    seaborn \
    scikit-learn \
    requests \
    openpyxl \
    reportlab \
    fpdf \
    docxtpl

# 4. Kluczowa komenda: Kontener ma "wisieć" i czekać na polecenia
CMD ["tail", "-f", "/dev/null"]
