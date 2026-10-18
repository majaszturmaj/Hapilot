#!/bin/bash

# Kolory
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}Uruchamianie Systemu Hapilot...${NC}"

# 1. Czyszczenie starych śmieci (Fix dla KeyError)
echo -e "${BLUE}--- 0/4 Czyszczenie starych procesów ---${NC}"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD="docker compose"
else
    DOCKER_COMPOSE_CMD="docker-compose"
fi

# Zabij stary Chainlit jeśli działa
pkill -f "chainlit run" 2>/dev/null || true

# Remove potentially stale containers that cause KeyError when inspected
docker rm -f agent_sandbox python_sandbox >/dev/null 2>&1 || true

# Bring down existing compose stacks (remove orphans)
$DOCKER_COMPOSE_CMD -f docker/docker-compose.yml --project-directory . down --remove-orphans > /dev/null 2>&1

# 2. Główna infrastruktura
echo -e "${BLUE}--- 1/4 Startowanie: ChromaDB & n8n ---${NC}"
# force-recreate naprawia problemy z configiem
$DOCKER_COMPOSE_CMD -f docker/docker-compose.yml --project-directory . up -d --force-recreate --remove-orphans || {
    echo -e "${RED}⚠️  Ostrzeżenie: nie udało się uruchomić głównych usług za pomocą $DOCKER_COMPOSE_CMD${NC}"
}

# 3. Sandbox
echo -e "${BLUE}--- 2/4 Startowanie: Agent Sandbox ---${NC}"
cd sandbox
# Usuń stare kontenery sandbox jeśli istnieją, potem uruchom
docker rm -f agent_sandbox python_sandbox >/dev/null 2>&1 || true
$DOCKER_COMPOSE_CMD up -d --force-recreate --remove-orphans || {
    echo -e "${RED}⚠️  Ostrzeżenie: nie udało się uruchomić sandboxu za pomocą $DOCKER_COMPOSE_CMD${NC}"
}
cd ..

# 4. Oczekiwanie na bazę
echo -e "${BLUE}⏳ Czekanie na wstanie bazy ChromaDB...${NC}"
DB_READY=false
for i in {1..30}; do
    if curl -s http://localhost:8000/api/v1/heartbeat > /dev/null; then
        echo -e "${GREEN}✅ ChromaDB jest gotowa!${NC}"
        DB_READY=true
        break
    fi
    echo -n "."
    sleep 1
done

# 5. Ingestia (Fix dla SyntaxError - używamy ścieżki bezpośredniej do venv)
if [ "$DB_READY" = true ]; then
    echo -e "${BLUE}--- 3/4 Karmienie Bazy Wiedzy ---${NC}"
    
    # UŻYWAMY BEZPOŚREDNIEJ ŚCIEŻKI DO PYTHON 3 W VENV
    # To zapobiega użyciu systemowego Pythona 2
    ./venv/bin/python src/ingest_data.py
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Wiedza załadowana pomyślnie!${NC}"
    else
        echo -e "${RED}❌ Błąd podczas ładowania wiedzy.${NC}"
        # Pokaż wersję pythona dla debugu
        echo "Używany Python: $(./venv/bin/python --version)"
    fi
else
    echo -e "${RED}❌ Błąd: ChromaDB nie wstała.${NC}"
fi

# 6. Uruchomienie Chainlit (Web Chat UI)
echo -e "${BLUE}--- 4/4 Startowanie: Web Chat UI (Chainlit) ---${NC}"

# Sprawdź, czy Chainlit już działa na porcie 8080
if curl -s -o /dev/null -w '' http://localhost:8080 2>/dev/null; then
    echo -e "${GREEN}✅ Chainlit już działa na porcie 8080.${NC}"
else
    # Zabij stary proces na wszelki wypadek
    pkill -f "chainlit run" 2>/dev/null || true
    sleep 1

    # Uruchom Chainlit w tle
    # Chainlit może być zainstalowany w venv, user site-packages, lub globalnie
    CHAINLIT_BIN=""
    if [ -f "./venv/bin/chainlit" ]; then
        CHAINLIT_BIN="./venv/bin/chainlit"
    elif [ -f "$HOME/.local/bin/chainlit" ]; then
        CHAINLIT_BIN="$HOME/.local/bin/chainlit"
    else
        CHAINLIT_BIN=$(which chainlit 2>/dev/null)
    fi

    if [ -z "$CHAINLIT_BIN" ]; then
        echo -e "${RED}❌ Chainlit nie jest zainstalowany. Zainstaluj: pip install chainlit${NC}"
    else
        nohup "$CHAINLIT_BIN" run src/app.py --host 0.0.0.0 --port 8080 > chainlit.log 2>&1 &
        CHAINLIT_PID=$!
        disown $CHAINLIT_PID 2>/dev/null

        # Poczekaj aż port 8080 zacznie odpowiadać (max 30s)
        echo -n "   Oczekiwanie na Chainlit"
        CHAINLIT_READY=false
        for i in {1..30}; do
            # Sprawdź czy proces nadal żyje
            if ! kill -0 $CHAINLIT_PID 2>/dev/null; then
                echo ""
                echo -e "${RED}❌ Proces Chainlit zakończył się nieoczekiwanie. Sprawdź chainlit.log:${NC}"
                tail -20 chainlit.log 2>/dev/null
                break
            fi
            # Sprawdź czy port odpowiada
            if curl -s -o /dev/null -w '' http://localhost:8080 2>/dev/null; then
                echo ""
                echo -e "${GREEN}✅ Chainlit uruchomiony (PID: $CHAINLIT_PID)${NC}"
                CHAINLIT_READY=true
                break
            fi
            echo -n "."
            sleep 1
        done

        if [ "$CHAINLIT_READY" = false ] && kill -0 $CHAINLIT_PID 2>/dev/null; then
            echo ""
            echo -e "${BLUE}⏳ Chainlit nadal się uruchamia (PID: $CHAINLIT_PID). Sprawdź za chwilę: http://localhost:8080${NC}"
        fi
    fi
fi

echo "---------------------------------------------------"
echo -e "${GREEN}✅ GOTOWE.${NC}"
echo -e "${BLUE}🌐 Chat dostępny pod adresem: ${GREEN}http://localhost:8080${NC}"
