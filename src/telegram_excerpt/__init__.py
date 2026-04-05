"""telegram-excerpt: multi-bot Telegram → PRD generator.

Monitors multiple Telegram groups through dynamically configurable bots,
buffers messages in Firestore, and after a period of silence uses an LLM
(via OpenRouter) to classify the conversation and generate structured
PRDs, delivered as .md files to the admin.
"""

__version__ = "0.1.0"
