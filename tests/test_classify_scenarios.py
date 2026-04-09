"""Prompt evaluation harness - realistic classification scenarios.

Run mock-based (default, fast, CI-safe):
    pytest tests/test_classify_scenarios.py

Run with real LLM (requires valid OPENROUTER_API_KEY):
    pytest tests/test_classify_scenarios.py -m llm
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import dotenv_values

from telegram_excerpt.llm import classify_batch
from telegram_excerpt.models import BufferedMessage

# ── Real .env values for live LLM tests ─────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


@lru_cache(maxsize=1)
def _load_real_env() -> dict[str, str | None]:
    return dotenv_values(_ENV_FILE) if _ENV_FILE.exists() else {}


_LLM_ENV_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
]


@pytest.fixture(autouse=True)
def _restore_real_env_for_llm(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Restore real .env values when running ``@pytest.mark.llm`` tests."""
    if "llm" not in {m.name for m in request.node.iter_markers()}:
        yield
        return

    real_env = _load_real_env()
    saved = {k: os.environ.get(k) for k in _LLM_ENV_KEYS}
    for k in _LLM_ENV_KEYS:
        if k in real_env and real_env[k] is not None:
            os.environ[k] = real_env[k]

    from telegram_excerpt.config import get_settings

    get_settings.cache_clear()
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    get_settings.cache_clear()


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_messages(
    raw: list[dict[str, str]],
    chat_id: int = -100,
) -> list[BufferedMessage]:
    """Convert scenario message dicts to ``BufferedMessage`` list."""
    base_ts = datetime(2026, 4, 5, 10, 0, tzinfo=UTC)
    return [
        BufferedMessage(
            message_id=i + 1,
            chat_id=chat_id,
            user_id=i + 1,
            user_name=m["user_name"],
            text=m["text"],
            ts=base_ts + timedelta(minutes=i),
        )
        for i, m in enumerate(raw)
    ]


def _fake_client(needs_prd: bool, reason: str = "") -> MagicMock:
    """Build an AsyncOpenAI mock returning the given classification."""
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = json.dumps({"needs_prd": needs_prd, "reason": reason})
    response.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


# ── Scenario catalogue ──────────────────────────────────────────────────

