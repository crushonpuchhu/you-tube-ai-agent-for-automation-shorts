"""Hard source-rights gate.

Search ranking, public visibility and owner approval are **not** permission to
download or reuse a video.  This module answers one question only:

    Is there a verified basis that lets the owner reuse this media?

Supported bases
---------------
* ``owner_media``             – the owner supplied the file with a rights note
* ``creator_permission``      – explicit permission recorded by the owner
* ``creator_allowlist``       – the source channel is on ``SOURCE_ALLOWLIST``
* ``verified_creative_commons`` – YouTube reports the video as CC-licensed

Everything else is ``unknown`` and must be skipped.  A rights-uncertain clip is
never downloaded, never queued and never published, even after owner approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

BASIS_OWNER_MEDIA = "owner_media"
BASIS_CREATOR_PERMISSION = "creator_permission"
BASIS_CREATOR_ALLOWLIST = "creator_allowlist"
BASIS_CREATIVE_COMMONS = "verified_creative_commons"
BASIS_UNKNOWN = "unknown"

VERIFIED_BASES = {
    BASIS_OWNER_MEDIA,
    BASIS_CREATOR_PERMISSION,
    BASIS_CREATOR_ALLOWLIST,
    BASIS_CREATIVE_COMMONS,
}

BASIS_LABELS = {
    BASIS_OWNER_MEDIA: "Owner-provided media (rights confirmed by owner)",
    BASIS_CREATOR_PERMISSION: "Explicit creator permission recorded by owner",
    BASIS_CREATOR_ALLOWLIST: "Creator/channel allowlist configured by owner",
    BASIS_CREATIVE_COMMONS: "Verified Creative Commons licence reported by YouTube",
    BASIS_UNKNOWN: "No verified reuse basis — download/publish blocked",
}


@dataclass(frozen=True)
class SourcePolicy:
    """Owner-configured source policy."""

    allowlist: tuple[str, ...] = ()
    allow_download: bool = False
    owner_media_dir: Path | None = None

    @classmethod
    def from_config(cls, aconf: Any) -> "SourcePolicy":
        return cls(
            allowlist=tuple(getattr(aconf, "source_allowlist", ()) or ()),
            allow_download=bool(getattr(aconf, "allow_download", False)),
            owner_media_dir=getattr(aconf, "source_dir", None),
        )


@dataclass(frozen=True)
class RightsDecision:
    allowed: bool
    verified: bool
    basis: str
    notes: str = ""
    evidence: dict | None = None

    @property
    def label(self) -> str:
        return BASIS_LABELS.get(self.basis, self.basis)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "verified": self.verified,
            "basis": self.basis,
            "label": self.label,
            "notes": self.notes,
            "evidence": self.evidence or {},
        }


def _normalise_license(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _allowlist_match(source: dict, allowlist: Iterable[str]) -> str:
    """Return the matching allowlist entry, or an empty string."""
    channel = str(source.get("channel") or "").strip().lower()
    channel_id = str(source.get("channel_id") or "").strip().lower()
    url = str(source.get("url") or "").strip().lower()
    values = {value for value in (channel, channel_id, url) if value}
    haystack = " | ".join(sorted(values))
    for entry in allowlist or ():
        item = str(entry or "").strip().lower()
        if not item:
            continue
        if item in values or item in haystack:
            return item
    return ""


def evaluate_source(source: dict, policy: SourcePolicy | None = None) -> RightsDecision:
    """Evaluate the reuse basis for one discovered source."""
    policy = policy or SourcePolicy()
    source = source or {}
    source_type = str(source.get("source_type") or "").strip().lower()
    confirmed = bool(source.get("rights_confirmed") or source.get("confirmed"))
    explicit = bool(source.get("explicit_permission") or source.get("creator_permission"))
    licence = _normalise_license(str(source.get("license") or ""))
    basis_hint = str(source.get("rights_basis") or "").strip().lower()

    evidence = {
        "source_type": source_type,
        "video_id": source.get("video_id"),
        "url": source.get("url"),
        "channel": source.get("channel"),
        "license": source.get("license"),
        "rights_basis_hint": basis_hint,
        "rights_confirmed": confirmed,
        "explicit_permission": explicit,
    }

    # 1) Owner-supplied file with an explicit rights confirmation sidecar.
    if source_type == "owner_media" and (confirmed or basis_hint == BASIS_OWNER_MEDIA):
        return RightsDecision(
            allowed=True,
            verified=True,
            basis=BASIS_OWNER_MEDIA,
            notes="File provided by the owner with a rights confirmation.",
            evidence=evidence,
        )

    # 2) Explicit permission recorded by the owner (allowlisted creator, DM, etc.).
    if explicit or basis_hint == BASIS_CREATOR_PERMISSION:
        return RightsDecision(
            allowed=True,
            verified=True,
            basis=BASIS_CREATOR_PERMISSION,
            notes="The owner recorded explicit permission from the creator.",
            evidence=evidence,
        )

    # 3) YouTube itself reports a Creative Commons licence.
    if licence in {"creativecommon", "creativecommons", "cc", "ccby", "ccbysa"}:
        return RightsDecision(
            allowed=True,
            verified=True,
            basis=BASIS_CREATIVE_COMMONS,
            notes=(
                "YouTube reports this video under a Creative Commons licence. "
                "Attribution requirements still apply and must be included in "
                "the published description."
            ),
            evidence=evidence,
        )

    # 4) Creator/channel allowlist configured by the owner.
    matched = _allowlist_match(source, policy.allowlist)
    if matched:
        return RightsDecision(
            allowed=True,
            verified=True,
            basis=BASIS_CREATOR_ALLOWLIST,
            notes=f"Source channel matches SOURCE_ALLOWLIST entry {matched!r}.",
            evidence=evidence,
        )

    # Public visibility, view counts and owner approval deliberately do NOT
    # appear here: they are not a reuse licence.
    return RightsDecision(
        allowed=False,
        verified=False,
        basis=BASIS_UNKNOWN,
        notes=(
            "No verified reuse basis. Public visibility, search ranking and "
            "owner approval are not permission to download or reuse."
        ),
        evidence=evidence,
    )


def can_download(decision: RightsDecision, source: dict, policy: SourcePolicy) -> bool:
    """Whether media may be downloaded/copied for processing."""
    if not decision.allowed or not decision.verified:
        return False
    if str(source.get("source_type") or "").lower() == "owner_media":
        # Nothing to download: the owner's own file is used in place.
        return True
    return bool(policy.allow_download)


def can_publish(decision: RightsDecision) -> bool:
    """A clip may only be published when the reuse basis is verified."""
    return bool(decision.allowed and decision.verified)


def attach_rights(
    sources: Iterable[dict], policy: SourcePolicy | None = None
) -> tuple[list[dict], list[dict]]:
    """Split candidates into (eligible, rejected) with a ``rights`` snapshot."""
    policy = policy or SourcePolicy()
    eligible: list[dict] = []
    rejected: list[dict] = []
    for source in sources:
        decision = evaluate_source(source, policy)
        enriched = dict(source)
        enriched["rights"] = decision.to_dict()
        if decision.allowed and decision.verified:
            eligible.append(enriched)
        else:
            rejected.append(enriched)
    return eligible, rejected
