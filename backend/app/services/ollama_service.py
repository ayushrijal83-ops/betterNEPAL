"""Self-hosted Ollama, across three nodes.

Why the ``openai`` package
--------------------------

Ollama serves an OpenAI-compatible API at ``/v1``, so the official client is
simply the most maintained implementation of that wire format. It is a protocol
client here, not a dependency on OpenAI the company: the ``base_url`` points at
a machine on your own network and no traffic leaves it. The API key is a
placeholder because Ollama ignores it, but the client requires one to be set.

Why three nodes
---------------

Work is sent to hardware suited to it rather than to whichever box answers:

* **vision node** (GPU) - image analysis, and the conversational assistant,
  which needs a larger model to produce prose worth reading.
* **text node** (CPU) - classification and severity extraction. Short prompts,
  structured output, a 3B model is plenty, and it keeps the GPU free.
* **embed node** (CPU) - vectors for duplicate detection.

Each role degrades on its own. A configured text node with an unreachable
vision node still classifies reports; only the assistant reports unavailable.
That is deliberate - one machine being off should not take the platform's AI
down wholesale.

Trust
-----

Nothing changes about how model output is treated. Everything comes back
through the same ``extract_json`` / ``parse_analysis`` validators the Gemini
provider uses, so a locally-hosted model that invents a category has that field
dropped exactly as a hosted one would. Running the model yourself removes the
privacy and cost problems; it does not make its output true.
"""
from __future__ import annotations

import time
from typing import Any

from flask import current_app

from .ai_service import (
    AIUnavailable,
    BaseAIService,
    DuplicateVerdict,
    ReportAnalysis,
    build_analysis_prompt,
    build_comparison_prompt,
    extract_json,
    parse_analysis,
    parse_duplicate_verdict,
)

# Ollama ignores the key, but the OpenAI client refuses to start without one.
PLACEHOLDER_API_KEY = "OLLAMA_DUMMY_KEY"

# Roles, so a caller asks for a capability rather than a machine.
ROLE_VISION = "vision"
ROLE_TEXT = "text"
ROLE_EMBED = "embed"

# Classification wants the same answer every time for the same report; prose
# does not. Temperature is set per role rather than globally for that reason.
TEMPERATURE_STRUCTURED = 0.0
TEMPERATURE_CHAT = 0.4

MAX_CHAT_TOKENS = 500
MAX_STRUCTURED_TOKENS = 400


class _Node:
    """One Ollama endpoint, with a cached health verdict.

    The cache matters: without it, an unreachable node adds its full connect
    timeout to *every* request that touches it. One switched-off laptop would
    make the whole API feel broken. A failed node is left alone for
    ``OLLAMA_HEALTH_TTL_SECONDS`` before being probed again.
    """

    def __init__(self, role: str, base_url: str, model: str, timeout: int) -> None:
        self.role = role
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = None
        # One timestamp for both outcomes: while `now` is before it, the last
        # verdict stands. A success means "assume healthy until then"; a
        # failure means "do not probe again until then".
        self._verdict_until = 0.0
        self._last_verdict = False
        self._last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _endpoint(self) -> str:
        """Ollama's OpenAI-compatible surface lives under /v1."""
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._endpoint(),
                api_key=PLACEHOLDER_API_KEY,
                timeout=self.timeout,
                # One attempt. The client's default retries would multiply an
                # already-generous CPU timeout into a request nobody waits for.
                max_retries=0,
            )
        return self._client

    def healthy(self, ttl: int) -> bool:
        """Is this node answering? Cached for ``ttl`` seconds."""
        if not self.configured:
            return False

        now = time.monotonic()
        if now < self._verdict_until:
            return self._last_verdict

        try:
            self.client().models.list()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._last_verdict = False
        else:
            self._last_error = None
            self._last_verdict = True

        self._verdict_until = now + ttl
        return self._last_verdict

    def describe(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "configured": self.configured,
            "base_url": self.base_url or None,
            "model": self.model,
            "last_error": self._last_error,
        }


