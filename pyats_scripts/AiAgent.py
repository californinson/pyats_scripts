from __future__ import annotations
"""AiAgent – helper class that sends device/raw‑output chunks to a language‑model service
running behind an API service.

-------------------------------
* PEP‑257‑style doc‑strings and PEP 8 compliant formatting.
* Introduced the ``AIAgentError`` exception for consistent error handling.
* Granular **info / warning / error** log messages that callers can follow the
  request/response life‑cycle in pyATS *TaskLog*.
* ``generate()`` and ``get_final_response()`` return a **tuple** – ``(ok: bool, payload: str)`` –
  instead of mutating global state only; the previous behaviour (storing summaries
  in‑memory) is still kept for convenience.
* Requests are funnelled through the private ``_request_ai`` helper that validates
  HTTP status codes, catches connectivity issues and logs the round‑trip latency.

Usage example
-------------
>>> agent = AIAgent()
>>> ok, _ = agent.generate(device="er11", user="lab", raw_output=device_output)
>>> if ok:
...     ok, summary = agent.get_final_response(device="er11", user="lab")
...     print(summary)
... else:
...     print("✅ fallback to rule‑based analysis …")
"""

from textwrap import wrap
import os
import time
import logging
import requests
from typing import Dict, List, Tuple

__all__ = ["AIAgent", "AIAgentError"]

RUNPOD_URL_DEFAULT = "http://<runpod-host>:8000"

# In‑memory cache → {user: {device: {"summary": [str, …]}}}
_DEVICE_CACHE: Dict[str, Dict[str, Dict[str, List[str]]]] = {}

DEFAULT_SYSTEM_PROMPT=(
    "### Role: You are a senior network engineer.\n"
    "### Task: Evaluate and summarize network-device output.\n\n"
)

class AIAgentError(RuntimeError):
    """Raised when communication with the LLM back‑end fails."""


class AIAgent:
    """Utility class that orchestrates prompt chunking & summarisation."""

    #: Approximate chunk size (tokens ~= ¾ characters for latin texts).
    CHUNK_CHAR_LEN = 1500
    #: Number of tokens requested for each generation step.
    MAX_NEW_TOKENS = 512

    def __init__(self, timeout: int = 30, system_prompt: str | None = None) -> None:
        self.base_url = RUNPOD_URL_DEFAULT.rstrip("/")
        self.timeout = timeout
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

        # Configure logger once per class (idempotent)
        self.logger = logging.getLogger(self.__class__.__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            fmt = "%(asctime)s [%(levelname)s] %(name)s – %(message)s"
            handler.setFormatter(logging.Formatter(fmt))
            self.logger.addHandler(handler)
        # Let pyATS / root logger decide the effective level; default INFO for standalone use.
        self.logger.setLevel(logging.INFO)

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _request_ai(self, prompt: str, *, max_tokens: int | None = None) -> str:
        """Send *prompt* to the /generate endpoint and return generated text.

        Raises
        ------
        AIAgentError
            If the HTTP request fails or the response JSON doesn’t contain an
            ``output`` field.
        """
        max_tokens = max_tokens or self.MAX_NEW_TOKENS
        url = f"{self.base_url}/generate"
        payload = {"prompt": prompt, "max_new_tokens": int(max_tokens)}

        self.logger.info("Sending prompt to %s (≈%d chars, max_tokens=%s)", url, len(prompt), max_tokens)
        start_ts = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            self.logger.error("HTTP request failed: %s", exc)
            raise AIAgentError("Failed to reach language‑model API") from exc

        duration = (time.perf_counter() - start_ts) * 1000
        self.logger.info("Received response in %.1f ms [status=%s]", duration, resp.status_code)

        if resp.status_code != 200:
            self.logger.error("Non‑200 response from API: %s – %s", resp.status_code, resp.text[:200])
            raise AIAgentError(f"API returned HTTP {resp.status_code}")

        data = resp.json()
        if "output" not in data:
            self.logger.error("API JSON missing 'output' field: %s", data)
            raise AIAgentError("Malformed API response – missing 'output'")

        return str(data["output"])

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------
    def _ensure_cache(self, user: str, device: str) -> List[str]:
        """Return *modifiable* summary list for *user/device*."""
        if user not in _DEVICE_CACHE:
            _DEVICE_CACHE[user] = {}
        if device not in _DEVICE_CACHE[user]:
            _DEVICE_CACHE[user][device] = {"summary": []}
        return _DEVICE_CACHE[user][device]["summary"]

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------
    def _prepare_payload(self, prompt, raw_output):
        return self.system_prompt + prompt + raw_output

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate(self, *, device: str, user: str, raw_output: str, prompt: str) -> Tuple[bool, str]:
        """Chunk *raw_output*, send each chunk for summarisation, and cache results.

        Returns
        -------
        tuple
            ``(True, "<last_chunk_summary>")`` on success, or ``(False, "<reason>")`` on failure.
        """
        summaries = self._ensure_cache(user, device)
        chunks = wrap(raw_output, self.CHUNK_CHAR_LEN)

        self.logger.info("Processing %d chunk(s) for user=%s, device=%s", len(chunks), user, device)

        try:
            for idx, chunk in enumerate(chunks, 1):
                prompt = self._prepare_payload(prompt, raw_output) + f" (part {idx}/{len(chunks)}):\n{chunk}"
                output = self._request_ai(prompt)
                summaries.append(output)
                self.logger.debug("Chunk %s summary length=%d", idx, len(output))
        except AIAgentError as exc:
            return False, str(exc)

        return True, summaries[-1] if summaries else ""

    def get_final_response(self, *, device: str, user: str) -> Tuple[bool, str]:
        """Combine cached chunk‑summaries into a single report using the LLM.

        Returns ``(ok, summary)`` where *ok* is *False* if the API request fails.
        """
        summaries = self._ensure_cache(user, device)
        if not summaries:
            msg = "No intermediate summaries found; call generate() first."
            self.logger.warning(msg)
            return False, msg

        try:
            prompt = (
                "Combine these summaries into a concise report for a network‑engineering audience:\n\n"
                + "\n---\n".join(summaries)
            )
            final_summary = self._request_ai(prompt)
        except AIAgentError as exc:
            return False, str(exc)

        return True, final_summary