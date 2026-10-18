import os
from dotenv import load_dotenv
import chromadb
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

load_dotenv()

MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME")

# Musimy użyć tych samych parametrów co przy ingestii!
embedding_function = HuggingFaceEmbeddings(
    model_name=MODEL_NAME,
    model_kwargs={'device': 'cpu'},
    encode_kwargs={'normalize_embeddings': True} 
)

vector_store = Chroma(
    client=chromadb.HttpClient(host='localhost', port=8000),
    collection_name=COLLECTION_NAME,
    embedding_function=embedding_function,
)

query = "Co ratujemy w przypadku pożaru?"
# Dodajemy prefiks dla modelu E5, jeśli go używasz
if "e5" in MODEL_NAME.lower():
    query = f"query: {query}"

results = vector_store.similarity_search(query, k=1)

if results:
    print("\n--- WYNIK WYSZUKIWANIA ---")
    print(results[0].page_content)
    print("\nMetadane:", results[0].metadata)
else:
    print("\n❌ Brak wyników. Baza jest pusta lub model nie znalazł dopasowania.")
