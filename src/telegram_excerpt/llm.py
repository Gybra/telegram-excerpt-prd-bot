"""LLM integration via OpenRouter.

Two steps:

1. :func:`classify_batch` — decides whether the batch of messages
   contains at least one concrete request/problem/proposal worth
   generating a PRD for. Returns JSON ``{needs_prd, reason}``.
2. :func:`generate_prds` — if necessary, produces one or more structured
   Markdown PRDs, one per distinct topic. Each PRD is associated with
   the ``message_id`` of the triggering message.

The integration uses the ``openai`` Python SDK configured with the
OpenRouter ``base_url``, as per the project convention.

Note: the system prompts below are intentionally in Italian. Fork the
repo and adapt them to your language/domain as needed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from openai import APIError, AsyncOpenAI

from telegram_excerpt.config import get_settings
from telegram_excerpt.exceptions import LLMClassificationError, LLMGenerationError
from telegram_excerpt.logging_conf import get_logger
from telegram_excerpt.models import ClassifyResult, PRDDoc

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram_excerpt.models import BufferedMessage

log = get_logger(__name__)


# ─── Prompts (Italian, domain-specific — adapt by forking) ────────────

_CLASSIFY_SYSTEM_PROMPT = """\
Sei un analista tecnico. Ricevi una conversazione da un gruppo Telegram
di sviluppo software e devi decidere se contiene almeno una richiesta,
problema, bug, proposta o argomento concreto e azionabile che giustifica
la creazione di un PRD (Product Requirements Document).

Rispondi ESCLUSIVAMENTE con JSON valido nella forma:
{"needs_prd": true|false, "reason": "breve motivazione in italiano"}

Regole:
- needs_prd=true solo se c'è contenuto concreto e azionabile
  (bug, richiesta feature, miglioramento, domanda tecnica rilevante).
- needs_prd=false per saluti, chitchat, meme, conferme, messaggi vuoti,
  off-topic, o conversazioni puramente informative senza azione richiesta.
"""

_GENERATE_SYSTEM_PROMPT = """\
Sei un analista tecnico che riceve una conversazione da un gruppo Telegram
di sviluppo software.

