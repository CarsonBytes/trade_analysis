"""Network/TLS bootstrap. IMPORT THIS FIRST, before any HTTP library.

This machine runs AVG antivirus, which intercepts HTTPS and re-signs every
cert with a local root that Windows trusts but certifi/libcurl do not. Two
layers need fixing:

  1. Python ssl (httpx, openai, urllib) -> truststore uses the OS trust store.
  2. libcurl (curl_cffi, used by yfinance) and requests -> they don't honour
     truststore, so we export the Windows root store into a PEM bundle and
     point CURL_CA_BUNDLE / REQUESTS_CA_BUNDLE / SSL_CERT_FILE at it.

After import, all HTTPS in the process verifies correctly with NOTHING
disabled. If you ever move off this machine it still works (the bundle just
contains the standard roots plus whatever the OS adds).
"""
from __future__ import annotations

import os
import sys
import ssl
import pathlib
import certifi
import truststore

_BUNDLE = pathlib.Path(__file__).resolve().parent / "winca.pem"
# Put the parent quant/ dir on the path so we can reuse features.py, analyst/, data.py
_QUANT_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
if _QUANT_DIR not in sys.path:
    sys.path.insert(0, _QUANT_DIR)


def _build_bundle() -> pathlib.Path:
    parts = [pathlib.Path(certifi.where()).read_bytes()]
    seen: set[bytes] = set()
    # enum_certificates is Windows-only; on other OSes we just use certifi.
    if hasattr(ssl, "enum_certificates"):
        for store in ("ROOT", "CA"):
            try:
                for der, _enc, _trust in ssl.enum_certificates(store):
                    if der in seen:
                        continue
                    seen.add(der)
                    parts.append(ssl.DER_cert_to_PEM_cert(der).encode())
            except Exception:
                pass
    _BUNDLE.write_bytes(b"\n".join(parts))
    return _BUNDLE


def bootstrap() -> None:
    truststore.inject_into_ssl()  # fixes httpx / openai / urllib
    if not _BUNDLE.exists():
        _build_bundle()
    bundle = str(_BUNDLE)
    for var in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        os.environ.setdefault(var, bundle)
    # Load the LLM credentials from the analyst package's .env (OPENAI_API_KEY etc.)
    try:
        from dotenv import load_dotenv
        load_dotenv(pathlib.Path(_QUANT_DIR) / "analyst" / ".env")
    except Exception:
        pass


bootstrap()
