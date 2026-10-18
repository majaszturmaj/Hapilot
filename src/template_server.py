"""
MCP Server for LaTeX template-based PDF generation.

This server provides tools for:
- Listing available LaTeX templates and their parameters
- Generating PDF documents by filling templates with content
- Compiling LaTeX to PDF in the Docker sandbox

Replaces the old Coder-based approach where LLM had to write LaTeX from scratch.
Now Bielik only selects a template and provides parameter values.
"""

import os
import re
import json
import time
import base64
from datetime import datetime

import docker
from mcp.server.fastmcp import FastMCP

# --- KONFIGURACJA ---
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
TEMPLATES_DIR = os.path.abspath(TEMPLATES_DIR)
TEMPLATES_METADATA_FILE = os.path.join(TEMPLATES_DIR, 'templates.json')

# --- DOCKER SANDBOX ---
_docker_client = None
_cached_container = None
_cached_container_id = None


def _get_docker_client():
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
        except Exception as e:
            print(f"⚠️ [TEMPLATE SERVER] Docker niedostępny: {e}")
    return _docker_client


def _get_sandbox_container():
    """Find and cache the sandbox container."""
    global _cached_container, _cached_container_id
    client = _get_docker_client()
    if not client:
        return None

    # Check cache
    if _cached_container_id:
        try:
            c = client.containers.get(_cached_container_id)
            c.reload()
            if c.status in ('running', 'created', 'exited'):
                _cached_container = c
                return c
        except Exception:
            _cached_container_id = None
            _cached_container = None

    # Search by name
    for name in ["agent_sandbox", "python_sandbox"]:
        try:
            c = client.containers.get(name)
            _cached_container_id = c.id
            _cached_container = c
            return c
        except Exception:
            continue

    # Search by docker-compose label
    try:
        for c in client.containers.list(all=True):
            labels = c.labels or {}
            service = labels.get('com.docker.compose.service', '')
            if service in ('python_sandbox', 'agent_sandbox', 'sandbox'):
                _cached_container_id = c.id
                _cached_container = c
                return c
    except Exception:
        pass

    # Fuzzy match
    try:
        for c in client.containers.list(all=True):
            if any(x in c.name for x in ["sandbox", "python_sandbox", "agent_sandbox"]):
                _cached_container_id = c.id
                _cached_container = c
                return c
    except Exception:
        pass

    return None


def _resolve_sandbox_workspace():
    """Resolve host path to sandbox workspace."""
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sandbox', 'workspace')
    try:
        client = _get_docker_client()
        if client:
            for c in client.containers.list(all=True):
                if 'sandbox' in c.name.lower():
                    for mount in c.attrs.get('Mounts', []):
                        if mount.get('Destination') == '/workspace':
                            source = mount.get('Source', '')
                            if source and os.path.isdir(source):
                                return source
    except Exception:
        pass
    return os.path.abspath(default_path)


def _load_templates_metadata() -> dict:
    """Load templates metadata from JSON file."""
    try:
        with open(TEMPLATES_METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e), "templates": {}}


def _fill_template(template_content: str, parameters: dict) -> str:
    """Replace {{PARAM}} placeholders in template with actual values."""
    result = template_content
    for key, value in parameters.items():
        placeholder = "{{" + key + "}}"
        result = result.replace(placeholder, str(value))
    return result


