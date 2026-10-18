# -*- coding: utf-8 -*-
import os
import glob
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
import chromadb
from dotenv import load_dotenv

load_dotenv()

# --- KONFIGURACJA ---
DATA_PATH = "./data/private_docs"
CHROMA_HOST = "localhost"
CHROMA_PORT = 8000
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "company_knowledge")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "intfloat/multilingual-e5-base")

def main():
    print(f"🚀 [INGEST] Rozpoczynam indeksowanie plików z: {DATA_PATH}")

    # 1. WCZYTYWANIE PLIKÓW
    documents = []
    
    # Obsługa plików .txt
    txt_files = glob.glob(os.path.join(DATA_PATH, "*.txt"))
    for file_path in txt_files:
        try:
            print(f"📄 Wczytuję: {file_path}...")
            # Wymuszamy UTF-8, żeby polskie znaki działały
            loader = TextLoader(file_path, encoding='utf-8')
            documents.extend(loader.load())
        except Exception as e:
            print(f"❌ Błąd przy pliku {file_path}: {e}")

    # Obsługa plików .pdf (na przyszłość)
    pdf_files = glob.glob(os.path.join(DATA_PATH, "*.pdf"))
    for file_path in pdf_files:
        try:
            print(f"📄 Wczytuję PDF: {file_path}...")
            loader = PyPDFLoader(file_path)
            documents.extend(loader.load())
        except Exception as e:
            print(f"❌ Błąd przy PDF {file_path}: {e}")

    if not documents:
        print("⚠️ Nie znaleziono żadnych dokumentów! Sprawdź ścieżkę ./data/private_docs")
        return

    print(f"✅ Załadowano łącznie {len(documents)} dokumentów źródłowych.")

    # 2. DZIELENIE NA KAWAŁKI (CHUNKING)
    # E5-base ma limit 512 tokenów, więc tniemy bezpiecznie na ~800 znaków
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=200,
        separators=["\n\n", "\n", ". ", " ", ""]
    )
    
    chunks = text_splitter.split_documents(documents)
    print(f"🔪 Podzielono na {len(chunks)} fragmentów (chunks).")

    # 3. PRZYGOTOWANIE BAZY
    print(f"🔌 Łączenie z ChromaDB ({CHROMA_HOST})...")
    chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    
    # Reset bazy (opcjonalnie - żeby nie dublować danych przy każdym uruchomieniu)
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
        print("🗑️ Wyczyszczono starą kolekcję.")
    except:
        pass # Kolekcja nie istniała

    # 4. EMBEDDING I ZAPIS
    print(f"🧠 Generowanie wektorów (Model: {EMBEDDING_MODEL})...")
    embedding_function = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={'device': 'cpu'},
        encode_kwargs={'normalize_embeddings': True}
    )

    vector_store = Chroma(
        client=chroma_client,
        collection_name=COLLECTION_NAME,
        embedding_function=embedding_function,
    )

    # Batchowe dodawanie (bezpieczniej dla pamięci)
    batch_size = 100
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        vector_store.add_documents(batch)
        print(f"   💾 Zapisano partię {i}-{min(i+batch_size, len(chunks))}")

    print(f"🎉 SUKCES! Baza wiedzy zaktualizowana o realne pliki.")

if __name__ == "__main__":
    main()
