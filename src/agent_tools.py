import os
import httpx
import chromadb
import docker
import re
import time
import json
import base64
import glob
from datetime import datetime
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from dotenv import load_dotenv
from src.llm_config import get_bielik_chat, DOCUMENT_COMPOSITION_STANDARDS
from langchain_core.messages import HumanMessage, SystemMessage

# --- KONFIGURACJA SZABLONÓW LaTeX ---
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
TEMPLATES_DIR = os.path.abspath(TEMPLATES_DIR)
TEMPLATES_METADATA_FILE = os.path.join(TEMPLATES_DIR, 'templates.json')

# --- KONFIGURACJA ŚCIEŻEK ---
# Próbujemy dynamicznie znaleźć ścieżkę workspace z Docker mount
def _resolve_sandbox_workspace():
    """Rozwiąż faktyczną ścieżkę host sandbox/workspace z Docker bind mount."""
    # Domyślna ścieżka
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sandbox', 'workspace')
    try:
        import docker as _docker
        client = _docker.from_env()
        # Szukamy kontenera sandbox
        for c in client.containers.list(all=True):
            if 'sandbox' in c.name.lower() and 'python' in c.name.lower():
                for mount in c.attrs.get('Mounts', []):
                    if mount.get('Destination') == '/workspace':
                        source = mount.get('Source', '')
                        if source and os.path.isdir(source):
                            return source
    except Exception:
        pass
    return os.path.abspath(default_path)

SANDBOX_WORKSPACE = _resolve_sandbox_workspace()
print(f"📂 [CONFIG] Sandbox workspace: {SANDBOX_WORKSPACE}")

load_dotenv()

# --- KONFIGURACJA ---
CHROMA_HOST = "localhost"
CHROMA_PORT = 8000
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "company_knowledge")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "intfloat/multilingual-e5-base")
N8N_WEBHOOK_BASE = "http://localhost:5679/webhook"
STATE_PERSISTENCE_DIR = os.getenv("STATE_PERSISTENCE_DIR", "./task_states")

# --- TASK STATE PERSISTENCE ---
os.makedirs(STATE_PERSISTENCE_DIR, exist_ok=True)

class TaskState:
    """Stanowy container dla zadań wymagających persistencji."""
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.state_file = os.path.join(STATE_PERSISTENCE_DIR, f"{task_id}.json")
        self.data = self._load_state() or {
            "task_id": task_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "steps_completed": [],
            "current_step": 0,
            "context": {},
            "outputs": {}
        }

    def _load_state(self) -> dict:
        """Wczytaj stan z pliku."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return None
        return None

    def save(self):
        """Zapisz stan do pliku."""
        self.data["updated_at"] = datetime.now().isoformat()
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_step(self, step_id: int, output: str):
        """Oznacz krok jako ukończony."""
        self.data["steps_completed"].append(step_id)
        self.data["current_step"] = step_id + 1
        self.data["outputs"][f"step_{step_id}"] = output
        self.save()

    def update_context(self, key: str, value):
        """Zapamiętaj informacje dla następnych kroków."""
        self.data["context"][key] = value
        self.save()

    def get_context(self, key: str = None):
        """Pobierz zapamiętane dane."""
        if key:
            return self.data["context"].get(key)
        return self.data["context"]

    def cleanup(self):
        """Usuń stan po ukończeniu."""
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

# --- KLIENT DOCKERA ---
try:
    docker_client = docker.from_env()
    print("✅ [TOOLS] Połączono z Dockerem lokalnym")
except Exception as e:
    print(f"⚠️ [TOOLS] Błąd połączenia z Dockerem: {e}")
    docker_client = None


# Cache for sandbox container to avoid repeated lookups
_cached_sandbox_container = None
_cached_sandbox_id = None

def get_sandbox_container() -> 'docker.models.containers.Container':
    """Znajdź sandbox container dynamicznie z cache'owaniem.
    
    Strategia wyszukiwania (od najbardziej do najmniej precyzyjnej):
    1. Cache - jeśli kontener nadal istnieje i działa
    2. Dokładna nazwa kontenera
    3. Label docker-compose (com.docker.compose.service=python_sandbox)
    4. Fuzzy match po nazwie
    """
    global _cached_sandbox_container, _cached_sandbox_id
    if not docker_client:
        return None
    
    # 0. Sprawdź cache
    if _cached_sandbox_id:
        try:
            c = docker_client.containers.get(_cached_sandbox_id)
            c.reload()
            if c.status in ('running', 'created', 'exited'):
                _cached_sandbox_container = c
                return c
        except Exception:
            _cached_sandbox_id = None
            _cached_sandbox_container = None
    
    # 1. Szukaj po dokładnej nazwie
    candidates = ["agent_sandbox", "python_sandbox"]
    for name in candidates:
        try:
            c = docker_client.containers.get(name)
            _cached_sandbox_id = c.id
            _cached_sandbox_container = c
            return c
        except Exception:
            continue

    # 2. Szukaj po docker-compose service label
    try:
        for c in docker_client.containers.list(all=True):
            labels = c.labels or {}
            service = labels.get('com.docker.compose.service', '')
            if service in ('python_sandbox', 'agent_sandbox', 'sandbox'):
                _cached_sandbox_id = c.id
                _cached_sandbox_container = c
                print(f"✅ [SANDBOX] Znaleziono kontener po label: {c.name} (service={service})")
                return c
    except Exception:
        pass

    # 3. Fuzzy match po nazwie (fallback)
    try:
        for c in docker_client.containers.list(all=True):
            if any(x in c.name for x in ["sandbox", "python_sandbox", "agent_sandbox"]):
                _cached_sandbox_id = c.id
                _cached_sandbox_container = c
                print(f"✅ [SANDBOX] Znaleziono kontener po nazwie: {c.name}")
                return c
    except Exception:
        pass

    print("⚠️ [SANDBOX] Nie znaleziono kontenera sandbox!")
    return None

# --- RAG SETUP (LAZY) ---
print(f"⚙️ [TOOLS] RAG lazy initialization configured (model={EMBEDDING_MODEL})")
_rag_initialized = False
embedding_function = None
chroma_client = None
vector_store = None

def ensure_rag_initialized():
    global _rag_initialized, embedding_function, chroma_client, vector_store
    if _rag_initialized:
        return
    try:
        print(f"⚙️ [TOOLS] Initializing RAG model: {EMBEDDING_MODEL}...")
        embedding_function = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={'device': 'cpu'},  # Force CPU to avoid CUDA OOM when LM Studio uses GPU
            encode_kwargs={'normalize_embeddings': True}
        )

        chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        vector_store = Chroma(
            client=chroma_client,
            collection_name=COLLECTION_NAME,
            embedding_function=embedding_function,
        )
        _rag_initialized = True
    except Exception as e:
        print(f"⚠️ [TOOLS] Błąd inicjalizacji RAG: {e}")
        _rag_initialized = False

# --- FUNKCJE NARZĘDZIOWE ---

def tool_rag_search(query: str) -> str:
    print(f"🔎 [TOOL USE] RAG Search: '{query}'")
    # Lazy-init RAG resources
    ensure_rag_initialized()
    if not _rag_initialized or vector_store is None:
        return "Błąd RAG: brak zainicjalizowanego vector_store"

    search_query = f"query: {query}" if "e5" in EMBEDDING_MODEL.lower() and not query.startswith("query:") else query
    try:
        results = vector_store.similarity_search(search_query, k=3)
        if not results: return "Brak wyników."
        return "\n".join([f"--- ŹRÓDŁO: {os.path.basename(d.metadata.get('source', ''))} ---\n{d.page_content}" for d in results])
    except Exception as e:
        return f"Błąd RAG: {e}"

def tool_check_calendar(date: str = "2024-05-20") -> str:
    print(f"📅 [TOOL USE] N8N Calendar: {date}")
    try:
        resp = httpx.post(f"{N8N_WEBHOOK_BASE}/outlook-calendar", json={"date": date}, timeout=3.0)
        return str(resp.json().get("events", "Kalendarz pusty."))
    except Exception as e:
        return f"Błąd N8N: {e}"

def tool_send_email_draft(recipient: str, subject: str, body: str) -> str:
    print(f"📧 [TOOL USE] N8N Email: {recipient}")
    try:
        resp = httpx.post(f"{N8N_WEBHOOK_BASE}/outlook-draft", json={"to": recipient, "subject": subject, "body": body}, timeout=5.0)
        return f"Draft ID: {resp.json().get('draft_id')}"
    except Exception as e:
        return f"Błąd N8N: {e}"

def tool_plan_task(query: str, plan_mode: str = "auto", history_context: str = "") -> str:
    """
    Generuje strukturalny plan wykonania zadania (STORM-like).
    Zwraca JSON z krokami, narzędziami i zależnościami.
    """
    print(f"📋 [TOOL USE] Task Planning (mode={plan_mode}): '{query[:50]}...'")
    
    llm, system_prompt = get_bielik_chat(role="task_planner")
    
    prompt = f"""Przeanalizuj to zapytanie i wygeneruj strukturalny plan wykonania.
