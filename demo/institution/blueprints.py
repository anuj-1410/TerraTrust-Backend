"""Institution stages from demo.md; farmer accounts 1–4 are unchanged."""

BLUEPRINTS = {
    5: {
        "checkpoint": "INSTITUTION_FRESH",
        "reset_policy": "NEXT_LOGIN",
        "landing": "Institution Setup",
    },
    6: {
        "checkpoint": "INSTITUTION_APPROVED",
        "reset_policy": "NEXT_LOGIN",
        "landing": "Institution Dashboard",
    },
    7: {
        "checkpoint": "CAMPUS_READY",
        "reset_policy": "NEXT_LOGIN",
        "landing": "Campus Dashboard",
    },
    8: {
        "checkpoint": "BIOMASS_COMPLETE_FORMS_PENDING",
        "reset_policy": "NEXT_LOGIN",
        "landing": "Campus Dashboard",
    },
    9: {
        "checkpoint": "ACCOUNTING_COMPLETE_TWO_YEARS",
        "reset_policy": "PERSISTENT",
        "landing": "Institution Results",
    },
}
KEYS = {number: row["checkpoint"] for number, row in BLUEPRINTS.items()}


def phone_for(account):
    if account not in BLUEPRINTS:
        raise ValueError("Institution demo account must be 5 through 9.")
    return f"+91900000000{account}"


def reset_policy(account):
    return BLUEPRINTS[account]["reset_policy"]