La conversazione può contenere PIÙ richieste, problemi o argomenti distinti.
Il tuo compito:
1. Identificare OGNI richiesta/problema/argomento separato.
2. Produrre un PRD INDIPENDENTE per ciascuno.
3. Associare ogni PRD al message_id del messaggio TRIGGER (quello che
   solleva la richiesta; in caso di ambiguità, l'ultimo messaggio che
   tratta quell'argomento).

Rispondi ESCLUSIVAMENTE con JSON valido nella forma:
{"prds": [
  {
    "title": "titolo breve e descrittivo",
    "trigger_message_id": 12345,
    "markdown": "contenuto markdown completo del PRD"
  },
  ...
]}

Il campo "markdown" deve seguire questo formato:

# PRD: [Titolo]

**Autore:** Nome utente che ha sollevato il tema.
**Tipo:** Bug Fix | Nuova Feature | Miglioramento | Analisi | Domanda | Altro
**Impatto:** Alto | Medio | Basso

## Contesto
Contesto specifico di QUESTA richiesta.

## Problema / Esigenza
- Comportamento attuale (se bug)
- Comportamento atteso
- Perché è importante

## Requisiti funzionali
Requisiti concreti, specifici e azionabili.

## Criteri di accettazione
Condizioni misurabili da soddisfare.

## Note e dipendenze
Vincoli, dipendenze, info aggiuntive. "N/A" se non applicabile.

Scrivi in italiano. Non omettere sezioni (usa "N/A" se serve).
Se la conversazione ha un solo argomento, restituisci un solo PRD.
"""


# ─── Client ───────────────────────────────────────────────────────────


def _build_client() -> AsyncOpenAI:
    """Build the OpenRouter client from current settings."""
    settings = get_settings()
    return AsyncOpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key.get_secret_value(),
    )


def _format_messages(messages: Sequence[BufferedMessage]) -> str:
    """Flatten messages for the user prompt."""
    lines: list[str] = []
    for m in messages:
        ts = m.ts.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[msg_id={m.message_id} | {ts} | {m.user_name}] {m.text}")
    return "\n".join(lines)


# ─── Public API ───────────────────────────────────────────────────────


async def classify_batch(
    messages: Sequence[BufferedMessage],
    *,
    client: AsyncOpenAI | None = None,
) -> ClassifyResult:
    """Classify whether the message batch warrants a PRD.

    Args:
        messages: Buffered messages of the batch.
        client: Injectable AsyncOpenAI (for tests); built if None.

    Returns:
        ``ClassifyResult`` with ``needs_prd`` and ``reason``.

    Raises:
        LLMClassificationError: If the model returns invalid JSON.
    """
    if not messages:
        return ClassifyResult(needs_prd=False, reason="batch vuoto")

    settings = get_settings()
    oai = client or _build_client()
    user_content = _format_messages(messages)

    try:
        response = await oai.chat.completions.create(
            model=settings.openrouter_model,
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except APIError as exc:
        raise LLMClassificationError(f"OpenRouter API error: {exc}") from exc

    if not response.choices:
        raise LLMClassificationError("empty choices from LLM")
    content = response.choices[0].message.content
    if not content:
        raise LLMClassificationError("empty content from LLM")

    parsed = _safe_json_loads(content)
    if "needs_prd" not in parsed:
        raise LLMClassificationError(f"missing needs_prd in LLM response: {content!r}")

    result = ClassifyResult(
        needs_prd=bool(parsed["needs_prd"]),
        reason=str(parsed.get("reason", "")),
    )
    log.info(
        "llm.classify.done",
        needs_prd=result.needs_prd,
        reason=result.reason,
        n_messages=len(messages),
    )
    return result


async def generate_prds(
    messages: Sequence[BufferedMessage],
    *,
    chat_title: str = "",
    client: AsyncOpenAI | None = None,
) -> list[PRDDoc]:
    """Generate PRDs from the message batch.

    Args:
        messages: Buffered messages of the batch.
        chat_title: Group title (inserted in the send caption).
        client: Injectable AsyncOpenAI for tests.

    Returns:
        List of ``PRDDoc``. Can be empty if the model finds no distinct
        topics despite classify returning true.

    Raises:
        LLMGenerationError: If the model returns unparsable JSON.
    """
    if not messages:
        return []

    settings = get_settings()
    oai = client or _build_client()
    user_content = _format_messages(messages)

    try:
        response = await oai.chat.completions.create(
            model=settings.openrouter_model,
            messages=[
                {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except APIError as exc:
        raise LLMGenerationError(f"OpenRouter API error: {exc}") from exc

    if not response.choices:
        raise LLMGenerationError("empty choices from LLM")
    content = response.choices[0].message.content
    if not content:
        raise LLMGenerationError("empty content from LLM")

    parsed = _safe_json_loads(content)
    items = parsed.get("prds")
    if not isinstance(items, list):
        raise LLMGenerationError(f"missing 'prds' array in response: {content!r}")

    # Map message_id → (user, ts) to enrich each PRD.
    by_id = {m.message_id: m for m in messages}

    prds: list[PRDDoc] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip() or "PRD senza titolo"
        md = str(raw.get("markdown", "")).strip()
        if not md:
            continue
        trigger_id = _coerce_trigger_id(raw.get("trigger_message_id"))
        trigger_msg = by_id.get(trigger_id) if trigger_id else None
        # Fallback: last message of the batch.
        if trigger_msg is None:
            trigger_msg = messages[-1]
            trigger_id = trigger_msg.message_id
        prds.append(
            PRDDoc(
                title=title,
                markdown=md,
                trigger_message_id=trigger_id,
                trigger_user=trigger_msg.user_name,
                trigger_ts=trigger_msg.ts,
            )
        )

    log.info(
        "llm.generate.done",
        n_prds=len(prds),
        n_messages=len(messages),
        chat_title=chat_title,
    )
    return prds


def _coerce_trigger_id(raw: Any) -> int | None:
    """Best-effort coercion of an LLM-returned ``trigger_message_id``.

    The model may emit the id as int or as string. Returns ``None`` if
    the value cannot be coerced to an integer.
    """
    if not isinstance(raw, int | str):
        return None
    as_str = str(raw).lstrip("-")
    if not as_str.isdigit():
        return None
    return int(raw)


def _safe_json_loads(s: str) -> dict[str, Any]:
    """Tolerant JSON parsing (handles ```json fences and whitespace)."""
    stripped = s.strip()
    if stripped.startswith("```"):
        # strip ```json ... ``` fences
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LLMClassificationError(f"invalid JSON from LLM: {s!r}") from exc
    if not isinstance(data, dict):
        raise LLMClassificationError(f"expected JSON object, got: {type(data).__name__}")
    return data