ZAWSZE ROZBIJAJ ZADANIA NA MAŁE, NIEZALEŻNE KROKI!

{f"HISTORIA OSTATNICH AKCJI I DOKUMENTÓW (jako kontekst, nie odpowiadaj na to wprost):{chr(10)}{history_context}{chr(10)}" if history_context else ""}
ZAPYTANIE: "{query}"

GENERUJ ODPOWIEDŹ W JSON Z PONIŻSZĄ STRUKTURĄ:
{{
  "task_name": "krótka nazwa zadania",
  "complexity": "low|medium|high",
  "steps": [
    {{"id": 1, "action": "opis akcji", "tool": "rag_search|python|template_pdf|llm_synthesis|calendar|email|chat", "input": "konkretny input"}},
    {{"id": 2, "action": "opis akcji", "tool": "...", "input": "..."}}
  ],
  "dependencies": [[2, 1], [3, 1]],
  "requires_state_persistence": true|false,
  "estimated_tokens": liczba
}}

DOSTĘPNE NARZĘDZIA:
- rag_search: Wyszukiwanie w dokumentach firmowych (RAG)
- llm_synthesis: Generowanie/synteza tekstu przez Bielika
- template_pdf: Generowanie PDF z szablonu LaTeX (list, raport, protokół). System AUTOMATYCZNIE poprosi użytkownika o potwierdzenie parametrów (HITL).
- modify_document: Modyfikacja wcześniej wygenerowanego dokumentu LaTeX (zmiana daty, adresu, treści) i rekompilacja do PDF
- python: Wykonanie kodu Python w sandboxie
- calendar: Sprawdzenie kalendarza
- email: Wysłanie maila
- chat: Zwykła rozmowa z użytkownikiem (luźne pytania, powitania, zgoda/odmowa)
- hitl_confirm: Zatrzymaj się i poproś użytkownika o potwierdzenie/decyzję (np. przed wysyłką maila). NIE dodawaj przed template_pdf — to jest automatyczne.

PRZYKŁADY WIELOETAPOWYCH ZADAŃ (ZWRACAJ MINIMUM 2 KROKI):
1. "Zredakuj mail do pani Kasi i zrób to jako PDF"
   - Krok 1: Bielik generuje treść maila (llm_synthesis)
   - Krok 2: Wypełnij szablon listu i wygeneruj PDF (template_pdf)
   
2. "Co robić w razie pożaru i napisz o tym raport PDF"
   - Krok 1: RAG szuka procedury (rag_search)
   - Krok 2: Bielik syntetyzuje odpowiedź (llm_synthesis)
   - Krok 3: Wypełnij szablon raportu i wygeneruj PDF (template_pdf)

3. "Napisz podanie o urlop"
   - Krok 1: Bielik generuje treść i parametry (llm_synthesis)
   - Krok 2: Wypełnij szablon listu i wygeneruj PDF (template_pdf)

4. "Jak tam dzień minął?"
   - Krok 1: Odpowiedz na czacie (chat)

PRZYKŁADY MODYFIKACJI DOKUMENTÓW (1 KROK):
4. "Zmień datę na 7 marca 2026"
   - Krok 1: Zmodyfikuj dokument (modify_document) z opisem: "Zmień datę na Wrocław, 7 marca 2026"

5. "Popraw adres na ul. Zmyślona 7 i zmień nazwę firmy"
   - Krok 1: Zmodyfikuj dokument (modify_document) z pełnym opisem zmian

6. "Dodaj nowy akapit o bezpieczeństwie i zmień tytuł"
   - Krok 1: Zmodyfikuj dokument (modify_document)

