"""Versioned institution behaviour owned by the application, not deployment input."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from models.institution.common import digest

RETRIEVAL_RULES_VERSION = "TT_PUBLIC_CORPUS_V2"
RETRIEVAL_RULES_ID = str(uuid5(NAMESPACE_URL, "terratrust:" + RETRIEVAL_RULES_VERSION))
PROVIDER_RULES = {
    "version": "TT_GEMINI_PUBLIC_FACTS_V1",
    "provider": "GEMINI",
    "private_data": "PROHIBITED",
    "paid_fallback": False,
}
PUBLIC_SOURCE_HOSTS = frozenset(
    {
        "ipcc.ch",
        "www.ipcc.ch",
        "cea.nic.in",
        "cpcb.nic.in",
        "mnre.gov.in",
        "mohua.gov.in",
        "jalshakti-dowr.gov.in",
        "www.fao.org",
        "www.epa.gov",
    }
)
ACTION_CATALOG = {
    "SOLAR": {
        "category": "ELECTRICITY",
        "prerequisites": [
            "Verify site suitability, metered generation and ownership of energy attributes."
        ],
    },
    "RAINWATER": {
        "category": "WATER",
        "prerequisites": [
            "Verify utilized water, the displaced supply route and pumping/treatment energy."
        ],
    },
    "BIOGAS": {
        "category": "STATIONARY_FUEL",
        "prerequisites": [
            "Verify feedstock, gas composition, useful output and displaced fuel."
        ],
    },
    "COMPOSTING": {
        "category": "WASTE",
        "prerequisites": [
            "Verify feedstock mass basis, actual treatment and the evidenced disposal baseline."
        ],
    },
}


def provider_rules(models):
    """Bind permitted payload handling to the exact configured models for provenance."""
    result = {**PROVIDER_RULES, "models": sorted(set(models))}
    return {"id": PROVIDER_RULES["version"], "hash": digest(result)}


def retention_rules(public_url):
    """Personal deletion preserves separately owned, referenced institution history.

    No legal retention period or provider backup-expiry guarantee is invented.
    Historical plans keep their complete frozen rules and content hashes.
    """
    shared = {
        "duration_days": None,
        "basis": "Preserve shared institution assessment and review provenance independently of a personal account.",
        "legal_hold_rule": "Retain while referenced by institution assessment or review history; manage disposal through the institution records process.",
        "deletion_rule": "RETAIN_SHARED_INSTITUTION_RECORD",
    }
    categories = [
        {"category": category, **shared}
        for category in (
            "EVIDENCE",
            "ANNUAL_AUDIT",
            "ANNUAL_RESULT",
            "TREE_AUDIT",
            "REVIEW_CASE",
        )
    ]
    categories += [
        {
            "category": category,
            "duration_days": 0,
            "basis": "Remove personal content after verified account deletion.",
            "legal_hold_rule": "",
            "deletion_rule": "DELETE_ON_VERIFIED_ACCOUNT_DELETION",
        }
        for category in ("UNBOUND_PERSONAL_UPLOADS", "PERSONAL_ADVISOR_TEXT")
    ]
    config = {
        "categories": categories,
        "backup_expiry_rule": "Provider-managed backups follow the existing backup schedule; reapply recorded account deletions after any restore.",
        "provider_rules": {
            "firebase": "DELETE_IDENTITY",
            "gemini": "NO_PRIVATE_DATA_TRANSMISSION",
        },
        "approved_policy_url": public_url,
    }
    checksum = digest(config)
    return {
        "id": str(uuid5(NAMESPACE_URL, "terratrust:personal-deletion:v1:" + checksum)),
        "logical_key": "TT_PERSONAL_DELETION",
        "version": 1,
        "kind": "RETENTION",
        "status": "ACTIVE",
        "config": deepcopy(config),
        "artifact_hash": checksum,
    }
