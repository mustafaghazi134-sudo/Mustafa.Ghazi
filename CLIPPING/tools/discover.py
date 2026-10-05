"""Step 3: CLIP DISCOVERY. Rule-based, local, free. No LLM required.

How it works
------------
1. Rebuild sentences from Whisper word timestamps (segments are not reliable sentence units).
2. Slide a window over sentence boundaries: every candidate starts at a sentence start and ends at a
   sentence end, so nothing is cut mid-sentence.
3. Score each window on hook strength, content signals (money, controversy, surprise, emotion,
   argument, humour, advice, story, curiosity), pacing (dead air), standalone-ness and duration fit.
4. Non-max suppression removes overlapping duplicates, then the top N are returned.

Everything is tuned via the LEXICON dict and weights below. Keep it simple and adjust from results.
Architecture note: `rank_candidates()` is the single function a future local LLM (e.g. Ollama) could
re-rank. Nothing else needs to change.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("clipping.discover")

# ----------------------------------------------------------------- lexicons (lower-case regexes)
LEXICON = {
    "money": [r"\$\s?\d", r"\b\d+(?:[.,]\d+)?\s*(?:k|m|million|billion|thousand|grand|percent|%)\b",
              r"\b(?:revenue|profit|salary|income|payout|paid|earn(?:ed|ing|s)?|made|lost|cost|price|worth|rich|broke|money|dollars?|invest(?:ed|ing|ment)?|roi|cash|debt)\b",
              r"\b\d{2,}\b"],
    "controversy": [r"\b(?:wrong|lie|lies|lying|scam|fake|overrated|underrated|nobody|no one|everyone|everybody)\b",
                    r"\b(?:honestly|unpopular opinion|controversial|hot take|the truth is|truth|hate|bullshit|garbage|stupid|dumb|ridiculous|disagree)\b",
                    r"\b(?:should never|never|always|the problem with|is a myth|doesn't work|does not work|waste of)\b"],
    "surprise": [r"\b(?:actually|turns out|crazy|insane|wild|shocking|shocked|unbelievable|surprising|surprised|secret|nobody knows|most people don't|didn't expect|mind.?blow)\b",
                 r"\b(?:what people don't|here's the thing|the reality is|plot twist|believe it or not)\b"],
    "emotion": [r"\b(?:cried|crying|tears|scared|terrified|afraid|love|loved|hated|died|death|dying|depress(?:ed|ion)|anxious|anxiety|proud|ashamed|embarrass(?:ed|ing)|heartbroken|miserable|happiest|worst day|best day|lonely|grateful|regret)\b"],
    "argument": [r"\b(?:no,? no|that's not|that is not|you're wrong|i disagree|come on|not true|that's ridiculous|hold on|let me finish|prove it|absolutely not)\b"],
    "funny": [r"\b(?:haha|lol|hilarious|funny|joke|laugh(?:ed|ing)?|ridiculous|dude|bro|literally)\b", r"\[laugh(?:ter|s)?\]"],
    "advice": [r"\b(?:you should|you need to|the key is|the trick is|the secret is|step one|first thing|rule number|my advice|if you want to|here's how|here is how|the best way|stop doing|start doing|don't ever|never do|always do|tip|lesson|framework|the formula)\b"],
    "story": [r"\b(?:when i was|one time|i remember|years ago|back when|so i|and then|the first time|at the time|i was \d+|growing up|my (?:dad|mom|father|mother|wife|husband|boss|friend)|true story|long story short)\b"],
    "curiosity": [r"\b(?:the thing nobody|what most people|here's why|here is why|the reason|the real reason|what happened next|you won't believe|wait until|the one thing|the biggest mistake|the difference between|nobody talks about)\b",
                  r"^(?:why|how|what if|what's the|what is the|do you know|have you ever|imagine)\b"],
}
_COMPILED = {k: [re.compile(p, re.I) for p in pats] for k, pats in LEXICON.items()}

CATEGORY_WEIGHT = {"money": 9, "controversy": 10, "surprise": 9, "emotion": 8, "argument": 8,
                   "funny": 6, "advice": 8, "story": 6, "curiosity": 10}

WEAK_OPENERS = re.compile(r"^(?:and|so|but|because|or|also|then|like|um+|uh+|yeah|yes|no|okay|ok|right|well|i mean|you know|anyway|which|that's why|that is why)\b", re.I)
DANGLING_OPENERS = re.compile(r"^(?:that|this|it|he|she|they|those|these|he's|she's|it's|they're|that's|this is|which)\b", re.I)
STRONG_OPENERS = re.compile(r"^(?:the (?:truth|reality|problem|reason|thing|biggest|best|worst|only|first)|here's|here is|nobody|no one|everyone|most people|i think|i believe|if you|you|why|how|what|when i|stop|never|always|there's|there is|one of the|the way|people|let me tell you|i'm going to|i will)\b", re.I)
FILLER = re.compile(r"\b(?:um+|uh+|you know|i mean|like,|sort of|kind of)\b", re.I)
TERMINAL = re.compile(r"[.!?][\"')\]]*$")
QUESTION = re.compile(r"\?$")


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list


@dataclass
class Candidate:
    start: float
    end: float
    duration: float
    text: str
    hook: str
    description: str
    score: int
    reasons: list = field(default_factory=list)
    categories: dict = field(default_factory=dict)
    standalone: bool = True
    features: dict = field(default_factory=dict)
    rank: int = 0
    clip_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- sentence building

def build_sentences(transcript: dict) -> list[Sentence]:
    """Flatten all words, then split on terminal punctuation or long pauses."""
    words = []
    for seg in transcript["segments"]:
        if seg.get("words"):
            words.extend(seg["words"])
        else:  # no word timestamps -> treat the segment as one word
            words.append({"start": seg["start"], "end": seg["end"], "word": " " + seg["text"], "prob": 1.0})
    sentences: list[Sentence] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        cur.append(w)
        token = w["word"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = (nxt["start"] - w["end"]) if nxt else 99
        ends = bool(TERMINAL.search(token)) or pause > 1.2
        # Avoid splitting on decimals/abbreviations like "3.5" or "Mr."
        if ends and token and token[-1] == "." and re.fullmatch(r"(?:\d+\.|mr\.|mrs\.|dr\.|vs\.|st\.|e\.g\.|i\.e\.)", token, re.I):
            ends = pause > 1.2
        if ends and len(cur) >= 1:
            sentences.append(_mk_sentence(cur))
            cur = []
    if cur:
        sentences.append(_mk_sentence(cur))
    # Merge very short fragments ("Yeah.", "Right.") into their neighbour so windows don't start on them.
    merged: list[Sentence] = []
    for s in sentences:
        if merged and len(s.words) <= 2 and (s.start - merged[-1].end) < 0.8:
            prev = merged[-1]
            merged[-1] = _mk_sentence(prev.words + s.words)
        else:
            merged.append(s)
    return merged


def _mk_sentence(words: list[dict]) -> Sentence:
    text = "".join(w["word"] for w in words).strip()
    text = re.sub(r"\s+", " ", text)
    return Sentence(start=float(words[0]["start"]), end=float(words[-1]["end"]), text=text, words=list(words))


# ----------------------------------------------------------------- scoring

def _hook_score(first: Sentence) -> tuple[float, list[str]]:
    t = first.text.strip()
    reasons = []
    score = 40.0
    n_words = len(t.split())
    if WEAK_OPENERS.match(t):
        score -= 25
        reasons.append("weak opener")
    if DANGLING_OPENERS.match(t):
        score -= 15
        reasons.append("opens on a reference (needs context)")
    if STRONG_OPENERS.match(t):
        score += 25
        reasons.append("strong opening statement")
    if QUESTION.search(t):
        score += 12
        reasons.append("opens with a question")
    if re.search(r"\byou\b", t, re.I):
        score += 8
    if 6 <= n_words <= 22:
        score += 10
    elif n_words < 4:
        score -= 20
        reasons.append("opening too short")
    elif n_words > 35:
        score -= 10
    for cat in ("controversy", "surprise", "curiosity", "money"):
        if any(p.search(t) for p in _COMPILED[cat]):
            score += 10
            reasons.append(f"{cat} in first line")
            break
    return max(0.0, min(100.0, score)), reasons


def _category_hits(text: str) -> dict[str, int]:
    hits = {}
    for cat, pats in _COMPILED.items():
        n = sum(len(p.findall(text)) for p in pats)
        if n:
            hits[cat] = n
    return hits


def _pacing(sentences: list[Sentence]) -> tuple[float, float, float]:
    """Returns (max_gap_seconds, silence_fraction, words_per_second)."""
    words = [w for s in sentences for w in s.words]
    if len(words) < 2:
        return 0.0, 0.0, 0.0
    gaps = [max(0.0, words[i + 1]["start"] - words[i]["end"]) for i in range(len(words) - 1)]
    total = words[-1]["end"] - words[0]["start"]
    silence = sum(g for g in gaps if g > 0.5)
    return max(gaps), (silence / total if total else 0.0), (len(words) / total if total else 0.0)


def score_window(sentences: list[Sentence], cfg: dict) -> Candidate:
    first, last = sentences[0], sentences[-1]
    text = " ".join(s.text for s in sentences)
    duration = last.end - first.start
    reasons: list[str] = []
    features: dict = {}

    hook, hook_reasons = _hook_score(first)
    reasons += hook_reasons
    features["hook"] = round(hook, 1)

    hits = _category_hits(text)
    minutes = max(duration / 60.0, 0.5)  # floor: no bias toward very short windows
    content = 0.0
    for cat, n in hits.items():
        content += CATEGORY_WEIGHT[cat] * min(n, 3) / minutes * 0.33
    content += 2.0 * min(len(hits), 4)  # variety bonus
    content = min(content, 34.0)
    features["content"] = round(content, 1)
    top_cats = sorted(hits.items(), key=lambda kv: -kv[1] * CATEGORY_WEIGHT[kv[0]])[:3]
    reasons += [f"{cat} signal x{n}" for cat, n in top_cats]

    max_gap, silence_frac, wps = _pacing(sentences)
    pacing = 0.0
    if max_gap > 2.5:
        pacing -= 12
        reasons.append(f"dead air {max_gap:.1f}s")
    if silence_frac > 0.25:
        pacing -= 10
        reasons.append("lots of pauses")
    if wps >= 2.2:
        pacing += 5
    filler_n = len(FILLER.findall(text))
    if filler_n / max(1, len(text.split())) > 0.06:
        pacing -= 6
        reasons.append("filler-heavy")
    features.update({"max_gap": round(max_gap, 2), "silence_frac": round(silence_frac, 3), "wps": round(wps, 2)})

    ending = 0.0
    if not TERMINAL.search(last.text):
        ending -= 8
        reasons.append("ending not a clean sentence")
    elif len(last.text.split()) >= 5:
        ending += 4
    if re.search(r"\b(?:so yeah|anyway|anyways|moving on|next question)\W*$", last.text, re.I):
        ending -= 6

    lo, hi = cfg["sweet_spot"]
    if lo <= duration <= hi:
        dur_fit = 8.0
    else:
        off = (lo - duration) if duration < lo else (duration - hi)
        dur_fit = max(-12.0, 8.0 - off * 0.6)
    features["duration_fit"] = round(dur_fit, 1)

    standalone = hook >= 45 and not DANGLING_OPENERS.match(first.text) and not WEAK_OPENERS.match(first.text)
    if standalone:
        reasons.append("understandable on its own")

    raw = hook * 0.42 + content + pacing + ending + dur_fit + (6 if standalone else -6)
    score = int(round(max(0.0, min(100.0, raw))))

    hook_text = _tidy_hook(first.text)
    desc = _tidy_desc(text)
    return Candidate(start=round(first.start, 2), end=round(last.end, 2), duration=round(duration, 2), text=text,
                     hook=hook_text, description=desc, score=score, reasons=reasons, categories=hits,
                     standalone=bool(standalone), features=features)


def _tidy_hook(text: str, max_len: int = 90) -> str:
    t = FILLER.sub("", text).strip(" ,")
    t = re.sub(r"\s+", " ", t)
    if len(t) > max_len:
        cut = t[:max_len].rsplit(" ", 1)[0]
        t = cut + "..."
    return t[:1].upper() + t[1:] if t else ""


def _tidy_desc(text: str, max_len: int = 220) -> str:
    t = re.sub(r"\s+", " ", FILLER.sub("", text)).strip()
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0] + "..."
    return t


# ----------------------------------------------------------------- windowing + selection

def generate_windows(sentences: list[Sentence], cfg: dict) -> list[Candidate]:
    min_d, max_d = float(cfg["min_duration"]), float(cfg["max_duration"])
    out: list[Candidate] = []
    n = len(sentences)
    for i in range(n):
        if (sentences[i].end - sentences[i].start) < 0.8:
            continue
        for j in range(i, n):
            dur = sentences[j].end - sentences[i].start
            if dur > max_d:
                break
            if dur < min_d:
                continue
            # Don't bridge a long silence (likely a topic change / cut).
            if j > i and (sentences[j].start - sentences[j - 1].end) > 3.0:
                break
            out.append(score_window(sentences[i:j + 1], cfg))
    return out


def dedupe(cands: list[Candidate], max_overlap: float) -> list[Candidate]:
    cands = sorted(cands, key=lambda c: (-c.score, c.start))
    kept: list[Candidate] = []
    for c in cands:
        ok = True
        for k in kept:
            inter = max(0.0, min(c.end, k.end) - max(c.start, k.start))
            if inter / min(c.duration, k.duration) > max_overlap:
                ok = False
                break
        if ok:
            kept.append(c)
    return kept


def rank_candidates(cands: list[Candidate], cfg: dict) -> list[Candidate]:
    """Single extension point: swap/augment this to re-rank with a local LLM later."""
    return dedupe(cands, float(cfg["max_overlap"]))


def discover(transcript: dict, cfg: dict, *, top_n: int | None = None, source_duration: float | None = None) -> list[Candidate]:
    dcfg = cfg["discovery"]
    top_n = top_n or int(dcfg["top_n"])
    sentences = build_sentences(transcript)
    LOG.info("Built %d sentences from transcript.", len(sentences))
    windows = generate_windows(sentences, dcfg)
    LOG.info("Scored %d candidate windows.", len(windows))
    ranked = rank_candidates(windows, dcfg)[:top_n]
    lead_in, lead_out = float(dcfg["lead_in"]), float(dcfg["lead_out"])
    total = source_duration or (transcript.get("duration") or 0) or None
    for r, c in enumerate(ranked, 1):
        c.rank = r
        c.start = round(max(0.0, c.start - lead_in), 2)
        c.end = round(min(total, c.end + lead_out) if total else c.end + lead_out, 2)
        c.duration = round(c.end - c.start, 2)
        c.clip_id = f"{transcript['source_id']}_{int(c.start):05d}"
    return ranked