INSTRUKCJE:
- Gdy użytkownik prosi o MODYFIKACJĘ istniejącego dokumentu (zmień, popraw, zaktualizuj, dodaj, usuń) -> UŻYJ modify_document
- Jeśli zapytanie to zwykła odpowiedź, kontynuacja konwersacji (np. "krócej", "napisz to prościej", "nie, dzięki", "jak się masz") -> OMIŃ rag_search i od razu użyj `chat` lub `llm_synthesis`. Dla samej rozmowy wybieraj `chat`.
- Jeśli pytanie wymaga NAJPIERW znalezienia informacji POTEM napisania czegoś -> MINIMUM 2 KROKI
- Jeśli pytanie wymaga NAPISANIA dokumentu (email, PDF, LaTeX) PRZYNAJMNIEJ z RAG -> MINIMUM 2 KROKI
- Jeśli to tylko RAG lub tylko generacja -> 1 KROK
- Dla nowych dokumentów PDF ZAWSZE używaj narzędzia template_pdf (NIE coder/latex)
- RAG jest kontekstem pomocniczym. Brak wyników RAG NIE może blokować planu generowania PDF.
- Jeśli użytkownik podał treść merytoryczną dokumentu (np. temat raportu), preferuj llm_synthesis -> template_pdf; rag_search dodawaj opcjonalnie.
- Nie zmieniaj typu dokumentu (report/protocol/letter) tylko dlatego, że RAG nic nie znalazł.
- Zawsze definiuj zależności między krokami!
"""

    try:
        response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
        # Spróbuj wyciągnąć JSON z odpowiedzi
        json_match = re.search(r'\{.*\}', response.content, re.DOTALL)
        if json_match:
            return json_match.group(0)
        return response.content
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "task_name": query[:30],
            "complexity": "medium",
            "steps": [{"id": 1, "action": "fallback", "tool": "template_pdf", "input": query}]
        })


# --- NARZĘDZIA SZABLONÓW LaTeX ---

def _load_templates_metadata() -> dict:
    """Wczytaj metadane szablonów z pliku JSON."""
    try:
        with open(TEMPLATES_METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e), "templates": {}}


def _sanitize_latex_value(value: str) -> str:
    """Escape LaTeX special characters in a parameter value while preserving
    intentional LaTeX commands (\\begin, \\end, \\item, \\textbf, \\\\, etc.).

    Strategy:
    1. Temporarily replace known LaTeX commands with placeholders
    2. Escape bare special characters: _ & % # $
    3. Restore the LaTeX commands from placeholders
    """
    if not value:
        return value

    # Known LaTeX command patterns to preserve (order matters)
    PRESERVED_PATTERNS = [
        (r'\\begin\{[^}]+\}', '__LATEX_BEGIN_{}__'),
        (r'\\end\{[^}]+\}', '__LATEX_END_{}__'),
        (r'\\item\b', '__LATEX_ITEM__'),
        (r'\\\\', '__LATEX_DBLBS__'),
        (r'\\textbf\{[^}]*\}', '__LATEX_TEXTBF_{}__'),
        (r'\\textit\{[^}]*\}', '__LATEX_TEXTIT_{}__'),
        (r'\\emph\{[^}]*\}', '__LATEX_EMPH_{}__'),
        (r'\\underline\{[^}]*\}', '__LATEX_UNDERLINE_{}__'),
        (r'\\hfill\b', '__LATEX_HFILL__'),
        (r'\\vskip\s+[\d.]+\s*\w+', '__LATEX_VSKIP_{}__'),
        (r'\\noindent\b', '__LATEX_NOINDENT__'),
        (r'\\newline\b', '__LATEX_NEWLINE__'),
        (r'\\par\b', '__LATEX_PAR__'),
        (r'\\Large\b', '__LATEX_LARGE__'),
        (r'\\large\b', '__LATEX_LOWERL__'),
        (r'\\small\b', '__LATEX_SMALL__'),
        (r'\\footnotesize\b', '__LATEX_FNSIZE__'),
        (r'\\phantom\{[^}]*\}', '__LATEX_PHANTOM_{}__'),
        (r'\\hspace\{[^}]*\}', '__LATEX_HSPACE_{}__'),
        (r'\\vspace\{[^}]*\}', '__LATEX_VSPACE_{}__'),
        (r'\\section\{[^}]*\}', '__LATEX_SECTION_{}__'),
        (r'\\subsection\{[^}]*\}', '__LATEX_SUBSECTION_{}__'),
    ]

    # Step 1: Protect known LaTeX commands
    preserved = []
    result = value
    for pattern, placeholder_tmpl in PRESERVED_PATTERNS:
        matches = list(re.finditer(pattern, result))
        for i, m in enumerate(reversed(matches)):
            idx = len(preserved)
            placeholder = placeholder_tmpl.replace('{}', str(idx))
            preserved.append((placeholder, m.group(0)))
            result = result[:m.start()] + placeholder + result[m.end():]

    # Step 2: Escape special characters that break LaTeX
    # Order matters: backslash-dependent ones first
    # Don't escape { } as they may be part of preserved patterns or intentional grouping
    result = result.replace('&', r'\&')
    result = result.replace('%', r'\%')
    result = result.replace('#', r'\#')
    result = result.replace('$', r'\$')
    # Escape underscores only if not already escaped
    result = re.sub(r'(?<!\\)_', r'\\_', result)
    # Escape ~ and ^ only as bare characters (not in commands)
    result = re.sub(r'(?<!\\)~', r'\textasciitilde{}', result)
    result = re.sub(r'(?<!\\)\^', r'\textasciicircum{}', result)

    # Step 3: Restore preserved LaTeX commands
    for placeholder, original in reversed(preserved):
        result = result.replace(placeholder, original)

    return result


def _fill_template(template_content: str, parameters: dict) -> str:
    """Wypełnij placeholdery {{PARAM}} w szablonie wartościami.

    Parameter values are sanitized to escape special LaTeX characters
    while preserving intentional LaTeX commands.
    """
    result = template_content
    for key, value in parameters.items():
        placeholder = "{{" + key + "}}"
        sanitized_value = _sanitize_latex_value(str(value))
        result = result.replace(placeholder, sanitized_value)
    return result


def tool_list_templates() -> str:
    """Zwraca listę dostępnych szablonów LaTeX z opisami i parametrami."""
    print("📋 [TOOL USE] Listing LaTeX templates")
    metadata = _load_templates_metadata()
    if "error" in metadata and not metadata.get("templates"):
        return f"BŁĄD: Nie można wczytać szablonów: {metadata['error']}"

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


def tool_generate_pdf_from_template(template_id: str, parameters: dict) -> str:
    """
    Generuje PDF na podstawie szablonu LaTeX.
    
    1. Wczytuje szablon z dysku
    2. Wypełnia placeholdery {{PARAM}} wartościami
    3. Kompiluje LaTeX do PDF w sandboxie Docker
    
    Args:
        template_id: ID szablonu (letter, report, protocol)
        parameters: Słownik z wartościami parametrów szablonu
    
    Returns:
        Komunikat o sukcesie lub błędzie
    """
    print(f"📄 [TOOL USE] Generate PDF from template: {template_id}")
    
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
    
    # 2. Validate required parameters
    missing = []
    for param_name, param_info in template_info.get("parameters", {}).items():
        if param_info.get("required", True) and param_name not in parameters:
            missing.append(f"{param_name} ({param_info['description']})")
    
    if missing:
        return f"BŁĄD: Brakujące wymagane parametry:\n" + "\n".join(f"  - {m}" for m in missing)
    
    # 3. Load and fill template
    try:
        with open(template_file, 'r', encoding='utf-8') as f:
            template_content = f.read()
    except Exception as e:
        return f"BŁĄD: Nie można wczytać szablonu: {e}"
    
    filled_content = _fill_template(template_content, parameters)
    
    # 4. Fill unfilled optional params with empty string
    unfilled = re.findall(r'\{\{([A-Z_]+)\}\}', filled_content)
    for placeholder in unfilled:
        param_info = template_info.get("parameters", {}).get(placeholder, {})
        if not param_info.get("required", True):
            filled_content = filled_content.replace("{{" + placeholder + "}}", "")
    
    # 5. Compile
    filename_base = _generate_unique_filename(prefix=template_id)
    result = tool_generate_pdf(filled_content, filename_base)
    return result


def tool_select_template_and_generate(user_request: str, context: str = "") -> str:
    """
    Kompletne narzędzie: Bielik analizuje zapytanie użytkownika, 
    wybiera szablon i generuje treść parametrów, potem kompiluje PDF.
    
    Args:
        user_request: Zapytanie użytkownika (np. "Napisz podanie o urlop")
        context: Dodatkowy kontekst (np. wyniki RAG)
    
    Returns:
        Wynik generowania PDF lub komunikat o błędzie
    """
    print(f"📄 [TOOL USE] Template select & generate for: '{user_request[:60]}...'")
    
    # 1. Get CONDENSED templates info (krótka wersja, żeby zmieścić się w kontekście)
    templates_summary = _get_condensed_templates_info()
    
    # 2. Truncate context to avoid context overflow
    max_context_chars = 1500
    if context and len(context) > max_context_chars:
        context = context[:max_context_chars] + "\n[...skrócono kontekst...]"
    
    # 3. Ask Bielik to select template and fill parameters
    llm, system_prompt = get_bielik_chat(role="template_filler")
    
    prompt = f"""DOSTĘPNE SZABLONY:
{templates_summary}

ZAPYTANIE UŻYTKOWNIKA: "{user_request}"

{f'KONTEKST:{chr(10)}{context}' if context else ''}

Wybierz szablon i wypełnij WSZYSTKIE wymagane parametry. Treść po polsku, formalnym językiem.
Dla list użyj \\begin{{enumerate}} \\item ... \\end{{enumerate}}

ZASADY WYBORU: (Priorytet!)
1. Jeśli użytkownik prosi o PODANIE, WNIOSEK, LIST -> "template_id: letter"
2. Jeśli użytkownik prosi o RAPORT, SPRAWOZDANIE, WYNIKI -> "template_id: report"
3. Jeśli użytkownik prosi o PROTOKÓŁ, NOTATKĘ ZE SPOTKANIA -> "template_id: protocol"

