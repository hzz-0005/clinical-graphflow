import re


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.lower()).strip()


def relevance(query: str, weighted_fields: list[tuple[str, float]]) -> float:
    normalized_query = normalize(query)
    latin_tokens = set(re.findall(r"[a-z0-9]+", normalized_query))
    score = 0.0
    for value, weight in weighted_fields:
        candidate = normalize(value)
        if not candidate:
            continue
        if candidate in normalized_query or normalized_query in candidate:
            score += weight * 2
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate))
        score += len(latin_tokens & candidate_tokens) * weight
        chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", candidate)
        score += sum(weight for chunk in chinese_chunks if chunk in normalized_query)
    return round(score, 4)

