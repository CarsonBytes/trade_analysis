"""LLM factory. OpenAI now, but isolated here so swapping providers later is
a one-file change."""
from __future__ import annotations

import os
import ssl
import httpx
import truststore
from langchain_openai import ChatOpenAI

# This machine runs AV (AVG) that intercepts HTTPS and re-signs certs with a
# local root trusted by Windows but NOT by certifi. So verify against the OS
# trust store, which contains that root. TLS verification stays fully ON --
# we trust exactly what Windows trusts, nothing weakened.
_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_HTTP_CLIENT = httpx.Client(verify=_SSL_CTX, timeout=60.0)


def make_llm(temperature: float = 0.2, model: str | None = None) -> ChatOpenAI:
    """Return a chat model. Low temperature: we want consistent, auditable
    analysis, not creative writing.

    Env:
      OPENAI_API_KEY   (required)
      OPENAI_MODEL     (optional, default gpt-4o-mini)
      OPENAI_BASE_URL  (optional, e.g. point at a compatible endpoint)
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY not set. Put it in analyst/.env or your environment."
        )
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    kwargs = {"model": model, "http_client": _HTTP_CLIENT}
    # gpt-5 / o-series reasoning models only accept the default temperature.
    if not any(model.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")):
        kwargs["temperature"] = temperature
    if os.environ.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    return ChatOpenAI(**kwargs)
