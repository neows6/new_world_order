"""
utils/ssl_context.py — Custom SSL context for environments with Norton/Symantec
or other AV products doing SSL inspection.

THE PROBLEM
═══════════
Norton Antivirus (and similar products) intercept all HTTPS traffic, decrypt
it for malware scanning, then re-encrypt with a locally-generated certificate.
The client browser/Python sees a cert signed by "Norton Web/Mail Shield Root"
instead of the real Anthropic/Let's Encrypt cert.

Windows knows about Norton's root (installed by the AV) so PowerShell/Chrome
work fine. But Python's certifi bundle doesn't include corporate AV roots,
so Python can't verify the chain.

Worse: Norton's root cert is technically malformed — its BasicConstraints
extension isn't marked "critical" as required by RFC 5280 §4.2.1.9. Newer
OpenSSL (used by Python 3.13+) enforces this strictly via VERIFY_X509_STRICT.

THE FIX
═══════
1. Extract Norton's root cert from Windows cert store (done by setup script)
2. Append it to certifi's bundle → data/ca_bundle_with_norton.pem
3. Build SSL context with that bundle AND disable VERIFY_X509_STRICT so
   Norton's non-spec cert is tolerated
4. Pass that context to httpx clients used by the Anthropic SDK

This module provides a single helper that returns the correctly-configured
SSL context, with graceful fallback to certifi-only when the custom bundle
is unavailable.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Optional

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
_CUSTOM_BUNDLE = ROOT / "data" / "ca_bundle_with_norton.pem"

_cached_context: Optional[ssl.SSLContext] = None


def get_ssl_context() -> ssl.SSLContext:
    """
    Return an SSL context configured for this machine's cert environment.
    Cached after first call.

    - Uses data/ca_bundle_with_norton.pem when present (Norton-affected machines)
    - Falls back to certifi's default bundle otherwise
    - Always disables VERIFY_X509_STRICT to tolerate corporate AV roots
      that don't mark BasicConstraints as critical
    """
    global _cached_context
    if _cached_context is not None:
        return _cached_context

    if _CUSTOM_BUNDLE.exists():
        ctx = ssl.create_default_context(cafile=str(_CUSTOM_BUNDLE))
        logger.info(f"[SSL] Using custom CA bundle: {_CUSTOM_BUNDLE.name}")
    else:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
            logger.debug("[SSL] Using certifi default bundle")
        except Exception:
            ctx = ssl.create_default_context()
            logger.debug("[SSL] Using system default bundle")

    # Tolerate non-spec-compliant CA certs (Norton, Symantec, Zscaler, etc.)
    # which sometimes omit the BasicConstraints critical flag
    try:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except Exception:
        pass

    _cached_context = ctx
    return ctx


def make_httpx_client(timeout: float = 30.0):
    """Convenience: returns an httpx.Client with the right SSL context."""
    import httpx
    return httpx.Client(verify=get_ssl_context(), timeout=timeout)
