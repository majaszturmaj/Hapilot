"""
ModelRegistry — persistent, runtime-editable registry of LLM connections.

Persists to models_config.json in the project root.
Acts as a singleton so all modules share the same live config without restart.

Structure of models_config.json:
{
  "models": [
    {
      "id": "bielik_default",
      "display_name": "Bielik 11B (domyślny)",
      "base_url": "http://localhost:1234/v1",
      "model_id": "bielik-11b-v3.0-instruct",
      "api_key": "lm-studio"
    },
    ...
  ],
  "main_model_id": "bielik_default",
  "coder_model_id": "qwen_default"
}
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

# Registry file lives in project root (one level above src/)
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.abspath(os.path.join(_SRC_DIR, '..', 'models_config.json'))

# Seed defaults — mirrors the original hard-coded constants in llm_config.py
_DEFAULT_MAIN_URL   = "http://localhost:1234/v1"
_DEFAULT_MAIN_ID    = "bielik-11b-v3.0-instruct"
_DEFAULT_CODER_URL  = "http://127.0.0.1:1235/v1"
_DEFAULT_CODER_ID   = "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"


@dataclass
class ModelEntry:
    id: str
    display_name: str
    base_url: str
    model_id: str
    api_key: str = ""

    def label(self) -> str:
        """Human-readable label used as the Select widget value.
        Format: 'Display Name  [model-id @ host:port]'
        """
        # Strip /v1 or trailing slash for display
        host = self.base_url.rstrip("/")
        if host.endswith("/v1"):
            host = host[:-3]
        host = host.replace("http://", "").replace("https://", "")
        return f"{self.display_name}  [{self.model_id} @ {host}]"


class ModelRegistry:
    """Singleton registry. Import the module-level `registry` instance."""

    _instance: Optional["ModelRegistry"] = None

    def __new__(cls) -> "ModelRegistry":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._models: dict[str, ModelEntry] = {}
            inst._main_model_id: str = ""
            inst._coder_model_id: str = ""
            inst._loaded: bool = False
            cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------ #
    # Load / Save                                                          #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Load registry from JSON file. Seeds defaults if the file is absent."""
        if os.path.exists(REGISTRY_FILE):
            try:
                with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._models = {
                    m['id']: ModelEntry(**m)
                    for m in data.get('models', [])
                }
                self._main_model_id  = data.get('main_model_id', '')
                self._coder_model_id = data.get('coder_model_id', '')
                # Validate that referenced IDs still exist
                if self._main_model_id not in self._models and self._models:
                    self._main_model_id = next(iter(self._models))
                if self._coder_model_id not in self._models and self._models:
                    self._coder_model_id = next(iter(self._models))
                self._loaded = True
                print(f"✅ [REGISTRY] Załadowano {len(self._models)} model(i) z {REGISTRY_FILE}")
                return
            except Exception as e:
                print(f"⚠️ [REGISTRY] Błąd ładowania models_config.json: {e} — tworzę domyślny")

        self._seed_defaults()
        self._loaded = True

    def _seed_defaults(self) -> None:
        """Create the two built-in models and save."""
        self._models = {
            "bielik_default": ModelEntry(
                id="bielik_default",
                display_name="Bielik 11B (domyślny)",
                base_url=_DEFAULT_MAIN_URL,
                model_id=_DEFAULT_MAIN_ID,
                api_key="lm-studio",
            ),
            "qwen_default": ModelEntry(
                id="qwen_default",
                display_name="Qwen3 Coder 30B (zdalny)",
                base_url=_DEFAULT_CODER_URL,
                model_id=_DEFAULT_CODER_ID,
                api_key="remote-server",
            ),
        }
        self._main_model_id  = "bielik_default"
        self._coder_model_id = "qwen_default"
        self.save()
        print(f"📝 [REGISTRY] Zainicjalizowano models_config.json z domyślnymi modelami")

    def save(self) -> None:
        """Persist current state to JSON."""
        data = {
            "models": [asdict(m) for m in self._models.values()],
            "main_model_id":  self._main_model_id,
            "coder_model_id": self._coder_model_id,
        }
        with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------ #
    # CRUD                                                                 #
    # ------------------------------------------------------------------ #

    def add_model(
        self,
        display_name: str,
        base_url: str,
        model_id: str,
        api_key: str = "",
    ) -> str:
        """Register a new model. Returns its generated ID."""
        self._ensure_loaded()
        mid = f"model_{int(time.time() * 1000)}"
        # Normalise base_url: ensure it ends with /v1
        url = base_url.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        entry = ModelEntry(
            id=mid,
            display_name=display_name,
            base_url=url,
            model_id=model_id,
            api_key=api_key,
        )
        self._models[mid] = entry
        self.save()
        print(f"➕ [REGISTRY] Dodano model: {display_name} ({url}, {model_id})")
        return mid

    def remove_model(self, mid: str) -> None:
        """Remove a model by ID. Falls back main/coder if they pointed to it."""
        self._ensure_loaded()
        if mid not in self._models:
            return
        del self._models[mid]
        # Update references if needed
        fallback = next(iter(self._models), "")
        if self._main_model_id == mid:
            self._main_model_id = fallback
        if self._coder_model_id == mid:
            self._coder_model_id = fallback
        self.save()

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def get_all(self) -> List[ModelEntry]:
        self._ensure_loaded()
        return list(self._models.values())

    def get_by_id(self, mid: str) -> Optional[ModelEntry]:
        self._ensure_loaded()
        return self._models.get(mid)

    def get_by_label(self, label: str) -> Optional[ModelEntry]:
        """Look up a model by its UI label string (used when settings update fires)."""
        self._ensure_loaded()
        for m in self._models.values():
            if m.label() == label:
                return m
        return None

    def labels(self) -> List[str]:
        """All model labels in insertion order — used to populate Select widgets."""
        self._ensure_loaded()
        return [m.label() for m in self._models.values()]

    # ------------------------------------------------------------------ #
    # Active model assignment                                              #
    # ------------------------------------------------------------------ #

    @property
    def main_model(self) -> Optional[ModelEntry]:
        self._ensure_loaded()
        return self._models.get(self._main_model_id)

    @property
    def coder_model(self) -> Optional[ModelEntry]:
        self._ensure_loaded()
        return self._models.get(self._coder_model_id)

    def main_model_label(self) -> str:
        m = self.main_model
        return m.label() if m else ""

    def coder_model_label(self) -> str:
        m = self.coder_model
        return m.label() if m else ""

    def set_main_model_by_label(self, label: str) -> bool:
        self._ensure_loaded()
        m = self.get_by_label(label)
        if m:
            self._main_model_id = m.id
            self.save()
            print(f"🔄 [REGISTRY] Główny model → {m.display_name}")
            return True
        return False

    def set_coder_model_by_label(self, label: str) -> bool:
        self._ensure_loaded()
        m = self.get_by_label(label)
        if m:
            self._coder_model_id = m.id
            self.save()
            print(f"🔄 [REGISTRY] Model kodera → {m.display_name}")
            return True
        return False

    def set_main_model_by_id(self, mid: str) -> bool:
        self._ensure_loaded()
        if mid in self._models:
            self._main_model_id = mid
            self.save()
            return True
        return False

    def set_coder_model_by_id(self, mid: str) -> bool:
        self._ensure_loaded()
        if mid in self._models:
            self._coder_model_id = mid
            self.save()
            return True
        return False


# Module-level singleton — import this everywhere
registry = ModelRegistry()