Odpowiedz WYŁĄCZNIE w formacie JSON:
{{
  "template_id": "letter|report|protocol",
  "parameters": {{
    "PARAM1": "wartość1",
    "PARAM2": "wartość2"
  }}
}}"""

    try:
        response = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
        raw_response = response.content
        
        # Wykryj błędy LLM (np. przepełnienie kontekstu)
        if raw_response.startswith("[LLM INVOKE ERROR]") or raw_response.startswith("[LLM"):
            print(f"⚠️ [TEMPLATE] Błąd LLM: {raw_response[:200]}, próba z minimalnym promptem...")
            return _retry_template_generation_minimal(llm, user_request)
        
        # Extract JSON from response
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if not json_match:
            return f"BŁĄD: Model nie zwrócił prawidłowego JSON. Spróbuj ponownie z prostszym zapytaniem.\n\nOdpowiedź modelu:\n{raw_response[:300]}"
        
        parsed = json.loads(json_match.group(0))
        template_id = parsed.get("template_id")
        parameters = parsed.get("parameters", {})
        
        if not template_id:
            return "BŁĄD: Model nie wybrał szablonu (brak template_id w odpowiedzi)"
        
        # 4. Generate PDF from template
        result = tool_generate_pdf_from_template(template_id, parameters)
        return result
        
    except json.JSONDecodeError as e:
        return f"BŁĄD: Nieprawidłowy JSON w odpowiedzi modelu: {e}"
    except Exception as e:
        return f"BŁĄD: {e}"


def _get_condensed_templates_info() -> str:
    """Zwraca skróconą wersję metadanych szablonów (oszczędza tokeny kontekstu)."""
    metadata = _load_templates_metadata()
    templates = metadata.get("templates", {})
    
    lines = []
    for tid, info in templates.items():
        params = info.get("parameters", {})
        param_parts = []
        for pname, pinfo in params.items():
            req_marker = " *" if pinfo.get("required", True) else ""
            param_parts.append(f"{pname}{req_marker}")
        lines.append(f"- {tid}: {info.get('name', tid)} — {info.get('description', '')[:80]}")
        lines.append(f"  Parametry: {', '.join(param_parts)}")
    
    return "\n".join(lines)


def _retry_template_generation_minimal(llm, user_request: str) -> str:
    """Ponowna próba generowania z minimalnym promptem gdy kontekst jest za mały."""
    minimal_prompt = f"""Wybierz szablon i wypełnij parametry dla zapytania: "{user_request[:300]}"

Szablony:
- letter: List/Podanie (SENDER_NAME, CITY_DATE, SENDER_COMPANY, SENDER_ADDRESS, RECIPIENT_TITLE, RECIPIENT_NAME, RECIPIENT_COMPANY, RECIPIENT_ADDRESS, DOCUMENT_TITLE, GREETING, BODY, CLOSING)
- report: Raport (INSTITUTION, REPORT_TITLE, AUTHOR_NAME, AUTHOR_POSITION, DEPARTMENT, REPORT_DATE, REPORT_NUMBER, SECTION_INTRODUCTION, SECTION_SCOPE, SECTION_ANALYSIS, SECTION_RESULTS, SECTION_CONCLUSIONS, APPROVER_NAME)
- protocol: Protokół (INSTITUTION, MEETING_TITLE, MEETING_DATE, MEETING_TIME, MEETING_PLACE, SECRETARY_NAME, CHAIRPERSON_NAME, PROTOCOL_NUMBER, ATTENDEES_LIST, AGENDA, MEETING_MINUTES, DECISIONS, ACTION_ITEMS)

!! BARDZO WAŻNE !!
Jeśli to jest PODANIE lub WNIOSEK Używa szablonu 'letter'. Jeśli RAPORT używa 'report'.

