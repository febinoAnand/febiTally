"""Suggest a Tally ledger for a bank statement narration, and learn from validated entries."""
import re

# Tokens that appear in almost every narration and say nothing about the counterparty.
STOPWORDS = {
    "upi", "neft", "rtgs", "imps", "ach", "nach", "ecs", "pos", "atm", "cash", "wdl", "dep", "chq", "cheque",
    "transfer", "trf", "to", "from", "by", "for", "the", "and", "ltd", "pvt", "limited", "private", "bank",
    "payment", "paid", "received", "inb", "mob", "net", "banking", "ref", "txn", "cr", "dr", "debit", "credit",
    "ok", "okaxis", "okhdfcbank", "okicici", "oksbi", "ybl", "paytm", "ibl", "axl", "apl", "rev", "mb",
}


def keywords(narration):
    """Significant tokens: alphabetic words of 4+ chars that are not boilerplate."""
    tokens = re.split(r"[^A-Za-z]+", narration or "")
    return [t.lower() for t in tokens if len(t) >= 4 and t.lower() not in STOPWORDS]


def suggest(narration, ledger_names, rules):
    """Return the best ledger name for a narration, or ''.

    rules: {keyword: ledger} learned for the company.
    Order: learned keyword rule, then a ledger name contained in the narration (longest wins).
    """
    valid = set(ledger_names)
    for kw in keywords(narration):
        ledger = rules.get(kw)
        if ledger and ledger in valid:
            return ledger
    text = " " + re.sub(r"[^a-z0-9]+", " ", (narration or "").lower()) + " "
    best = ""
    for name in ledger_names:
        needle = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if len(needle) >= 4 and (" " + needle + " ") in text and len(needle) > len(best):
            best = name
    return best


def learn(narration, ledger, max_keywords=2):
    """Keyword -> ledger pairs to remember from a validated entry (first significant tokens,
    which for UPI/NEFT narrations are usually the payee name)."""
    return [(kw, ledger) for kw in keywords(narration)[:max_keywords]]
