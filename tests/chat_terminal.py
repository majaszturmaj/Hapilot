import os
import sys
from langchain_core.messages import HumanMessage, AIMessage
from src.agent_graph import app

# Kolory do terminala (żeby było ładnie i czytelnie)
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def main():
    clear_screen()
    print(f"{Colors.HEADER}{Colors.BOLD}╔══════════════════════════════════════════════════╗{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}║           SYSTEM HAPILOT - TERMINAL CHAT           ║{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}╚══════════════════════════════════════════════════╝{Colors.ENDC}")
    print(f"{Colors.CYAN}Dostępne komendy: 'exit', 'quit', 'clear'{Colors.ENDC}")
    print("-" * 60)

    # --- PAMIĘĆ RAM ROZMOWY ---
    # Ponieważ nie mamy jeszcze bazy danych do historii (Postgres),
    # trzymamy historię w zwykłej liście Pythona podczas działania skryptu.
    chat_history = []

    while True:
        try:
            # 1. Pobranie wejścia od użytkownika
            user_input = input(f"\n{Colors.GREEN}TY: {Colors.ENDC}")
            
            if user_input.lower() in ["exit", "quit"]:
                print(f"\n{Colors.YELLOW}Do widzenia!{Colors.ENDC}")
                break
            
            if user_input.lower() == "clear":
                chat_history = []
                clear_screen()
                print(f"{Colors.YELLOW}Historia rozmowy wyczyszczona.{Colors.ENDC}")
                continue

            if not user_input.strip():
                continue

            # 2. Przygotowanie stanu
            # Dodajemy nową wiadomość użytkownika do historii
            chat_history.append(HumanMessage(content=user_input))
            
            print(f"{Colors.BLUE}--- MYŚLENIE I WYKONYWANIE ZADAŃ ---{Colors.ENDC}")
            
            # 3. Uruchomienie Agenta (Invoke)
            # Przekazujemy całą historię, żeby agent miał kontekst
            # recursion_limit=50 pozwala na długie pętle naprawcze (coder <-> execute)
            inputs = {"messages": chat_history}
            result = app.invoke(inputs, config={"recursion_limit": 50})
            
            # 4. Aktualizacja historii
            # Wynik agenta zawiera zaktualizowaną listę wiadomości.
            # Nadpisujemy naszą lokalną historię tym, co zwrócił graf.
            chat_history = result["messages"]
            
            # 5. Wyświetlenie ostatniej odpowiedzi
            # Ostatnia wiadomość w liście to odpowiedź końcowa (od Chat lub Knowledge)
            # Chyba że skończyliśmy na Execute - wtedy trzeba poszukać ostatniej AIMessage
            last_msg = chat_history[-1]
            
            print(f"{Colors.BLUE}------------------------------------{Colors.ENDC}")
            print(f"{Colors.YELLOW}AGENT: {Colors.ENDC}{last_msg.content}")

        except KeyboardInterrupt:
            print(f"\n\n{Colors.RED}Przerwano przez użytkownika.{Colors.ENDC}")
            break
        except Exception as e:
            print(f"\n{Colors.RED}BŁĄD KRYTYCZNY: {e}{Colors.ENDC}")

if __name__ == "__main__":
    main()
