from __future__ import annotations
"""
CloudflareAIAgent - helper class for interacting with the Cloudflare Workers AI service
using a specific LLM (e.g. Llama-3-8b-instruct) via HTTPS API.

This module is designed for network test automation scenarios (e.g. via PyATS),
where large CLI outputs (e.g. BGP tables, configs) are sent to an LLM for summarization.

------------------------------------------------------
Key Features:
* Sends raw device output to the Cloudflare LLM API in character-limited chunks.
* Uses an in-memory cache to associate AI-generated summaries with user/device.
* Provides `generate()` to collect summaries, and `get_final_response()` to merge them.
* Controls output length via `max_tokens=1024` to prevent truncation.
* Logs API request timing, payloads, and raw responses for traceability.

Usage Example:
--------------
>>> agent = CloudflareAIAgent(ai_model="meta/llama-3-8b-instruct", api_key="...")
>>> ok, _ = agent.generate(device="er11", user="lab", raw_output=cli_output, prompt="Summarise BGP routes")
>>> if ok:
...     ok, summary = agent.get_final_response(device="er11", user="lab")
...     print(summary)
>>> else:
...     print("Fallback to rule-based analysis…")
"""

from textwrap import wrap
import os
import time
import logging
import requests
from typing import Dict, List, Tuple

__all__ = ["CloudflareAIAgent", "CloudflareAIAgentError"]

DEFAULT_SYSTEM_PROMPT= {"role": "system", "content": "You are a senior network engineer. "
                              "Provide feedback on network-device output."
 }

API_BASE_URL="https://api.cloudflare.com/client/v4/accounts/937f912241bfc6f8bcb7e7b8e1ad3543/ai/run/"

# in-memory cache user → device → { "summary": [...] }
_DEVICE_CACHE: Dict[str, Dict[str, Dict[str, List[str]]]] = {}


class CloudflareAIAgentError(RuntimeError):
    """Raised when communication with the LLM back-end fails."""


class CloudflareAIAgent:
    # Max number of characters per chunk of CLI output to avoid overly large LLM prompts.
    # Cloudflare LLM API has a token limit (~8K tokens), but we chunk based on characters.
    CHUNK_CHAR_LEN = 6_144

    # --------------------------------------------------------------------- #
    # constructor & helpers                                                 #
    # --------------------------------------------------------------------- #
    def __init__(self, *, ai_model: str | None = None,
                 timeout: int = 30, system_prompt: str | dict | None = None, api_key: str | None = None
                 ) -> None:
        self.base_url = self._set_ai_host_url(ai_model)
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

    def _set_ai_host_url(self, ai_model: str | None) -> str:
        if(not ai_model):
            self.logger.error("AI agent host can't be None")
            raise CloudflareAIAgentError("Error while adding AI agent host.")

        ai_host_full_url = f"{API_BASE_URL}{ai_model}"

        return ai_host_full_url

    def _prepare_payload(self, user_prompt: str, chunk: str) -> List:
        """Compose the final prompt with system + user message format required by Cloudflare API."""
        full_prompt = [
            self.system_prompt,
            {"role": "user", "content": user_prompt+'\n\n'+chunk}
        ]

        return full_prompt

    def _request_ai(self, prompt: List) -> str:
        # Send the prompt to Cloudflare's LLM endpoint using HTTPS POST.
        # Uses a `curl`-style User-Agent to mimic CLI behavior (helps with reliability).
        # Sets `max_tokens=1024` to ensure full output is returned and not truncated.
        url=self.base_url

        api_key=self.api_key

        self.logger.info(f"HTTPS Cloudflare LLM API: {url}")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "curl/8.1.2"
        }

        self.logger.info("POST %s – len(prompt)=%d", url, len(prompt))
        start = time.perf_counter()
        try:
            self.logger.info(f"**** PROMPT being sent:\n{prompt}\n")

            inputs={
                "messages": prompt,
                "max_tokens": 1024
            }

            resp = requests.post(url, headers=headers, json=inputs, timeout=90, stream=True)
        except requests.RequestException as exc:
            self.logger.error("HTTPS error contacting Cloudflare LLM API: %s", exc)
            raise CloudflareAIAgentError("Network error talking to Cloudflare LLM API") from exc

        rtt = (time.perf_counter() - start) * 1_000
        self.logger.info("Cloudflare LLM answered HTTPS %s in %.1f ms", resp.status_code, rtt)

        if resp.status_code != 200:
            raise CloudflareAIAgentError(f"Cloudflare LLM API returned HTTPS {resp.status_code}: {resp.text[:120]}")

        data = resp.json()
        self.logger.info("=== RAW RESPONSE TEXT ===\n%s", resp.text)
        response=str(data['result']['response'])

        return response.strip()

    # --------------------------------------------------------------------- #
    # cache utilities                                                       #
    # --------------------------------------------------------------------- #
    def _ensure_cache(self, user: str, device: str) -> List[str]:
        # Create or retrieve the in-memory summary cache for a given user/device pair.
        if user not in _DEVICE_CACHE:
            _DEVICE_CACHE[user] = {}
        if device not in _DEVICE_CACHE[user]:
            _DEVICE_CACHE[user][device] = {"summary": []}
        return _DEVICE_CACHE[user][device]["summary"]

    # --------------------------------------------------------------------- #
    # public API                                                            #
    # --------------------------------------------------------------------- #
    def generate(self, *, device: str, user: str, raw_output: str, prompt: str) -> Tuple[bool, str]:
        # Split large raw CLI output into character-limited chunks and send each to the LLM.
        # Each chunk is treated as an independent request for summarization.
        summaries = self._ensure_cache(user, device)
        chunks = [raw_output[i:i + self.CHUNK_CHAR_LEN] for i in range(0, len(raw_output), self.CHUNK_CHAR_LEN)]

        self.logger.info("Analysing %d chunk(s) for user=%s device=%s", len(chunks), user, device)

        try:
            for idx, chunk in enumerate(chunks, 1):
                full_prompt = self._prepare_payload(
                    f"{prompt} (part {idx}/{len(chunks)})", chunk
                )
                output = self._request_ai(full_prompt)
                summaries.append(output)
                self.logger.debug("Chunk %s → summary %d chars", idx, len(output))
        except CloudflareAIAgentError as exc:
            return False, str(exc)

        return True, summaries[-1] if summaries else ""

    def get_final_response(self, *, device: str, user: str) -> Tuple[bool, str]:
        # If multiple chunks were summarized, ask the LLM to combine them into a final summary.
        # Otherwise, return the only chunk summary directly.
        summaries = self._ensure_cache(user, device)
        if not summaries:
            msg = "No intermediate summaries found – call generate() first."
            self.logger.warning(msg)
            return False, msg

        #If len(summaries)<2 it means the raw output was not chunked as it was less than 6144 characters
        if(len(summaries)<2):
            final=''.join(summaries)

            return True, final
        else:
            try:
                merge_prompt = (
                    "Use one or more summaries to combine into a single, concise report "
                    "for a network-engineering audience. Do not omit important details.\n\n"
                    + "\n---\n".join(summaries)
                )
                final = self._request_ai(self._prepare_payload(merge_prompt, ""))

                return True, final
            except CloudflareAIAgentError as exc:
                return False, str(exc)