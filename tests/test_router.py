from langchain_core.messages import HumanMessage
from src.agent_graph import app
import sys

def test_query(query: str):
    print(f"\n{'='*50}")
    print(f"👤 USER: {query}")
    try:
        inputs = {"messages": [HumanMessage(content=query)]}
        result = app.invoke(inputs)
        last_msg = result["messages"][-1].content
        print(f"🤖 AGENT: {last_msg}")
    except Exception as e:
        print(f"❌ BŁĄD: {e}")
        print("Czy LM Studio i zdalny serwer są włączone?")

if __name__ == "__main__":
    print("🚀 TESTOWANIE GRAFU (Local Bielik + Remote Qwen)")
    
    # 1. Test Wiedzy (oczekiwane: KNOWLEDGE)
    test_query("Jaka jest procedura pożarowa w firmie?")
    
    # 2. Test Operacyjny (oczekiwane: KNOWLEDGE)
    test_query("Sprawdź mój kalendarz na jutro.")
    
    # 3. Test Kodowania (oczekiwane: CODER - tu nastąpi połączenie z serwerem modelu kodu)
    test_query("Napisz prostą funkcję w Pythonie sumującą listę.")
    
    test_query("Napisz kod w Pythonie, który obliczy pierwiastek z liczby 1444 i wypisze wynik.")
    
    
    # 4. Test Czat (oczekiwane: CHAT)
    test_query("Opowiedz krótki żart.")
    
    test_query("Wygeneruj macierz 3x3 losowych liczb używając numpy i wypisz jej średnią.")
    
    
    test_query("Napisz kod, który dzieli liczbę 10 przez 0 i obsłuż ten błąd.")
    
    