def _generate_unique_filename(prefix: str = "dokument") -> str:
    """Generate unique filename with timestamp."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}"


def _compile_latex_in_sandbox(latex_content: str, filename_base: str) -> str:
    """Compile LaTeX content to PDF inside Docker sandbox.
    
    Returns success message with PDF path or error message.
    """
    container = _get_sandbox_container()
    if not container:
        return "BŁĄD: Sandbox nie działa. Sprawdź czy kontener jest uruchomiony (docker-compose up -d)."

    # Restart if stopped
    if container.status != 'running':
        try:
            container.start()
            time.sleep(2)
            container.reload()
        except Exception as e:
            return f"BŁĄD: Nie można uruchomić kontenera sandbox: {e}"

    tex_filename = f"{filename_base}.tex"
    pdf_filename = f"{filename_base}.pdf"

    try:
        # 1. Write .tex file via base64 (safe transfer)
        encoded = base64.b64encode(latex_content.encode('utf-8')).decode('ascii')
        write_cmd = f"echo '{encoded}' | base64 -d > /workspace/{tex_filename}"
        exec_write = container.exec_run(cmd=["sh", "-c", write_cmd], workdir="/workspace")
        if exec_write.exit_code != 0:
            return f"BŁĄD ZAPISU PLIKU TEX: {exec_write.output.decode('utf-8')}"

        # 2. Verify pdflatex exists
        which_result = container.exec_run(cmd=["sh", "-c", "which pdflatex || true"], workdir="/workspace")
        if not which_result.output.decode('utf-8').strip():
            return "BŁĄD: pdflatex nie zainstalowany w sandboxie."

        # 3. Compile (double pass for TOC/references)
        compile_cmd = (
            f"timeout 120 sh -c '"
            f"cd /workspace && "
            f"pdflatex -interaction=nonstopmode -halt-on-error {tex_filename} && "
            f"pdflatex -interaction=nonstopmode {tex_filename}'"
        )
        compile_result = container.exec_run(
            cmd=["sh", "-c", compile_cmd],
            workdir="/workspace"
        )

        # 4. Check if PDF was created
        check_cmd = f"test -f /workspace/{pdf_filename} && stat -c%s /workspace/{pdf_filename}"
        check_result = container.exec_run(cmd=["sh", "-c", check_cmd], workdir="/workspace")

        if check_result.exit_code == 0:
            pdf_size = check_result.output.decode('utf-8').strip()

            # 5. Cleanup auxiliary files
            aux_extensions = "aux log out toc nav snm vrb fls fdb_latexmk synctex.gz bbl blg"
            cleanup_cmd = f"cd /workspace && rm -f {' '.join(f'{filename_base}.{ext}' for ext in aux_extensions.split())}"
            container.exec_run(cmd=["sh", "-c", cleanup_cmd], workdir="/workspace")

            return f"SUKCES! Wygenerowano plik: {pdf_filename} ({pdf_size} bajtów)\n(Plik znajduje się w folderze sandbox/workspace/{pdf_filename})"
        else:
            # PDF not created - get diagnostic logs
            log_cmd = f"cat /workspace/{filename_base}.log 2>/dev/null || echo 'Brak pliku logu'"
            log_result = container.exec_run(cmd=["sh", "-c", log_cmd], workdir="/workspace")
            log_content = log_result.output.decode('utf-8', errors='ignore')

            error_lines = []
            lines = log_content.splitlines()
            for i, line in enumerate(lines):
                if line.startswith('!') or 'Error' in line or 'Fatal' in line:
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    error_lines.extend(lines[start:end])
                    error_lines.append('---')

            error_msg = "\n".join(error_lines[-40:]) if error_lines else "\n".join(lines[-30:])

            if compile_result.exit_code == 124:
                return f"BŁĄD KOMPILACJI: Timeout (>120s).\n{error_msg}"

            return f"BŁĄD KOMPILACJI (exit_code={compile_result.exit_code}):\n{error_msg}"

    except Exception as e:
        return f"BŁĄD DOCKERA: {str(e)}"


# --- MCP SERVER ---
mcp = FastMCP("LaTeX Template Server")


@mcp.tool()
def list_templates() -> str:
    """
    Zwraca listę dostępnych szablonów LaTeX z ich opisami i parametrami.
    Użyj tego narzędzia, aby sprawdzić jakie szablony są dostępne
    i jakie parametry wymagają do wypełnienia.
    
    Returns:
        JSON z listą szablonów, ich opisami i wymaganymi parametrami.
    """
    metadata = _load_templates_metadata()
    if "error" in metadata and not metadata.get("templates"):
        return f"BŁĄD: Nie można wczytać metadanych szablonów: {metadata['error']}"

    templates = metadata.get("templates", {})
    result = []
    for template_id, info in templates.items():
        params_summary = {}
        for param_name, param_info in info.get("parameters", {}).items():
            params_summary[param_name] = {
                "opis": param_info["description"],
                "przykład": param_info.get("example", ""),
                "wymagany": param_info.get("required", True)
            }
        result.append({
            "id": template_id,
            "nazwa": info["name"],
            "opis": info["description"],
            "parametry": params_summary
        })

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def generate_pdf_from_template(template_id: str, parameters_json: str) -> str:
    """
    Generuje dokument PDF na podstawie wybranego szablonu LaTeX i podanych parametrów.
    
    Workflow:
    1. Wczytuje szablon z dysku
    2. Wypełnia placeholdery {{PARAM}} wartościami z parameters_json
    3. Kompiluje LaTeX do PDF w sandboxie Docker
    4. Zwraca ścieżkę do wygenerowanego PDF
    
    Args:
        template_id: Identyfikator szablonu (np. "letter", "report", "protocol").
        parameters_json: JSON string z wartościami parametrów szablonu.
                        Klucze to nazwy parametrów (np. "SENDER_NAME", "BODY"),
                        wartości to treści do wstawienia.
    
    Returns:
        Komunikat o sukcesie z nazwą pliku PDF lub komunikat o błędzie.
    """
    # 1. Load metadata
    metadata = _load_templates_metadata()
    templates = metadata.get("templates", {})

    if template_id not in templates:
        available = ", ".join(templates.keys())
        return f"BŁĄD: Nieznany szablon '{template_id}'. Dostępne szablony: {available}"

    template_info = templates[template_id]
    template_file = os.path.join(TEMPLATES_DIR, template_info["file"])

    if not os.path.exists(template_file):
        return f"BŁĄD: Plik szablonu nie istnieje: {template_info['file']}"

    # 2. Parse parameters
    try:
        if isinstance(parameters_json, str):
            params = json.loads(parameters_json)
        else:
            params = parameters_json
    except json.JSONDecodeError as e:
        return f"BŁĄD: Nieprawidłowy JSON parametrów: {e}"

    # 3. Validate required parameters
    missing = []
    for param_name, param_info in template_info.get("parameters", {}).items():
        if param_info.get("required", True) and param_name not in params:
            missing.append(f"{param_name} ({param_info['description']})")

    if missing:
        return f"BŁĄD: Brakujące wymagane parametry:\n" + "\n".join(f"  - {m}" for m in missing)

    # 4. Load template and fill parameters
    try:
        with open(template_file, 'r', encoding='utf-8') as f:
            template_content = f.read()
    except Exception as e:
        return f"BŁĄD: Nie można wczytać szablonu: {e}"

    filled_content = _fill_template(template_content, params)

    # 5. Check for unfilled placeholders
    unfilled = re.findall(r'\{\{([A-Z_]+)\}\}', filled_content)
    if unfilled:
        # Fill unfilled optional params with empty string
        for placeholder in unfilled:
            param_info = template_info.get("parameters", {}).get(placeholder, {})
            if not param_info.get("required", True):
                filled_content = filled_content.replace("{{" + placeholder + "}}", "")
            # If required but unfilled, it was already caught above

    # 6. Generate unique filename and compile
    filename_base = _generate_unique_filename(prefix=template_id)
    print(f"📄 [TEMPLATE SERVER] Generowanie PDF z szablonu '{template_id}': {filename_base}.pdf")

    result = _compile_latex_in_sandbox(filled_content, filename_base)
    return result


@mcp.tool()
def get_template_details(template_id: str) -> str:
    """
    Zwraca szczegółowe informacje o konkretnym szablonie,
    w tym pełną listę parametrów z opisami i przykładami.
    
    Args:
        template_id: Identyfikator szablonu (np. "letter", "report", "protocol").
    
    Returns:
        JSON ze szczegółami szablonu.
    """
    metadata = _load_templates_metadata()
    templates = metadata.get("templates", {})

    if template_id not in templates:
        available = ", ".join(templates.keys())
        return f"BŁĄD: Nieznany szablon '{template_id}'. Dostępne: {available}"

    info = templates[template_id]
    result = {
        "id": template_id,
        "nazwa": info["name"],
        "opis": info["description"],
        "plik": info["file"],
        "parametry": {}
    }

    for param_name, param_info in info.get("parameters", {}).items():
        result["parametry"][param_name] = {
            "opis": param_info["description"],
            "przykład": param_info.get("example", ""),
            "wymagany": param_info.get("required", True)
        }

    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