Treść po polsku, formalnie. JSON:"""

    try:
        response = invoke_llm_and_normalize(llm, [
            SystemMessage(content="Generuj JSON z template_id i parameters. Treść po polsku."),
            HumanMessage(content=minimal_prompt)
        ])
        raw = response.content
        
        if raw.startswith("[LLM"):
            return (
                f"BŁĄD: Model językowy nie może przetworzyć zapytania — za mały kontekst.\n\n"
                f"💡 **Rozwiązanie:** Zwiększ `n_ctx` w LM Studio do minimum **8192** tokenów "
                f"(Ustawienia modelu → Context Length).\n\n"
                f"Szczegóły techniczne: {raw[:200]}"
            )
        
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return f"BŁĄD: Model nie zwrócił JSON nawet przy skróconym prompcie.\n\nOdpowiedź: {raw[:300]}"
        
        parsed = json.loads(json_match.group(0))
        template_id = parsed.get("template_id")
        parameters = parsed.get("parameters", {})
        
        if not template_id:
            return "BŁĄD: Model nie wybrał szablonu"
        
        return tool_generate_pdf_from_template(template_id, parameters)
        
    except Exception as e:
        return (
            f"BŁĄD: Nie udało się wygenerować dokumentu: {e}\n\n"
            f"💡 **Wskazówka:** Zwiększ kontekst modelu (n_ctx) w LM Studio do minimum 8192 tokenów."
        )

def tool_validate_document(latex_content: str, doc_type: str = "formal_report") -> dict:
    """
    Waliduje dokument LaTeX zgodnie ze standardami formatowania.
    Sprawdza pakiety, strukturę, zbalansowanie środowisk i nawiasów.
    
    doc_type: "formal_report" | "technical_specification" | "security_procedure"
    """
    print(f"✓ [TOOL USE] Document Validation: {doc_type}")
    
    standards = DOCUMENT_COMPOSITION_STANDARDS.get(doc_type, DOCUMENT_COMPOSITION_STANDARDS["formal_report"])
    
    issues = []
    warnings = []
    
    # --- 1. Sprawdzenie wymaganych pakietów (elastyczne dopasowanie) ---
    required_packages = ["fontenc", "inputenc", "babel"]
    for pkg_name in required_packages:
        # Szukamy dowolnej formy: \usepackage{pkg}, \usepackage[...]{pkg}
        pattern = r'\\usepackage(\[.*?\])?\{' + re.escape(pkg_name) + r'\}'
        if not re.search(pattern, latex_content):
            issues.append(f"Brak pakietu: {pkg_name}")
    
    # --- 2. Sprawdzenie struktury dokumentu ---
    required_commands = ["\\documentclass", "\\begin{document}", "\\end{document}"]
    for cmd in required_commands:
        if cmd not in latex_content:
            issues.append(f"Brak wymaganej komendy: {cmd}")
    
    # --- 3. Sprawdzenie zbalansowania środowisk LaTeX ---
    begin_envs = re.findall(r'\\begin\{(\w+)\}', latex_content)
    end_envs = re.findall(r'\\end\{(\w+)\}', latex_content)
    begin_counts = {}
    end_counts = {}
    for env in begin_envs:
        begin_counts[env] = begin_counts.get(env, 0) + 1
    for env in end_envs:
        end_counts[env] = end_counts.get(env, 0) + 1
    
    for env, count in begin_counts.items():
        end_count = end_counts.get(env, 0)
        if count != end_count:
            issues.append(f"Niezbalansowane środowisko: \\begin{{{env}}} x{count} vs \\end{{{env}}} x{end_count}")
    for env in end_counts:
        if env not in begin_counts:
            issues.append(f"Środowisko \\end{{{env}}} bez odpowiadającego \\begin{{{env}}}")
    
    # --- 4. Sprawdzenie zbalansowania nawiasów klamrowych ---
    # Pomijamy komentarze (linie zaczynające się od %)
    brace_depth = 0
    for line in latex_content.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('%'):
            continue
        for i, ch in enumerate(line):
            if ch == '{' and (i == 0 or line[i-1] != '\\'):
                brace_depth += 1
            elif ch == '}' and (i == 0 or line[i-1] != '\\'):
                brace_depth -= 1
    if brace_depth != 0:
        warnings.append(f"Niezbalansowane nawiasy klamrowe (różnica: {brace_depth})")
    
    # --- 5. Sprawdzenie fontu ---
    if "Times New Roman" in standards["font"]:
        if "\\usepackage{times}" not in latex_content and "times" not in latex_content.lower():
            warnings.append("Sugestia: Dodaj Times New Roman za pomocą \\usepackage{times}")
    
    # --- 6. Sprawdzenie języka polskiego ---
    if "polish" in standards["language"]:
        if "polish" not in latex_content:
            issues.append("Dokument powinien być w języku polskim (babel)")
    
    # --- 7. Struktura sekcji ---
    has_sections = "\\section{" in latex_content or "\\chapter{" in latex_content
    if not has_sections and len(latex_content) > 500:
        warnings.append("Dodaj sekcje do strukturyzacji dokumentu")
    
    # --- 8. Wykrywanie typowych błędów LLM ---
    if '\\begin{documet}' in latex_content or '\\end{documet}' in latex_content:
        issues.append("Literówka: 'documet' zamiast 'document'")
    if latex_content.count('\\maketitle') > 1:
        warnings.append("Wielokrotne \\maketitle — prawdopodobnie duplikat")
    
    return {
        "valid": len(issues) == 0,
        "doc_type": doc_type,
        "standards": standards,
        "issues": issues,
        "warnings": warnings,
        "suggestions": [
            f"Font powinien być: {standards['font']} {standards['size']}",
            f"Język: {standards['language'].upper()}",
            f"Struktura: {' -> '.join(standards['structure'][:3])}..."
        ]
    }

def tool_execute_python(code: str) -> str:
    """Uruchamia kod Pythona w Sandboxie."""
    container = get_sandbox_container()
    if not container: return "BŁĄD: Brak kontenera sandbox."

    # Restart jeśli zatrzymany
    if container.status != 'running':
        try:
            container.start()
            time.sleep(1)
            container.reload()
        except:
            return "BŁĄD: Nie można uruchomić kontenera."

    print(f"💻 [TOOL USE] Executing Python (container={container.name})...")
    
    # Robustna ekstrakcja kodu Python z odpowiedzi LLM
    clean_code = _extract_python_from_llm_output(code)
    
    if not clean_code.strip():
        return "BŁĄD: Nie udało się wyekstrahować kodu Python z odpowiedzi."
    
    try:
        # Użyj base64 encoding żeby uniknąć problemów z cudzysłowami/znakami specjalnymi
        import base64
        encoded = base64.b64encode(clean_code.encode('utf-8')).decode('ascii')
        exec_cmd = f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode('utf-8'))\""
        
        exec_result = container.exec_run(
            cmd=["bash", "-c", exec_cmd],
            workdir="/workspace"
        )
        output = exec_result.output.decode("utf-8").strip()
        
        if exec_result.exit_code == 0:
            return f"SUKCES:\n{output if output else '[Brak outputu]'}"
        else:
            return f"BŁĄD WYKONANIA:\n{output}"
    except Exception as e:
        return f"BŁĄD DOCKERA: {e}"

def _extract_python_from_llm_output(raw_content: str) -> str:
    """Robustne wyciąganie kodu Python z odpowiedzi LLM. Uodpornione na gadatliwość bota."""
    if not raw_content or not raw_content.strip():
        return ""
    
    # 1. Explicit ```python block (wybierz najdłuższy, bo LLM może dać kilka)
    python_blocks = re.findall(r'```(?:python|py)\s*\n(.*?)```', raw_content, re.DOTALL)
    if python_blocks:
        return max(python_blocks, key=len).strip()
    
    # 2. Any ``` block
    code_blocks = re.findall(r'```(?:.*?)\s*\n(.*?)```', raw_content, re.DOTALL)
    if code_blocks:
        return max(code_blocks, key=len).strip()
        
    # 3. Jeśli nie ma bloków markdown, przeanalizuj surowy tekst
    stripped = raw_content.strip()
    
    # Heurystyka do odrzucania polsko/angielskojęzycznego lania wody
    common_words = ['oto', 'poniżej', 'wynik', 'kod', 'to', 'jest', 'proszę', 'here', 'this', 'below', 'sure', 'oczywiście', 'pewnie', 'kod:']
    first_word = stripped.split()[0].lower().rstrip(':,.') if stripped.split() else ""
    if first_word in common_words:
        print(f"      ⚠️ [EXTRACT] Odrzucono próbę wstrzyknięcia języka naturalnego: '{stripped[:50]}...'")
        return ""
        
    # Check for python indicators
    has_python_keywords = any(kw in stripped for kw in [
        'import ', 'from ', 'def ', 'class ', 'print(', 'return ',
        'for ', 'while ', 'if __name__'
    ])
    
    if has_python_keywords:
        return stripped
        
    return ""


def _extract_latex_from_llm_output(raw_content: str) -> str:
    """Robustne wyciąganie kodu LaTeX z odpowiedzi LLM.
    
    Strategia:
    1. Szukaj bloku ```latex ... ``` (preferowany)
    2. Szukaj bloku ```tex ... ```
    3. Szukaj \\documentclass ... \\end{document} bezpośrednio
    4. Szukaj dowolnego bloku ``` ... ``` zawierającego \\documentclass
    5. Fallback: zwróć surowy input
    """
    # 1. Explicit ```latex block
    match = re.search(r'```latex\s*\n(.*?)```', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 2. ```tex block
    match = re.search(r'```tex\s*\n(.*?)```', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 3. Direct \documentclass ... \end{document}
    match = re.search(r'(\\documentclass.*?\\end\{document\})', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 4. Any ``` block containing \documentclass
    code_blocks = re.findall(r'```(?:\w*)?\s*\n(.*?)```', raw_content, re.DOTALL)
    for block in code_blocks:
        if '\\documentclass' in block:
            return block.strip()
    
    # 5. Fallback
    return raw_content.strip()


def _generate_unique_filename(prefix: str = "raport") -> str:
    """Generuj unikalne nazwy plików z timestampem."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.tex"


