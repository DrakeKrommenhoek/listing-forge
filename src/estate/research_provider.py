"""Pluggable comparable-research provider.

Mirrors the shape of ``vision.py``'s provider pattern deliberately: one small
interface, selected by an environment variable, with a safe, free, offline
default that the rest of the pipeline can always fall back to.

Why this exists
----------------
``research.py`` already enforces the hard rule that a comparable without a
source URL is not evidence (``import_worksheet`` rejects it). What this
module adds is an explicit seam for *how* comparables get found in the first
place, so a real search/agentic provider can be plugged in later without
touching ``pipeline.py`` or ``approval.py`` at all -- exactly the same
promise ``get_vision_provider()`` makes for identification.

Only one provider is implemented here: ``ManualQueueResearchProvider``. It
calls no network, costs nothing, and never invents a comparable -- it simply
confirms the CSV worksheet exists and reports the item as queued for a human.
This is the project's explicit, permanent default (see CLAUDE.md: "if
automated research cannot access reliable sold data, create a manual
research queue rather than inventing results").

A real automated provider (an eBay Browse/Marketplace-Insights API client, or
an agentic web-search provider) is intentionally NOT implemented in this
codebase. Per the project's operating constraints, activating a paid API is a
separate, explicit decision the operator makes -- not something this MVP
turns on by default. When one is built, it must:

- Return ``Comparable`` rows with ``needs_confirmation=True``. They may count
  toward confidence scoring but must never unlock the approval gate on their
  own -- see ``approval.prepare_review``'s ``confirmed_comps`` filter.
- Set ``price_type`` honestly (schema.PriceType) -- ``exact`` only for a
  verified completed-sale page; ``hidden`` for a Best-Offer-accepted sale
  where the real price is not published; ``estimated`` for anything without
  a page to point to; ``upper_bound`` for an active asking price.
- Always include a real ``url`` -- ``import_worksheet`` and the approval gate
  both already reject a comparable without one, and an automated provider
  must be held to the same standard, not given a bypass.
- Fall back to ``ManualQueueResearchProvider`` on any failure, exactly like
  ``get_vision_provider()`` falls back to ``mock`` -- never raise out of
  ``get_research_provider()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from estate.schema import ResearchStatus
from estate._compat import get_logger

logger = get_logger(__name__)


@dataclass
class ResearchResult:
    #: Newly proposed comparables, if any. Always empty for the manual-queue
    #: provider. A future automated provider populates this with
    #: needs_confirmation=True rows -- see the module docstring.
    comparables: list = field(default_factory=list)
    status: str = ResearchStatus.NOT_STARTED.value
    provider: str = ""
    notes: str = ""


class ResearchProvider(ABC):
    name = "base"

    @abstractmethod
    def find_comparables(self, item: Any) -> ResearchResult:
        """Look for comparable sales for one item. Never raises; a provider
        that cannot find anything returns an empty, honest ResearchResult
        rather than fabricating a plausible-looking one."""


class ManualQueueResearchProvider(ResearchProvider):
    """The safe, permanent default: no network call, no cost, no invention.

    Ensures the comps worksheet (research.write_worksheet) exists with
    targeted search queries already filled in, and reports the item as
    queued for a human researcher. This is not a placeholder for a future
    default -- it is the intended steady-state behaviour whenever no
    automated source is configured, which is the common case for this
    project (see the module docstring on why an agentic provider is
    deliberately not implemented here).
    """

    name = "manual_queue"

    def find_comparables(self, item: Any) -> ResearchResult:
        from estate import research

        research.write_worksheet(item)
        item_id = getattr(item, "item_id", "")
        logger.info({"action": "research_queued_manual", "item_id": item_id})
        return ResearchResult(
            comparables=[],
            status=ResearchStatus.QUEUED_FOR_MANUAL_RESEARCH.value,
            provider=self.name,
            notes=(
                "No automated research source is configured. A worksheet with "
                "targeted search queries has been prepared for a human "
                "researcher to fill in with real, sourced listings."
            ),
        )


class ExternalJobResearchProvider(ResearchProvider):
    """Hand the item to an out-of-process researcher and import the answer.

    This is the seam an automated web-research provider plugs into without
    anything in ``pipeline.py`` or ``orchestrator.py`` changing. It does no
    network I/O itself, costs nothing, and cannot invent a comparable -- it
    writes a machine-readable job file describing exactly what evidence is
    wanted, and, if a completed results file is already sitting next to it,
    parses that instead.

    The point of the split is trust. Whatever finds the comparables -- an
    eBay API client, an agentic browser, a person with a spreadsheet -- ends
    up going through ONE validated import path with ONE set of rules:

    - a comparable with no source URL is discarded, not stored;
    - every imported row lands ``needs_confirmation=True``, so it can raise
      the confidence score but can never by itself unlock approval;
    - ``price_type`` must be one of schema.PRICE_TYPES, and anything else is
      downgraded to ``estimated`` rather than silently trusted as exact;
    - an active listing is forced to ``upper_bound`` and ``is_sold=False``
      no matter what the file claims, because an asking price is not a sale.

    Set ``ESTATE_RESEARCH_PROVIDER=external_job`` to use it. With no results
    file present it behaves exactly like the manual queue, which is the safe
    default state.
    """

    name = "external_job"

    def find_comparables(self, item: Any) -> ResearchResult:
        from estate import research

        item_id = getattr(item, "item_id", "")
        research.write_worksheet(item)
        job_path = research.write_research_job(item)
        results_path = research.research_results_path(item_id)

        if not results_path.exists():
            logger.info({"action": "research_job_written", "item_id": item_id,
                         "path": str(job_path)})
            return ResearchResult(
                comparables=[],
                status=ResearchStatus.QUEUED_FOR_MANUAL_RESEARCH.value,
                provider=self.name,
                notes=(
                    "A research job describing exactly what evidence is needed has "
                    "been written. Drop the completed results file beside it and "
                    "re-run the item to import them."
                ),
            )

        comparables, errors = research.import_research_results(results_path)
        status = (
            ResearchStatus.COMPLETE.value if comparables
            else ResearchStatus.NEEDS_MORE_EVIDENCE.value
        )
        logger.info({"action": "research_results_imported", "item_id": item_id,
                     "accepted": len(comparables), "rejected": len(errors)})
        return ResearchResult(
            comparables=comparables,
            status=status,
            provider=self.name,
            notes=(
                "Imported %d comparable(s); %d row(s) rejected. Every imported row "
                "still needs human confirmation before it can unlock approval."
                % (len(comparables), len(errors))
            ),
        )


_PROVIDERS = {
    "manual_queue": ManualQueueResearchProvider,
    "external_job": ExternalJobResearchProvider,
}


def get_research_provider(name: str = "") -> ResearchProvider:
    """Factory, selected by ESTATE_RESEARCH_PROVIDER (or an explicit name).

    Falls back to manual_queue -- never raises -- when the requested
    provider is unknown or fails to initialise, mirroring
    vision.get_vision_provider()'s fallback contract.
    """
    from estate._compat import get_settings

    settings = get_settings()
    key = (name or getattr(settings, "estate_research_provider", "") or "manual_queue")
    key = key.strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        logger.error({"action": "research_provider_unknown", "requested": key})
        return ManualQueueResearchProvider()
    try:
        return cls()
    except Exception as exc:  # defensive -- must never take down finalise_draft
        logger.error(
            {"action": "research_provider_init_failed", "provider": key,
             "error_type": type(exc).__name__}
        )
        return ManualQueueResearchProvider()
