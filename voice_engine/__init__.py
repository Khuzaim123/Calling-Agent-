"""Real-time voice engine.

Scope (by architecture decision): live audio, VAD, STT, LLM streaming, TTS,
barge-in and session transport. Business data and decisions live in n8n and
are reached through `voice_engine.business.BusinessGateway`.
"""

__version__ = "0.1.0"