def tool_generate_pdf(latex_content: str, filename: str = None) -> str:
    """
    Kompiluje kod LaTeX do PDF wewnątrz Sandboxa.
    
    Ulepszenia:
    - Base64 encoding dla bezpiecznego transferu pliku .tex
    - Unikalne nazwy plików z timestampem
    - Timeout na kompilację (120s)
    - Czyszczenie plików pomocniczych po kompilacji
    - Robustne wyciąganie LaTeX z odpowiedzi LLM
    """
    container = get_sandbox_container()
    if not container:
        return "BŁĄD: Sandbox nie działa. Sprawdź czy kontener jest uruchomiony (docker-compose up -d)."

    # Restart jeśli zatrzymany
    if container.status != 'running':
        try:
            container.start()
            time.sleep(2)
            container.reload()
        except Exception as e:
            return f"BŁĄD: Nie można uruchomić kontenera sandbox: {e}"

    # Generuj unikalne nazwy plików jeśli nie podano
    if filename is None:
        filename = _generate_unique_filename()
    
    pdf_name = filename.replace(".tex", ".pdf")
    base_name = filename.replace(".tex", "")
    
    print(f"📄 [TOOL USE] Generowanie PDF: {filename}...")

    # Robustne wyciąganie LaTeX
    clean_latex = _extract_latex_from_llm_output(latex_content)
    
    # Wstępna walidacja — czy to w ogóle LaTeX?
    if '\\documentclass' not in clean_latex:
        return "BŁĄD: Wyciągnięty tekst nie zawiera \\documentclass. Nie jest to poprawny kod LaTeX."

    try:
        # --- 1. ZAPIS PLIKU .TEX PRZEZ BASE64 (bezpieczny transfer) ---
        encoded = base64.b64encode(clean_latex.encode('utf-8')).decode('ascii')
        write_cmd = f"echo '{encoded}' | base64 -d > /workspace/{filename}"
        exec_write = container.exec_run(cmd=["sh", "-c", write_cmd], workdir="/workspace")
        if exec_write.exit_code != 0:
            return f"BŁĄD ZAPISU PLIKU TEX: {exec_write.output.decode('utf-8')}"
        
        # Weryfikacja zapisu
        verify_cmd = f"wc -c < /workspace/{filename}"
        verify_result = container.exec_run(cmd=["sh", "-c", verify_cmd], workdir="/workspace")
        file_size = verify_result.output.decode('utf-8').strip()
        print(f"      📝 Zapisano {filename} ({file_size} bajtów)")

        # --- 2. SPRAWDZENIE PDFLATEX ---
        which_result = container.exec_run(cmd=["sh", "-c", "which pdflatex || true"], workdir="/workspace")
        which_out = which_result.output.decode('utf-8').strip()
        if not which_out:
            return "BŁĄD: pdflatex nie zainstalowany w sandboxie. Sprawdź Dockerfile sandboxu."

        # --- 3. KOMPILACJA Z TIMEOUTEM (podwójna dla TOC/referencji) ---
        # timeout 120s na całą kompilację (2 przebiegi)
        compile_cmd = (
            f"timeout 120 sh -c '"
            f"cd /workspace && "
            f"pdflatex -interaction=nonstopmode -halt-on-error {filename} && "
            f"pdflatex -interaction=nonstopmode {filename}'"
        )
        
        compile_result = container.exec_run(
            cmd=["sh", "-c", compile_cmd],
            workdir="/workspace"
        )

        # --- 4. WERYFIKACJA — czy PDF istnieje? ---
        check_cmd = f"test -f /workspace/{pdf_name} && stat -c%s /workspace/{pdf_name}"
        check_result = container.exec_run(cmd=["sh", "-c", check_cmd], workdir="/workspace")

        if check_result.exit_code == 0:
            pdf_size = check_result.output.decode('utf-8').strip()
            
            # --- 5. CZYSZCZENIE PLIKÓW POMOCNICZYCH ---
            aux_extensions = "aux log out toc nav snm vrb fls fdb_latexmk synctex.gz bbl blg"
            cleanup_cmd = f"cd /workspace && rm -f {' '.join(f'{base_name}.{ext}' for ext in aux_extensions.split())}"
            container.exec_run(cmd=["sh", "-c", cleanup_cmd], workdir="/workspace")
            print(f"      🧹 Wyczyszczono pliki pomocnicze")
            
            return f"SUKCES! Wygenerowano plik: {pdf_name} ({pdf_size} bajtów)\n(Plik znajduje się w folderze sandbox/workspace/{pdf_name})"
        else:
            # PDF nie powstał — pobierz logi diagnostyczne
            log_cmd = f"cat /workspace/{base_name}.log 2>/dev/null || echo 'Brak pliku logu'"
            log_result = container.exec_run(cmd=["sh", "-c", log_cmd], workdir="/workspace")
            log_content = log_result.output.decode('utf-8', errors='ignore')
            
            # Wyciągnij tylko linie z błędami + kontekst
            error_lines = []
            lines = log_content.splitlines()
            for i, line in enumerate(lines):
                if line.startswith('!') or 'Error' in line or 'Fatal' in line:
                    # Dodaj 2 linie kontekstu przed i po
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    error_lines.extend(lines[start:end])
                    error_lines.append('---')
            
            if error_lines:
                error_msg = "\n".join(error_lines[-40:])
            else:
                # Fallback: ostatnie 30 linii logu
                error_msg = "\n".join(lines[-30:])
            
            # Sprawdź czy timeout
            if compile_result.exit_code == 124:
                return f"BŁĄD KOMPILACJI: Timeout (>120s). Dokument może zawierać nieskończoną pętlę.\n{error_msg}"
            
            return f"BŁĄD KOMPILACJI (exit_code={compile_result.exit_code}):\n{error_msg}"

    except Exception as e:
        return f"BŁĄD DOCKERA: {str(e)}"


def tool_read_sandbox_file(filename: str) -> str:
    """
    Odczytuje zawartość pliku z folderu sandbox/workspace.
    Obsługuje pliki .tex, .txt, .py i inne pliki tekstowe.
    
    Args:
        filename: Nazwa pliku (np. 'letter_20260305_173122.tex')
    
    Returns:
        Zawartość pliku lub komunikat o błędzie
    """
    print(f"📖 [TOOL USE] Read sandbox file: {filename}")
    filepath = os.path.join(SANDBOX_WORKSPACE, filename)
    
    if not os.path.exists(filepath):
        # Try to find similar files
        pattern = os.path.splitext(filename)[0]
        matches = glob.glob(os.path.join(SANDBOX_WORKSPACE, f"{pattern}*"))
        if matches:
            available = ", ".join(os.path.basename(m) for m in matches)
            return f"BŁĄD: Plik '{filename}' nie istnieje. Podobne pliki: {available}"
        # List recent files
        all_files = sorted(glob.glob(os.path.join(SANDBOX_WORKSPACE, "*")), key=os.path.getmtime, reverse=True)
        if all_files:
            recent = ", ".join(os.path.basename(f) for f in all_files[:5])
            return f"BŁĄD: Plik '{filename}' nie istnieje. Ostatnie pliki: {recent}"
        return f"BŁĄD: Plik '{filename}' nie istnieje, folder sandbox/workspace jest pusty."
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        size = os.path.getsize(filepath)
        return f"[Plik: {filename}, {size} bajtów]\n{content}"
    except UnicodeDecodeError:
        return f"BŁĄD: Plik '{filename}' nie jest plikiem tekstowym (binarny)."
    except Exception as e:
        return f"BŁĄD: Nie można odczytać pliku: {e}"