SCENARIOS: list[pytest.ParameterSet] = [
    # ─────────────────── needs_prd = True (actionable) ──────────────────
    pytest.param(
        [
            {
                "user_name": "Mario",
                "text": (
                    "Ragazzi, il login non funziona più da stamattina. "
                    "Errore 500 sul POST /api/auth/login"
                ),
            },
            {
                "user_name": "Luigi",
                "text": (
                    "Confermo, anche io stesso errore. Stack trace punta "
                    "a NullPointerException in AuthService.java:142"
                ),
            },
            {
                "user_name": "Mario",
                "text": "Succede solo con utenti che hanno 2FA attivo",
            },
        ],
        True,
        id="true_bug_report",
    ),
    pytest.param(
        [
            {
                "user_name": "Giulia",
                "text": ("Servirebbe una funzione di export CSV nella dashboard analytics"),
            },
            {
                "user_name": "Marco",
                "text": (
                    "Sì concordo, i clienti lo chiedono spesso. "
                    "Vorrei anche poter filtrare per data"
                ),
            },
            {
                "user_name": "Giulia",
                "text": "Esatto, filtro per data e per tipo di evento",
            },
        ],
        True,
        id="true_feature_request",
    ),
    pytest.param(
        [
            {
                "user_name": "Andrea",
                "text": (
                    "Ho notato che la query per il report mensile impiega "
                    "45 secondi. Potremmo ottimizzare usando una "
                    "materialized view"
                ),
            },
            {
                "user_name": "Sara",
                "text": (
                    "Buona idea, potremmo anche aggiungere un indice "
                    "composto su (tenant_id, created_at)"
                ),
            },
        ],
        True,
        id="true_technical_improvement",
    ),
    pytest.param(
        [
            {
                "user_name": "Paolo",
                "text": ("Il bottone 'Salva' nella pagina profilo non fa niente quando clicco"),
            },
            {
                "user_name": "Paolo",
                "text": "[screenshot: bottone_salva_rotto.png]",
            },
            {
                "user_name": "Elena",
                "text": ("Lo vedo anche io su Chrome 120. Su Firefox funziona"),
            },
        ],
        True,
        id="true_broken_ui",
    ),
    pytest.param(
        [
            {
                "user_name": "Marco",
                "text": (
                    "Dobbiamo integrare il sistema di notifiche con Slack. "
                    "Il cliente Enterprise lo richiede per il go-live "
                    "di maggio"
                ),
            },
            {
                "user_name": "Giulia",
                "text": (
                    "Ok, servirà anche il webhook per gli eventi di pagamento verso il loro ERP"
                ),
            },
        ],
        True,
        id="true_integration_request",
    ),
    pytest.param(
        [
            {
                "user_name": "Luca",
                "text": ("L'API /api/products è passata da 200ms a 3 secondi dopo l'ultimo deploy"),
            },
            {
                "user_name": "Sara",
                "text": (
                    "Ho controllato i log, sembra un N+1 query problem introdotto con la PR #342"
                ),
            },
            {
                "user_name": "Luca",
                "text": ("Dobbiamo fixarlo prima che i clienti se ne accorgano"),
            },
        ],
        True,
        id="true_performance_regression",
    ),
    pytest.param(
        [
            {
                "user_name": "Andrea",
                "text": (
                    "Ho trovato che l'endpoint /api/users/{id} non "
                    "controlla l'autorizzazione. Qualsiasi utente "
                    "autenticato può vedere i dati di altri utenti"
                ),
            },
            {
                "user_name": "Marco",
                "text": ("Cavolo, è un IDOR. Va fixato subito, è critico per GDPR"),
            },
        ],
        True,
        id="true_security_vulnerability",
    ),
    pytest.param(
        [
            {
                "user_name": "Sara",
                "text": (
                    "Propongo di migrare il database da MongoDB a "
                    "PostgreSQL. Abbiamo troppi problemi con le "
                    "transazioni multi-documento"
                ),
            },
            {
                "user_name": "Luca",
                "text": (
                    "Concordo. Potremmo fare la migrazione in 3 fasi: "
                    "prima lo schema, poi il dual-write, infine lo switch"
                ),
            },
            {
                "user_name": "Andrea",
                "text": "Serve un piano dettagliato. Stimo almeno 2 sprint",
            },
        ],
        True,
        id="true_migration_proposal",
    ),
    # ─────────────────── needs_prd = False (not actionable) ─────────────
    pytest.param(
        [
            {"user_name": "Mario", "text": "Ciao a tutti!"},
            {"user_name": "Luigi", "text": "Buongiorno!"},
            {"user_name": "Giulia", "text": "Ehi, buondì 👋"},
        ],
        False,
        id="false_greetings",
    ),
    pytest.param(
        [
            {
                "user_name": "Paolo",
                "text": "Qualcuno viene a pranzo al giapponese oggi?",
            },
            {
                "user_name": "Elena",
                "text": "Sì! Io prendo il solito ramen",
            },
            {
                "user_name": "Marco",
                "text": "Arrivo alle 12:30, tenetemi un posto",
            },
        ],
        False,
        id="false_chitchat_lunch",
    ),
    pytest.param(
        [
            {"user_name": "Luca", "text": "ok"},
            {"user_name": "Sara", "text": "fatto"},
            {"user_name": "Andrea", "text": "perfetto, grazie"},
            {"user_name": "Marco", "text": "👍"},
        ],
        False,
        id="false_confirmations",
    ),
    pytest.param(
        [
            {
                "user_name": "Paolo",
                "text": "Quando il codice compila al primo tentativo 😂",
            },
            {
                "user_name": "Elena",
                "text": "[sticker: celebration_cat.webp]",
            },
            {"user_name": "Marco", "text": "Impossibile, ricontrolla 😄"},
            {
                "user_name": "Giulia",
                "text": "Classico venerdì da developer 😂😂",
            },
        ],
        False,
        id="false_memes_jokes",
    ),
    pytest.param(
        [
            {
                "user_name": "Andrea",
                "text": ("Deploy v2.3.1 completato in produzione, tutto ok"),
            },
            {
                "user_name": "Sara",
                "text": "Metriche stabili, nessun errore nei log",
            },
            {"user_name": "Luca", "text": "Perfetto, buon lavoro team"},
        ],
        False,
        id="false_deploy_ok",
    ),
    pytest.param(
        [
            {
                "user_name": "Marco",
                "text": "Ci vediamo alle 15 per il daily?",
            },
            {
                "user_name": "Giulia",
                "text": "Per me va bene, sono in sala riunioni B",
            },
            {"user_name": "Paolo", "text": "Arrivo 5 minuti in ritardo"},
        ],
        False,
        id="false_meeting_coordination",
    ),
    pytest.param(
        [
            {
                "user_name": "Sara",
                "text": (
                    "FYI: il certificato SSL è stato rinnovato "
                    "automaticamente stamattina. Scade il 2027-04-09. "
                    "Tutto funziona."
                ),
            },
            {"user_name": "Luca", "text": "Grazie per l'info"},
        ],
        False,
        id="false_fyi_informative",
    ),
    # ─────────────────── Edge cases ─────────────────────────────────────
    pytest.param(
        [
            {
                "user_name": "Paolo",
                "text": ("Come funziona il meccanismo di retry nella coda messaggi?"),
            },
            {
                "user_name": "Elena",
                "text": ("Usa exponential backoff con max 5 tentativi, timeout 30s per tentativo"),
            },
            {"user_name": "Paolo", "text": "Ah ok chiaro, grazie"},
        ],
        False,
        id="edge_informational_question",
    ),
    pytest.param(
        [
            {
                "user_name": "Paolo",
                "text": (
                    "Come possiamo migliorare il tempo di risposta "
                    "della ricerca full-text? I clienti si lamentano"
                ),
            },
            {
                "user_name": "Elena",
                "text": ("Potremmo passare a Elasticsearch invece di fare LIKE su PostgreSQL"),
            },
            {
                "user_name": "Paolo",
                "text": "Sì, esploriamo questa opzione",
            },
        ],
        True,
        id="edge_improvement_question",
    ),
    pytest.param(
        [
            {
                "user_name": "Luca",
                "text": "Il sistema è lento oggi...",
            },
            {
                "user_name": "Marco",
                "text": "Anche a me sembra più lento del solito",
            },
        ],
        True,
        id="edge_vague_complaint",
    ),
    pytest.param(
        [
            {
                "user_name": "Giulia",
                "text": "Buongiorno! Com'è andato il weekend?",
            },
            {
                "user_name": "Marco",
                "text": "Bene dai, sono andato al lago",
            },
            {
                "user_name": "Giulia",
                "text": (
                    "Bellissimo! Ah senti, ho notato che le notifiche "
                    "email non partono più da venerdì sera"
                ),
            },
            {
                "user_name": "Marco",
                "text": ("Mmm brutto, forse si è rotto il job cron dopo il deploy di venerdì"),
            },
        ],
        True,
        id="edge_mixed_chitchat_bug",
    ),
    pytest.param(
        [
            {
                "user_name": "Luca",
                "text": ("Ho aperto il ticket PROJ-456 su Jira per il bug del calcolo IVA"),
            },
            {
                "user_name": "Sara",
                "text": "Ok visto, l'ho assegnato a Marco",
            },
            {
                "user_name": "Marco",
                "text": "Ci guardo domani mattina",
            },
        ],
        False,
        id="edge_already_tracked",
    ),
    pytest.param(
        [
            {
                "user_name": "Sara",
                "text": "Sarebbe bello se l'app avesse una dark mode",
            },
            {"user_name": "Andrea", "text": "Eh sì, sarebbe carino"},
        ],
        True,
        id="edge_vague_suggestion",
    ),
    pytest.param(
        [
            {
                "user_name": "Andrea",
                "text": ("L'endpoint /api/orders restituisce 404 per ordini vecchi. È un bug?"),
            },
            {
                "user_name": "Sara",
                "text": (
                    "No, è by design. Gli ordini più vecchi di 90 giorni "
                    "vengono archiviati. Usa /api/archive/orders per quelli"
                ),
            },
            {
                "user_name": "Andrea",
                "text": "Ah perfetto, non lo sapevo. Grazie!",
            },
        ],
        False,
        id="edge_resolved_question",
    ),
]


# ── Mock-based tests (default, CI-safe) ────────────────────────────────


@pytest.mark.parametrize(("messages_raw", "expected"), SCENARIOS)
async def test_classify_scenario_mock(
    messages_raw: list[dict[str, str]],
    expected: bool,
) -> None:
    """Pipeline + parsing works for every scenario (mocked LLM)."""
    msgs = _make_messages(messages_raw)
    client = _fake_client(needs_prd=expected)
    result = await classify_batch(msgs, client=client)
    assert result.needs_prd is expected


# ── Live LLM tests (opt-in: pytest -m llm) ─────────────────────────────


@pytest.mark.llm
@pytest.mark.parametrize(("messages_raw", "expected"), SCENARIOS)
async def test_classify_scenario_llm(
    messages_raw: list[dict[str, str]],
    expected: bool,
) -> None:
    """Call the real LLM and verify the prompt classifies correctly."""
    msgs = _make_messages(messages_raw)
    result = await classify_batch(msgs)  # no mock — real client
    assert result.needs_prd is expected, (
        f"Expected needs_prd={expected}, got {result.needs_prd}. LLM reason: {result.reason}"
    )
