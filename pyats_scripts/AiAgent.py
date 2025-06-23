from __future__ import annotations

from ixnetwork_restpy.samples.sessions.linux_sessions import api_key

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

DEFAULT_SYSTEM_PROMPT = (
    "### Role: You are a senior network engineer.\n"
    "### Task: Evaluate and summarise network-device output.\n\n"
)

# in-memory cache user → device → { "summary": [...] }
_DEVICE_CACHE: Dict[str, Dict[str, Dict[str, List[str]]]] = {}


class AIAgentError(RuntimeError):
    """Raised when communication with the LLM back-end fails."""


class AIAgent:
    """Send log chunks to an LLM service and keep the intermediate summaries."""

    CHUNK_CHAR_LEN = 1_500          # ≈ 2 kB – safe for 512 tokens/model-context
    MAX_NEW_TOKENS = 512

    # --------------------------------------------------------------------- #
    # constructor & helpers                                                 #
    # --------------------------------------------------------------------- #
    def __init__(self, *, ai_host: str | None = None, ai_host_port: str | None = None,
                 timeout: int = 30, system_prompt: str | dict | None = None, api_key: str | None = None
                 ) -> None:
        self.base_url = self._set_ai_host_url(ai_host, ai_host_port)
        self.timeout = timeout
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.api_key=api_key

        self.logger = logging.getLogger(self.__class__.__name__)
        if not self.logger.handlers:  # keeps idempotent if the module is re-loaded
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s – %(message)s"))
            self.logger.addHandler(h)
        self.logger.setLevel(logging.INFO)

    def _set_system_prompt(self, system_prompt):
        self.system_prompt=system_prompt

    def _set_ai_host_url(self, ai_host: str | None, ai_host_port: int | str | None) -> str:
        if(not ai_host):
            self.logger.error("AI agent host can't be None")
            raise AIAgentError("Error while adding AI agent host.")

        ai_host_port = ai_host_port or None

        if(ai_host_port):
            ai_host_full_url=f"http://{ai_host}:{ai_host_port}"
        else:
            ai_host_full_url = f"http://{ai_host}"

        return ai_host_full_url

    def _prepare_payload(self, user_prompt: str, chunk: str) -> str:
        """Compose the final prompt (`system` + user + chunk)."""
        ai_host=self.base_url

        if("cloudlfare" in ai_host):
            full_prompt=[
                self.system_prompt,
                { "role": "user", "content": user_prompt}
            ]
        else:
            full_prompt =f"<s>[INST] {self.system_prompt}{user_prompt}\n\n{chunk} [/INST]"

        return full_prompt

    def _request_ai(self, prompt: str, *, max_tokens: int | None = None) -> str:
        max_tokens = max_tokens or self.MAX_NEW_TOKENS

        if("cloudflare" in self.base_url):
            url=self.base_url
        else:
            url = f"{self.base_url}/generate"

        api_key=self.api_key

        self.logger.info(f"HTTP LLM API: {url}")

        headers=None

        if(api_key):
            headers= {f"Authorization": "Bearer {api_key}"}

        if("cloudflare" in self.base_url):
            payload= { "messages": prompt }
        else:
            payload = {"prompt": prompt, "max_new_tokens": max_tokens}

        self.logger.info("POST %s – len(prompt)=%d, max_tokens=%s", url, len(prompt), max_tokens)
        start = time.perf_counter()
        try:
            if ("cloudflare" in self.base_url):
                resp = requests.post(url, headers=headers, json=input)
            else:
                resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            self.logger.error("HTTP error contacting LLM API: %s", exc)
            raise AIAgentError("Network error talking to LLM API") from exc

        rtt = (time.perf_counter() - start) * 1_000
        self.logger.info("LLM answered HTTP %s in %.1f ms", resp.status_code, rtt)

        if resp.status_code != 200:
            raise AIAgentError(f"LLM API returned HTTP {resp.status_code}: {resp.text[:120]}")

        data = resp.json()
        if "output" not in data and "cloudflare" not in url:
            raise AIAgentError("LLM API JSON missing 'output' field")

        if("cloudflare" not in url):
            response = str(data["output"])
            if(prompt in response):
                response=response.replace(prompt,'')
        else:
            response=str(data['result']['response'])

        return response.strip()
        #return str(data["output"])

    # --------------------------------------------------------------------- #
    # cache utilities                                                       #
    # --------------------------------------------------------------------- #
    def _ensure_cache(self, user: str, device: str) -> List[str]:
        if user not in _DEVICE_CACHE:
            _DEVICE_CACHE[user] = {}
        if device not in _DEVICE_CACHE[user]:
            _DEVICE_CACHE[user][device] = {"summary": []}
        return _DEVICE_CACHE[user][device]["summary"]

    # --------------------------------------------------------------------- #
    # public API                                                            #
    # --------------------------------------------------------------------- #
    def generate(self, *, device: str, user: str, raw_output: str, prompt: str) -> Tuple[bool, str]:
        """Send **each chunk** of *raw_output* to the LLM.

        Returns ``(True, last_chunk_summary)`` on success or ``(False, reason)``.
        """
        summaries = self._ensure_cache(user, device)
        chunks = wrap(raw_output, self.CHUNK_CHAR_LEN)

        self.logger.info("Analysing %d chunk(s) for user=%s device=%s", len(chunks), user, device)

        try:
            for idx, chunk in enumerate(chunks, 1):
                full_prompt = self._prepare_payload(
                    f"{prompt} (part {idx}/{len(chunks)})", chunk
                )
                output = self._request_ai(full_prompt)
                summaries.append(output)
                self.logger.debug("Chunk %s → summary %d chars", idx, len(output))
        except AIAgentError as exc:
            return False, str(exc)

        return True, summaries[-1] if summaries else ""

    def get_final_response(self, *, device: str, user: str) -> Tuple[bool, str]:
        """Ask the LLM to merge the intermediate summaries into a concise report."""
        summaries = self._ensure_cache(user, device)
        if not summaries:
            msg = "No intermediate summaries found – call generate() first."
            self.logger.warning(msg)
            return False, msg

        try:
            merge_prompt = (
                "Combine these partial summaries into a single, concise report "
                "for a network-engineering audience:\n\n"
                + "\n---\n".join(summaries)
            )
            final = self._request_ai(self._prepare_payload(merge_prompt, ""))

            return True, final
        except AIAgentError as exc:
            return False, str(exc)