def tool_modify_and_recompile_tex(tex_filename: str, modifications_prompt: str, context: str = "") -> str:
    """
    Modyfikuje istniejący plik .tex w sandbox/workspace i rekompiluje do PDF.
    
    1. Odczytuje oryginalny plik .tex
    2. Używa LLM do wprowadzenia żądanych zmian
    3. Zapisuje zmodyfikowany .tex
    4. Rekompiluje do PDF
    
    Args:
        tex_filename: Nazwa pliku .tex do modyfikacji
        modifications_prompt: Opis zmian do wprowadzenia  
        context: Dodatkowy kontekst (np. nowa data, adres)
    
    Returns:
        Wynik generowania PDF lub komunikat o błędzie
    """
    print(f"📝 [TOOL USE] Modify & recompile: {tex_filename}")
    
    # 1. Read the original .tex file
    tex_path = os.path.join(SANDBOX_WORKSPACE, tex_filename)
    if not os.path.exists(tex_path):
        # Try to find the latest .tex file
        tex_files = sorted(glob.glob(os.path.join(SANDBOX_WORKSPACE, "*.tex")), key=os.path.getmtime, reverse=True)
        if tex_files:
            tex_path = tex_files[0]
            tex_filename = os.path.basename(tex_path)
            print(f"      📂 Plik nie znaleziony, używam najnowszego: {tex_filename}")
        else:
            return "BŁĄD: Brak plików .tex w sandbox/workspace. Najpierw wygeneruj dokument."
    
    try:
        with open(tex_path, 'r', encoding='utf-8') as f:
            original_tex = f.read()
    except Exception as e:
        return f"BŁĄD: Nie można odczytać pliku: {e}"
    
    # 2. Use LLM to apply modifications
    llm, system_prompt = get_bielik_chat(role="template_filler")
    
    # Truncate tex if very long
    tex_for_prompt = original_tex
    if len(tex_for_prompt) > 3000:
        tex_for_prompt = tex_for_prompt[:3000] + "\n% [...skrócono...]"
    
    modify_prompt = f"""Masz poniższy dokument LaTeX. Wprowadź TYLKO żądane zmiany, zachowując resztę bez zmian.

ORYGINALNY DOKUMENT:
```latex
{tex_for_prompt}
```

ŻĄDANE ZMIANY:
{modifications_prompt}

{f'DODATKOWY KONTEKST: {context}' if context else ''}

ZASADY:
- Zmień TYLKO te elementy, które dotyczą żądanych zmian
- NIE zmieniaj struktury dokumentu, preambuły ani formatowania LaTeX
- Zachowaj wszystkie polecenia LaTeX (\\begin, \\end, \\documentclass itp.)
- Zwróć KOMPLETNY zmodyfikowany kod LaTeX (cały dokument)

Odpowiedz WYŁĄCZNIE kodem LaTeX (bez dodatkowego tekstu):"""

    try:
        response = invoke_llm_and_normalize(llm, [
            SystemMessage(content=system_prompt),
            HumanMessage(content=modify_prompt)
        ])
        modified_tex = response.content
        
        if modified_tex.startswith("[LLM"):
            return f"BŁĄD: Model nie może przetworzyć — za mały kontekst.\n{modified_tex[:200]}"
        
        # Extract LaTeX from response (LLM may wrap in markdown code blocks)
        modified_tex = _extract_latex_from_llm_output(modified_tex)
        
        if '\\documentclass' not in modified_tex:
            return "BŁĄD: Zmodyfikowany tekst nie zawiera \\documentclass — LLM nie zwrócił poprawnego LaTeX."
        
    except Exception as e:
        return f"BŁĄD: Nie udało się zmodyfikować dokumentu: {e}"
    
    # 3. Generate new filename and compile
    prefix = os.path.splitext(tex_filename)[0].split('_')[0]  # e.g. "letter" from "letter_20260305_173122.tex"
    new_filename = _generate_unique_filename(prefix=prefix)
    
    result = tool_generate_pdf(modified_tex, new_filename)
    return result


# --- HUMAN-IN-THE-LOOP (HITL) HELPERS ---

def create_hitl_action_request(content: str, actions: list, continuation: dict = None) -> dict:
    """Create a HITL request that presents selectable options to the user.

    Args:
        content: Message shown to the user explaining what to choose.
        actions: List of dicts with keys: name (str), label (str), payload (dict).
        continuation: Dict describing how to continue after user responds.
            e.g. {"type": "generate_pdf", "template_id": "letter", "parameters": {...}}
    """
    return {
        "type": "action",
        "content": content,
        "actions": actions,
        "continuation": continuation or {},
    }


def create_hitl_form_request(content: str, fields: list, continuation: dict = None) -> dict:
    """Create a HITL request that presents a fillable form to the user.

    Args:
        content: Message shown above the form.
        fields: List of form field dicts with keys:
            - id (str): field identifier
            - label (str): display label
            - type (str): "text" | "textarea" | "select"
            - value (str): pre-filled value from the agent
            - options (list[str]): choices for "select" type
            - required (bool): whether the field is required
        continuation: Dict describing how to continue after user responds.
    """
    return {
        "type": "form",
        "content": content,
        "fields": fields,
        "continuation": continuation or {},
    }


def create_hitl_confirm_request(content: str, continuation: dict = None) -> dict:
    """Create a HITL request for simple confirmation (approve/reject/edit).

    Args:
        content: Message with preview of what will be done, asking user to confirm.
        continuation: Dict describing how to continue after user responds.
    """
    return {
        "type": "confirm",
        "content": content,
        "continuation": continuation or {},
    }


