import httpx
from mcp.server.fastmcp import FastMCP

# Adres lokalny n8n
N8N_WEBHOOK_BASE = "http://localhost:5679/webhook"

mcp = FastMCP("Office 365 Operations")

@mcp.tool()
def create_email_draft(to_recipients: str, subject: str, body_content: str) -> str:
    """
    Tworzy wersję roboczą (Draft) wiadomości e-mail w Outlooku.
    NIE wysyła wiadomości automatycznie, pozwala użytkownikowi na weryfikację.
    
    Args:
        to_recipients: Adres(y) email odbiorców (oddzielone przecinkami).
        subject: Temat wiadomości.
        body_content: Treść wiadomości (może być tekst lub prosty HTML).
    """
    url = f"{N8N_WEBHOOK_BASE}/outlook-draft"
    payload = {
        "to": to_recipients,
        "subject": subject,
        "body": body_content
    }
    
    try:
        # Timeout 5s jest bezpieczny dla lokalnej sieci
        response = httpx.post(url, json=payload, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        return f"✅ Utworzono draft w Outlooku. ID: {data.get('draft_id')}. Folder: {data.get('folder')}"
    except Exception as e:
        return f"❌ Błąd tworzenia draftu: {str(e)}"

@mcp.tool()
def get_calendar_events(date: str) -> str:
    """
    Pobiera wydarzenia z kalendarza na wskazany dzień.
    Pomocne do sprawdzania dostępności lub planowania spotkań.
    
    Args:
        date: Data w formacie YYYY-MM-DD (np. '2024-05-20').
    """
    url = f"{N8N_WEBHOOK_BASE}/outlook-calendar"
    payload = {
        "date": date
    }
    
    try:
        response = httpx.post(url, json=payload, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        
        # Formatowanie odpowiedzi dla Agenta, żeby była czytelna
        events = data.get("events", [])
        if not events:
            return f"📅 Brak wydarzeń w kalendarzu na dzień {date}."
            
        result = f"📅 Wydarzenia na dzień {date}:\n"
        for event in events:
            result += f"- {event['start']} - {event['end']}: {event['subject']} (Org: {event['organizer']})\n"
            
        return result
    except Exception as e:
        return f"❌ Błąd pobierania kalendarza: {str(e)}"

@mcp.tool()
def log_security_audit(event_type: str, details: str) -> str:
    """
    Loguje zdarzenie w systemie audytowym OmegaCorp.
    Użyj TEGO narzędzia ZAWSZE, gdy korzystasz z tajnych procedur lub danych osobowych.
    """
    url = f"{N8N_WEBHOOK_BASE}/audit-log"
    payload = {
        "event_type": event_type,
        "details": details,
        "agent": "Hapilot-System"
    }
    
    try:
        response = httpx.post(url, json=payload, timeout=5.0)
        return f"🔒 Zdarzenie zalogowane w audycie."
    except Exception as e:
        return f"❌ Błąd audytu: {str(e)}"

if __name__ == "__main__":
    mcp.run()
