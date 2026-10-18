import os
import logging
from typing import List
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Importy z LangChain / Chroma (te same co w ETL)
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
import chromadb

# 1. Konfiguracja i Logowanie
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("knowledge_server")

# Stałe (zgodne z ingest_data.py)
MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "intfloat/multilingual-e5-base")
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "company_knowledge")

# 2. Inicjalizacja Serwera MCP
mcp = FastMCP("Company Knowledge Base")

# 3. Inicjalizacja Zasobów (Globalna, by nie ładować modelu przy każdym zapytaniu)
logger.info(f"⏳ Ładowanie modelu embeddingu: {MODEL_NAME}...")
try:
    embedding_function = HuggingFaceEmbeddings(
        model_name=MODEL_NAME,
        model_kwargs={'device': 'cpu'},
        encode_kwargs={'normalize_embeddings': True} # Kluczowe dla modelu E5
    )
    
    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    
    vector_store = Chroma(
        client=client,
        collection_name=COLLECTION_NAME,
        embedding_function=embedding_function,
    )
    logger.info("✅ Połączono z ChromaDB i załadowano model.")
except Exception as e:
    logger.error(f"❌ Błąd inicjalizacji: {e}")
    raise e

@mcp.tool()
def search_company_docs(query: str) -> str:
    """
    Przeszukuje bazę wiedzy. Zwraca fragmenty z metadanymi źródłowymi.
    """
    logger.info(f"🔍 Otrzymano zapytanie: {query}")
    
    try:
        search_query = query
        if "e5" in MODEL_NAME.lower() and not query.startswith("query:"):
            search_query = f"query: {query}"
            
        results = vector_store.similarity_search(search_query, k=3)
        
        if not results:
            return "Nie znaleziono dokumentów."
        
        # Budujemy odpowiedź, która instruuje Model, jak ma cytować
        response_text = "Znaleziono następujące informacje. Użyj ich do odpowiedzi i ZAWSZE cytuj ID źródła.\n\n"
        
        for i, doc in enumerate(results, 1):
            # Pobieramy pełną ścieżkę i wyciągamy samą nazwę pliku dla czytelności
            full_path = doc.metadata.get("source", "N/A")
            filename = os.path.basename(full_path)
            
            # Symulacja linku (w prawdziwym systemie tu byłby link do SharePoint/Jira/GoogleDrive)
            # Jeśli to plik lokalny, możemy zostawić nazwę.
            reference_link = f"file://{filename}" 
            
            content = doc.page_content.strip()
            
            # Formatujemy blok dla LLM
            response_text += f"""
--- FRAGMENT #{i} ---
TREŚĆ: "{content}"
METADANE_ID: [{filename}]
LOKALIZACJA: {full_path}
---------------------
"""
            
        return response_text

    except Exception as e:
        logger.error(f"Błąd: {e}")
        return str(e)

if __name__ == "__main__":
    # Ten blok pozwala uruchomić serwer bezpośrednio z pythona
    mcp.run()