def build_document_hitl_form(template_id: str, parameters: dict, templates_metadata: dict) -> dict:
    """Build a HITL form for reviewing/editing document parameters before PDF generation.

    Returns a hitl_request dict ready to be set in AgentState.
    """
    template_info = templates_metadata.get("templates", {}).get(template_id, {})
    template_name = template_info.get("name", template_id)
    param_definitions = template_info.get("parameters", {})

    def _normalize_key(key: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(key).lower())

    # Canonicalize incoming params to actual template keys to avoid missing required fields.
    alias_map = {}
    for canonical in param_definitions.keys():
        alias_map[_normalize_key(canonical)] = canonical

    # Common alias mappings for known templates (best-effort normalization).
    if template_id == "letter":
        alias_map.update({
            _normalize_key("sender"): "SENDER_NAME",
            _normalize_key("sender_name"): "SENDER_NAME",
            _normalize_key("nadawca"): "SENDER_NAME",
            _normalize_key("podpis"): "SENDER_NAME",
            _normalize_key("signature"): "SENDER_NAME",
            _normalize_key("sender_company"): "SENDER_COMPANY",
            _normalize_key("sender_address"): "SENDER_ADDRESS",
            _normalize_key("email"): "SENDER_EMAIL",
            _normalize_key("sender_email"): "SENDER_EMAIL",
            _normalize_key("recipient"): "RECIPIENT_NAME",
            _normalize_key("recipient_name"): "RECIPIENT_NAME",
            _normalize_key("adresat"): "RECIPIENT_NAME",
            _normalize_key("recipient_company"): "RECIPIENT_COMPANY",
            _normalize_key("recipient_address"): "RECIPIENT_ADDRESS",
            _normalize_key("recipient_title"): "RECIPIENT_TITLE",
            _normalize_key("date"): "CITY_DATE",
            _normalize_key("data"): "CITY_DATE",
            _normalize_key("city_date"): "CITY_DATE",
            _normalize_key("subject"): "DOCUMENT_TITLE",
            _normalize_key("title"): "DOCUMENT_TITLE",
            _normalize_key("tytul"): "DOCUMENT_TITLE",
            _normalize_key("greeting"): "GREETING",
            _normalize_key("powitanie"): "GREETING",
            _normalize_key("salutation"): "GREETING",
            _normalize_key("body"): "BODY",
            _normalize_key("tresc"): "BODY",
            _normalize_key("content"): "BODY",
            _normalize_key("closing"): "CLOSING",
            _normalize_key("pozdrowienia"): "CLOSING",
        })
    elif template_id == "report":
        alias_map.update({
            _normalize_key("institution"): "INSTITUTION",
            _normalize_key("instytucja"): "INSTITUTION",
            _normalize_key("title"): "REPORT_TITLE",
            _normalize_key("report_title"): "REPORT_TITLE",
            _normalize_key("tytul"): "REPORT_TITLE",
            _normalize_key("subtitle"): "REPORT_SUBTITLE",
            _normalize_key("report_subtitle"): "REPORT_SUBTITLE",
            _normalize_key("podtytul"): "REPORT_SUBTITLE",
            _normalize_key("author"): "AUTHOR_NAME",
            _normalize_key("author_name"): "AUTHOR_NAME",
            _normalize_key("autor"): "AUTHOR_NAME",
            _normalize_key("author_position"): "AUTHOR_POSITION",
            _normalize_key("stanowisko"): "AUTHOR_POSITION",
            _normalize_key("department"): "DEPARTMENT",
            _normalize_key("dzial"): "DEPARTMENT",
            _normalize_key("zaklad"): "DEPARTMENT",
            _normalize_key("report_date"): "REPORT_DATE",
            _normalize_key("date"): "REPORT_DATE",
            _normalize_key("data"): "REPORT_DATE",
            _normalize_key("report_number"): "REPORT_NUMBER",
            _normalize_key("numer"): "REPORT_NUMBER",
            _normalize_key("intro"): "SECTION_INTRODUCTION",
            _normalize_key("introduction"): "SECTION_INTRODUCTION",
            _normalize_key("wstep"): "SECTION_INTRODUCTION",
            _normalize_key("wprowadzenie"): "SECTION_INTRODUCTION",
            _normalize_key("scope"): "SECTION_SCOPE",
            _normalize_key("zakres"): "SECTION_SCOPE",
            _normalize_key("analysis"): "SECTION_ANALYSIS",
            _normalize_key("analiza"): "SECTION_ANALYSIS",
            _normalize_key("results"): "SECTION_RESULTS",
            _normalize_key("wyniki"): "SECTION_RESULTS",
            _normalize_key("conclusions"): "SECTION_CONCLUSIONS",
            _normalize_key("wnioski"): "SECTION_CONCLUSIONS",
            _normalize_key("rekomendacje"): "SECTION_CONCLUSIONS",
            _normalize_key("approver"): "APPROVER_NAME",
            _normalize_key("zatwierdzajacy"): "APPROVER_NAME",
        })
    elif template_id == "protocol":
        alias_map.update({
            _normalize_key("institution"): "INSTITUTION",
            _normalize_key("instytucja"): "INSTITUTION",
            _normalize_key("meeting_title"): "MEETING_TITLE",
            _normalize_key("title"): "MEETING_TITLE",
            _normalize_key("temat"): "MEETING_TITLE",
            _normalize_key("tytul"): "MEETING_TITLE",
            _normalize_key("meeting_date"): "MEETING_DATE",
            _normalize_key("date"): "MEETING_DATE",
            _normalize_key("data"): "MEETING_DATE",
            _normalize_key("meeting_time"): "MEETING_TIME",
            _normalize_key("time"): "MEETING_TIME",
            _normalize_key("czas"): "MEETING_TIME",
            _normalize_key("godzina"): "MEETING_TIME",
            _normalize_key("meeting_place"): "MEETING_PLACE",
            _normalize_key("place"): "MEETING_PLACE",
            _normalize_key("miejsce"): "MEETING_PLACE",
            _normalize_key("sala"): "MEETING_PLACE",
            _normalize_key("secretary"): "SECRETARY_NAME",
            _normalize_key("protokolant"): "SECRETARY_NAME",
            _normalize_key("sekretarz"): "SECRETARY_NAME",
            _normalize_key("chairperson"): "CHAIRPERSON_NAME",
            _normalize_key("prowadzacy"): "CHAIRPERSON_NAME",
            _normalize_key("przewodniczacy"): "CHAIRPERSON_NAME",
            _normalize_key("protocol_number"): "PROTOCOL_NUMBER",
            _normalize_key("numer"): "PROTOCOL_NUMBER",
            _normalize_key("attendees"): "ATTENDEES_LIST",
            _normalize_key("obecni"): "ATTENDEES_LIST",
            _normalize_key("uczestnicy"): "ATTENDEES_LIST",
            _normalize_key("lista_obecnosci"): "ATTENDEES_LIST",
            _normalize_key("agenda"): "AGENDA",
            _normalize_key("porzadek"): "AGENDA",
            _normalize_key("minutes"): "MEETING_MINUTES",
            _normalize_key("przebieg"): "MEETING_MINUTES",
            _normalize_key("opis"): "MEETING_MINUTES",
            _normalize_key("dyskusja"): "MEETING_MINUTES",
            _normalize_key("decisions"): "DECISIONS",
            _normalize_key("decyzje"): "DECISIONS",
            _normalize_key("ustalenia"): "DECISIONS",
            _normalize_key("actions"): "ACTION_ITEMS",
            _normalize_key("zadania"): "ACTION_ITEMS",
        })

    normalized_params = {}
    for key, value in (parameters or {}).items():
        canonical = alias_map.get(_normalize_key(key))
        if canonical and canonical not in normalized_params:
            normalized_params[canonical] = value

    fields = []
    for param_name, param_def in param_definitions.items():
        param_value = normalized_params.get(param_name, "")
        field_type = "textarea" if len(str(param_value)) > 100 else "text"
        fields.append({
            "id": param_name,
            "label": param_def.get("description", param_name),
            "type": field_type,
            "value": str(param_value),
            "required": param_def.get("required", True),
        })

    # Add template selection as a select field at the top
    available_templates = list(templates_metadata.get("templates", {}).keys())
    fields.insert(0, {
        "id": "_template_id",
        "label": "Szablon dokumentu",
        "type": "select",
        "value": template_id,
        "options": available_templates,
        "required": True,
    })

    content = (
        f"📋 **Przegląd parametrów dokumentu: {template_name}**\n\n"
        f"Sprawdź i ewentualnie zmodyfikuj poniższe pola przed wygenerowaniem PDF."
    )

    return create_hitl_form_request(
        content=content,
        fields=fields,
        continuation={
            "type": "generate_pdf",
            "template_id": template_id,
            "parameters": parameters,
        },
    )


def invoke_llm_and_normalize(llm, messages):
    """Invoke an LLM and normalize the result to a message-like object.

    Handles several adapter return shapes: direct message objects, dicts,
    streaming iterators (collects chunks), and awaitables (best-effort).
    Returns an object with a `content` attribute (AIMessage/SystemMessage
    or similar). On irrecoverable errors returns a simple AIMessage-like
    object containing the error text.
    """
    from langchain_core.messages import AIMessage, SystemMessage
    import inspect
    import asyncio

    try:
        resp = llm.invoke(messages)
    except Exception as e:
        try:
            return AIMessage(content=f"[LLM INVOKE ERROR] {e}")
        except:
            return SystemMessage(content=f"[LLM INVOKE ERROR] {e}")

    # If the adapter returned an awaitable/coroutine, try to resolve it
    if inspect.isawaitable(resp):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Running inside an event loop: don't block — return a placeholder
                return AIMessage(content=f"[LLM] returned awaitable while loop running: {type(resp)}")
            else:
                resp = loop.run_until_complete(resp)
        except Exception as e:
            return AIMessage(content=f"[LLM AWAIT ERROR] {e}")

    # PRIORITY: If it already has a content attribute (AIMessage, SystemMessage, etc.),
    # return it directly. This MUST come before the iterator check because
    # LangChain message objects are iterable (yield field tuples) but we want .content.
    if hasattr(resp, 'content') and isinstance(resp.content, str):
        return resp

    # If it's a dict-like response, extract text
    if isinstance(resp, dict):
        content = resp.get('content') or resp.get('text') or str(resp)
        return AIMessage(content=content)

    # If it's an iterator (streaming), collect chunks
    try:
        if hasattr(resp, '__iter__') and not isinstance(resp, (str, bytes, dict)):
            parts = []
            for chunk in resp:
                if hasattr(chunk, 'content'):
                    parts.append(chunk.content)
                elif isinstance(chunk, dict):
                    parts.append(chunk.get('content') or chunk.get('text') or str(chunk))
                else:
                    parts.append(str(chunk))
            return AIMessage(content=''.join(parts))
    except Exception:
        pass

    # Fallback: stringify
    return AIMessage(content=str(resp))
