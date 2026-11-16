from __future__ import annotations
import hashlib
import re
import zlib
import numpy as np

MONTHS = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december"
          r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?")
DAYS = (r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\.?")
NOISE_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?\b",  
    r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b",                     
    rf"\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",  
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}\s+\d{{4}}\b",    
    rf"\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",             
    r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?(?:\s*[a-z]{2,4}t)?\b",  
    r"\b(?:\d+|an?|one)\s+(?:second|minute|min|hour|hr|day|week|month|year)s?\s+ago\b",
    r"\b(?:just now|today|yesterday)\b",
    rf"\b{DAYS}(?!\w)",
    r"\b(?:updated|published|posted|modified|last updated)\s*:?",
    r"©\s*\d{4}|\bcopyright\s+\d{4}",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)


def extract_main_text(html: str | bytes) -> str:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    text = None
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True, deduplicate=False)
    except Exception:
        text = None
    if not text:
        text = _fallback_text(html)
    return text or ""


def _fallback_text(html: str) -> str:
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
        for bad in doc.xpath("//script|//style|//noscript|//template"):
            bad.drop_tree()
        return doc.text_content()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def normalize(text: str) -> str:
    text = NOISE_RE.sub(" ", text.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_hash(clean_text: str) -> str:
    return hashlib.sha256(clean_text.encode("utf-8")).hexdigest()


def shingles(clean_text: str, k: int = 5) -> set[str]:
    words = clean_text.split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def minhash_bytes(clean_text: str, k: int = 5, num_perm: int = 128) -> bytes:
    from datasketch import MinHash
    m = MinHash(num_perm=num_perm, seed=1)
    for s in shingles(clean_text, k):
        m.update(s.encode("utf-8"))
    return m.hashvalues.astype(np.uint64).tobytes()


def similarity_from_bytes(a: bytes, b: bytes) -> float:
    ha = np.frombuffer(a, dtype=np.uint64)
    hb = np.frombuffer(b, dtype=np.uint64)
    if len(ha) != len(hb) or len(ha) == 0:
        return 0.0
    return float(np.mean(ha == hb))


def analyze_html(html: bytes, k: int = 5, num_perm: int = 128) -> dict:
    main = extract_main_text(html)
    clean = normalize(main)
    return {
        "text_sha256": text_hash(clean),
        "main_text_z": zlib.compress(main.encode("utf-8"), 6),
        "minhash": minhash_bytes(clean, k, num_perm),
        "n_words": len(clean.split()),
    }


def compare_html(html_a: str | bytes, html_b: str | bytes, theta: float = 0.95, k: int = 5, num_perm: int = 128) -> tuple[float, bool]:
    a, b = analyze_html(_b(html_a), k, num_perm), analyze_html(_b(html_b), k, num_perm)
    if a["text_sha256"] == b["text_sha256"]:
        return 1.0, False
    sim = similarity_from_bytes(a["minhash"], b["minhash"])
    return sim, sim < theta


def _b(x):
    return x.encode("utf-8") if isinstance(x, str) else x
