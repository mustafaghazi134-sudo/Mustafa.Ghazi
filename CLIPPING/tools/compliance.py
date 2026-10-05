"""Campaign compliance screening for clip candidates.

Hard topics (politics, religion, drugs, adult, violence, hate) FAIL the candidate with the matched
phrase quoted so you can verify. Softer wording (e.g. a film title containing "war", "high" as in
high school) is only flagged as a concern and still passes - read the transcript line before using.
Misleading/exaggerated-claim detection is heuristic: absolute superlatives and numeric claims get a
concern note so the hook/title you write stays honest.
"""
from __future__ import annotations

import re

HARD = {
    "politics": [r"\b(?:democrat(?:s|ic)?|republican(?:s)?|trump|biden|obama|clinton|election|vote(?:d|rs|ing)?|congress|senat(?:e|or)|president(?:ial)?|white house|liberal(?:s)?|conservative(?:s)?|left.?wing|right.?wing|maga|immigration|abortion|gun control|supreme court|governor|mayor|campaign trail|politic(?:s|al|ian|ians))\b"],
    "religion": [r"\b(?:god|jesus|christ|allah|muslim(?:s)?|islam(?:ic)?|christian(?:s|ity)?|catholic(?:s)?|jew(?:s|ish)?|judaism|bible|quran|koran|church|mosque|synagogue|temple|priest|pastor|rabbi|imam|prayer|praying|pray|sin(?:s|ful|ner)?|heaven|hell|atheis(?:t|m)|religio(?:n|us)|scientolog(?:y|ist)|mormon(?:s)?|faith)\b"],
    "drugs": [r"\b(?:cocaine|coke|heroin|weed|marijuana|cannabis|pot|meth|crack|pills|opioid(?:s)?|oxy|fentanyl|xanax|adderall|mushrooms|shrooms|acid|lsd|ecstasy|molly|mdma|ketamine|drug(?:s|ged)?|dealer|overdose|high as|getting high|stoned|rehab|relaps(?:e|ed)|sober|sobriety|alcoholi(?:c|sm)|addict(?:ed|ion)?|drunk|wasted|hungover|hangover|blackout drunk|binge)\b"],
    "adult": [r"\b(?:sex|sexual|sexy|porn(?:o|ography)?|nude|naked|strip(?:per|club)|hooker|prostitut(?:e|ion)|orgasm|erotic|threesome|one.?night stand|hook(?:ed|ing)? up|sleeping with|slept with|blowjob|dick|pussy|cock|tits|boobs|horny|nsfw|onlyfans)\b"],
    "violence": [r"\b(?:kill(?:ed|ing|er)?|murder(?:ed|er)?|shot|shooting|stab(?:bed|bing)?|gun(?:s)?|knife|beat (?:him|her|them|up)|beat(?:en|ing) up|assault(?:ed)?|punch(?:ed)?|fist ?fight|blood(?:y)?|massacre|terror(?:ist|ism)|bomb(?:ing)?|rape(?:d)?|abuse(?:d)?|suicid(?:e|al)|self.?harm)\b"],
    "hate": [r"\b(?:racis(?:t|m)|nigg(?:a|er)|fag(?:got)?|retard(?:ed)?|tranny|kike|spic|chink|wetback|white power|nazi(?:s)?|hitler|kkk|homophob(?:e|ic|ia)|transphob(?:e|ic|ia)|antisemit(?:ic|ism)|slur|bigot(?:ry)?)\b"],
}
SOFT = {
    "politics": [r"\b(?:government|law(?:s)?|protest(?:s)?|tax(?:es)?|woke|cancel(?:led)? culture|free speech)\b"],
    "drugs/alcohol": [r"\b(?:beer(?:s)?|wine|whiskey|vodka|tequila|bar(?:s)?|party(?:ing)?|cigarette(?:s)?|smok(?:e|ed|ing)|vape|high school)\b"],
    "violence": [r"\b(?:fight(?:s|ing)?|war|dead|death|died|die|hit (?:him|her|me)|attack(?:ed)?|threat(?:s|ened)?|crime|criminal|prison|jail|cop(?:s)?|police|arrest(?:ed)?)\b"],
    "adult": [r"\b(?:dating|girlfriend|boyfriend|affair|divorce|cheat(?:ed|ing)?|kiss(?:ed|ing)?|bed)\b"],
    "religion": [r"\b(?:bless(?:ed)?|holy|soul|spiritual|miracle)\b"],
}
CLAIMS = [r"\b(?:guaranteed?|100 ?%|always works|never fails|the only way|everyone (?:knows|agrees)|scientifically proven|proven fact|best (?:actor|movie|film|director) (?:ever|of all time)|worst (?:actor|movie|film|director) (?:ever|of all time))\b",
          r"\b(?:secretly|exposed|the truth about|what they don't want you to know|caught|leaked)\b"]

_HARD = {k: [re.compile(p, re.I) for p in v] for k, v in HARD.items()}
_SOFT = {k: [re.compile(p, re.I) for p in v] for k, v in SOFT.items()}
_CLAIMS = [re.compile(p, re.I) for p in CLAIMS]


def _hits(patterns, text, limit=3):
    found = []
    for p in patterns:
        for m in p.finditer(text):
            if m.group(0).lower() not in [f.lower() for f in found]:
                found.append(m.group(0))
            if len(found) >= limit:
                return found
    return found


def screen(text: str, hook: str, campaign: dict) -> dict:
    """Returns {"status": "PASS"|"FAIL", "fails": {...}, "concerns": [...], "reason": str}."""
    reject = set(campaign.get("auto_reject_topics", []))
    fails: dict[str, list[str]] = {}
    concerns: list[str] = []
    for topic, pats in _HARD.items():
        if topic in reject or not reject:
            h = _hits(pats, text)
            if h:
                fails[topic] = h
    for topic, pats in _SOFT.items():
        h = _hits(pats, text, 2)
        if h and topic.split("/")[0] not in fails:
            concerns.append(f"{topic} wording ({', '.join(repr(x) for x in h)}) - read the line, likely fine in context")
    claim_hits = _hits(_CLAIMS, hook + " " + text, 2)
    if claim_hits:
        concerns.append(f"possible exaggerated/misleading phrasing ({', '.join(repr(x) for x in claim_hits)}) - keep the title literal")
    if len(hook.split()) <= 3:
        concerns.append("hook is very short - make sure the on-screen hook is not clickbait")

    if fails:
        reason = "; ".join(f"{t}: {', '.join(repr(x) for x in h)}" for t, h in fails.items())
        return {"status": "FAIL", "fails": fails, "concerns": concerns, "reason": f"prohibited topic - {reason}"}
    return {"status": "PASS", "fails": {}, "concerns": concerns,
            "reason": "; ".join(concerns) if concerns else "no concerns found"}


def description_for(candidate: dict, campaign: dict) -> str:
    tpl = campaign.get("description_template")
    if not tpl:
        return ""
    return tpl.format(hook=candidate.get("hook", ""), clip_id=candidate.get("clip_id", ""))