class OllamaService(BaseAIService):
    """Multi-node local inference."""

    name = "ollama"

    def __init__(
        self,
        vision_url: str = "",
        text_url: str = "",
        embed_url: str = "",
        vision_model: str = "qwen2.5:7b",
        text_model: str = "qwen2.5:3b",
        embed_model: str = "nomic-embed-text",
        timeout: int = 120,
        health_ttl: int = 60,
    ) -> None:
        self._health_ttl = health_ttl
        self._nodes = {
            ROLE_VISION: _Node(ROLE_VISION, vision_url, vision_model, timeout),
            ROLE_TEXT: _Node(ROLE_TEXT, text_url, text_model, timeout),
            ROLE_EMBED: _Node(ROLE_EMBED, embed_url, embed_model, timeout),
        }

    @classmethod
    def from_config(cls, config) -> "OllamaService":
        return cls(
            vision_url=config.get("OLLAMA_VISION_NODE", ""),
            text_url=config.get("OLLAMA_TEXT_NODE", ""),
            embed_url=config.get("OLLAMA_EMBED_NODE", ""),
            vision_model=config.get("OLLAMA_VISION_MODEL", "qwen2.5:7b"),
            text_model=config.get("OLLAMA_TEXT_MODEL", "qwen2.5:3b"),
            embed_model=config.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
            timeout=config.get("OLLAMA_TIMEOUT_SECONDS", 120),
            health_ttl=config.get("OLLAMA_HEALTH_TTL_SECONDS", 60),
        )

    @property
    def available(self) -> bool:
        """True when any node is configured.

        Deliberately not a live health check: ``available`` is read on hot
        paths (statistics, duplicate search) and must not make a network call.
        Reachability is proven when a request is actually made.
        """
        return any(node.configured for node in self._nodes.values())

    # --- node selection ----------------------------------------------------

    def _node_for(self, role: str, fallback: str | None = None) -> _Node:
        """The node for a role, falling back to another when it is absent.

        A single-machine deployment configures one node and everything routes
        there; that is a normal setup, not a degraded one.
        """
        node = self._nodes[role]
        if node.configured:
            return node

        if fallback:
            alternative = self._nodes[fallback]
            if alternative.configured:
                return alternative

        raise AIUnavailable(
            f"No Ollama node is configured for {role}. "
            f"Set OLLAMA_{role.upper()}_NODE."
        )

    def _require(self, role: str, fallback: str | None = None) -> _Node:
        node = self._node_for(role, fallback)
        if not node.healthy(self._health_ttl):
            raise AIUnavailable(
                f"The Ollama {node.role} node at {node.base_url} is not responding."
            )
        return node

    # --- generation --------------------------------------------------------

    def _complete(
        self,
        node: _Node,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        force_json: bool = False,
    ) -> str:
        """One chat completion. Returns the raw text."""
        kwargs: dict[str, Any] = {
            "model": node.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if force_json:
            # Ollama honours this for models that support it. Best-effort, not
            # a guarantee - which is why the response still goes through
            # extract_json, and why a model that ignores it degrades to the
            # same "no usable suggestion" path rather than failing.
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = node.client().chat.completions.create(**kwargs)
        except Exception as exc:
            current_app.logger.warning(
                "Ollama %s node failed: %s: %s", node.role, type(exc).__name__, exc
            )
            # Node details stay in the log. A client learns the capability is
            # unavailable, not the internal address of the machine.
            raise AIUnavailable(
                f"The Ollama {node.role} node could not be reached."
            ) from None

        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise AIUnavailable(
                f"The Ollama {node.role} node returned an unreadable response."
            ) from None

        if not text or not text.strip():
            raise AIUnavailable(f"The Ollama {node.role} node returned nothing.")
        return text

    # --- BaseAIService -----------------------------------------------------

    def analyze_report_text(self, title: str, description: str) -> ReportAnalysis:
        """Classify a report on the text node.

        Falls back to the vision node when no text node is configured, so a
        one-machine setup still works.
        """
        node = self._require(ROLE_TEXT, fallback=ROLE_VISION)
        raw = self._complete(
            node,
            build_analysis_prompt(title, description),
            temperature=TEMPERATURE_STRUCTURED,
            max_tokens=MAX_STRUCTURED_TOKENS,
            force_json=True,
        )
        analysis = parse_analysis(extract_json(raw), model=node.model)
        analysis.raw_response = raw
        return analysis

    def compare_reports(self, report_a, report_b) -> DuplicateVerdict:
        """Judge duplication on the text node - a short, structured question."""
        node = self._require(ROLE_TEXT, fallback=ROLE_VISION)
        raw = self._complete(
            node,
            build_comparison_prompt(
                report_a.title, report_a.description, report_b.title, report_b.description
            ),
            temperature=TEMPERATURE_STRUCTURED,
            max_tokens=MAX_STRUCTURED_TOKENS,
            force_json=True,
        )
        return parse_duplicate_verdict(extract_json(raw), model=node.model)

    def chat(self, prompt: str) -> str:
        """The assistant, on the GPU node.

        Prose for a citizen needs a larger model than classification does, and
        this is the one call where a slow answer is visibly slow to a person
        waiting for it.
        """
        node = self._require(ROLE_VISION, fallback=ROLE_TEXT)
        return self._complete(
            node,
            prompt,
            temperature=TEMPERATURE_CHAT,
            max_tokens=MAX_CHAT_TOKENS,
        )

    # --- extras ------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """A vector for semantic duplicate detection.

        Present and working, but nothing calls it yet: duplicate detection is
        still proximity plus an LLM judgement (Phase 10). Wiring pgvector is a
        separate change, and this is the piece it will need.
        """
        node = self._require(ROLE_EMBED, fallback=ROLE_TEXT)
        try:
            response = node.client().embeddings.create(
                model=node.model, input=text[:4000]
            )
            return list(response.data[0].embedding)
        except Exception as exc:
            current_app.logger.warning("Ollama embed node failed: %s", exc)
            raise AIUnavailable("The Ollama embedding node could not be reached.") from None

    def health(self) -> dict[str, Any]:
        """Per-node status, for an operator diagnosing a quiet AI."""
        return {
            "provider": self.name,
            "available": self.available,
            "nodes": {
                role: {
                    **node.describe(),
                    "reachable": node.healthy(self._health_ttl) if node.configured else False,
                }
                for role, node in self._nodes.items()
            },
        }
