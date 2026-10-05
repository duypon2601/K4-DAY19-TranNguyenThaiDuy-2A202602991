"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Two ontologies live in this file; the env var KG_ONTOLOGY picks one (default "own"):

    own   my design, documented in report/ONTOLOGY.md (sections marked OWN)
    hint  the suggested ontology of the lab (sections marked HINT), kept as the baseline that
          produced ket_qua_benchmark_kg.hint.txt:  KG_ONTOLOGY=hint python bench_kg.py --judge --out ...

Own ontology (Crime is still the bridge, but it is reached per person through a reified Charge):

    (:Article {id, title, law, doc_id, max_clause, max_penalty})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text, supplementary, doc_id})
    (:Clause)-[:HAS_THRESHOLD {point, min_g, max_g, text}]->(:Substance {name, aliases})
    (:Report {doc_id, name, published})-[:REPORTS]->(:Case {id, name, summary, date})
    (:Case)-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount, grams}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {key, name, aliases})-[:INVOLVED_IN {role}]->(:Case)
    (:Person)-[:FACES]->(:Charge {id, name, role, stage, sentence, doc_id})-[:FOR_CRIME]->(:Crime)
    (:Charge)-[:IN_CASE]->(:Case)

Suggested ontology (HINT):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_normalized = {normalize(k): k for k in known}
    if target in by_normalized:
        return by_normalized[target]
    close = difflib.get_close_matches(target, list(by_normalized), n=1, cutoff=0.8)
    return by_normalized[close[0]] if close else None

def use_hint_ontology() -> bool:
    return os.getenv("KG_ONTOLOGY", "own").strip().lower() == "hint"

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# OWN — my ontology: extraction helpers (see report/ONTOLOGY.md)
# ----------------------------------------------------------------------------------------------

# Canonical substance -> aliases used by Vietnamese news. One real substance = one node.
SUBSTANCE_ALIASES = {
    "Heroine": ["heroin", "bạch phiến"],
    "Cocaine": ["cocain", "côcain"],
    "Methamphetamine": ["ma túy đá", "meth", "hàng đá", "đá"],
    "Amphetamine": [],
    "MDMA": ["thuốc lắc", "ecstasy", "kẹo"],
    "XLR-11": [],
    "Ketamine": ["ketamin", "ke", "khay"],
    "cần sa": ["cỏ", "cần sa khô", "cần sa thảo mộc"],
    "nhựa cần sa": [],
    "nhựa thuốc phiện": ["thuốc phiện"],
    "cao côca": [],
    "lá côca": [],
    "quả thuốc phiện khô": [],
    "quả thuốc phiện tươi": [],
}
GENERIC_SUBSTANCES = {"", "ma túy", "chất ma túy", "ma túy tổng hợp", "ma túy các loại", "tiền chất"}
# Not named in BLHS: their weight falls under "các chất ma túy khác ở thể rắn".
OTHER_SOLID_SUBSTANCES = ["Ketamine"]
NAMED_SYNTHETICS = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11"]
ACCUSED_ROLES = {"bị cáo", "bị can", "nghi phạm"}
STAGES = ["bắt giữ", "khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm"]

POINT_LINE = re.compile(r"^([a-zđ])\)\s+(.+)$", re.MULTILINE)
THRESHOLD = re.compile(
    r"^(?P<subject>.+?) có (?:khối lượng|thể tích) (?:từ )?(?P<low>[\d.,]+) (?P<unit>gam|kilôgam)"
    r"(?: đến dưới (?P<high>[\d.,]+) (?P<high_unit>gam|kilôgam)| trở lên)")
AMOUNT = re.compile(r"(\d+(?:[.,]\d+)*)\s*(kg|kilôgam|kilogam|kilogram|ký|gam|gram|g)\b", re.IGNORECASE)

def normalize_substance(name: str) -> str:
    """'Ma túy đá (Methamphetamine)' -> 'ma túy đá'; 'loại MDMA' -> 'mdma'."""
    name = re.sub(r"\(.*?\)", " ", name.lower())
    name = re.sub(r"^(loại|chất|ma túy loại|ma túy dạng)\s+", "", re.sub(r"\s+", " ", name).strip())
    return name.strip(" .,;:\"'“”")

def crime_key(name: str) -> str:
    """Looser key for the bridge: 'mua bán ma túy' and 'mua bán trái phép chất ma túy' -> 'mua bán ma túy'."""
    return re.sub(r"\s+", " ", re.sub(r"\b(trái phép|chất)\b", " ", normalize_crime(name))).strip()

def link_crime(name: str, known: list[str]) -> str | None:
    """KG-1 twice: strict spelling match first, then the looser key journalists' short forms need."""
    return link_entity(name, known) or link_entity(name, known, normalize=crime_key)

def link_substance(name: str) -> str | None:
    """Canonical substance for a free-text mention; None for generic words ('ma túy'); unknown names are kept."""
    cleaned = normalize_substance(name or "")
    if cleaned in GENERIC_SUBSTANCES:
        return None
    lookup = {alias: canonical for canonical, aliases in SUBSTANCE_ALIASES.items() for alias in [canonical, *aliases]}
    hit = link_entity(cleaned, list(lookup), normalize=normalize_substance)
    if hit:
        return lookup[hit]
    for match in re.findall(r"\((.*?)\)", name):          # "ma túy đá (Methamphetamine)" -> try the bracket too
        hit = link_entity(match, list(lookup), normalize=normalize_substance)
        if hit:
            return lookup[hit]
    return cleaned

def find_canonical_substances(text: str) -> list[str]:
    """Canonical substances named in a question, by canonical name or alias (aliases of >= 4 letters only)."""
    lowered = text.lower()
    return [canonical for canonical, aliases in SUBSTANCE_ALIASES.items()
            if any(n.lower() in lowered for n in [canonical, *(a for a in aliases if len(a) >= 4)])]

def to_number(text: str) -> float:
    """Vietnamese number: '0,1' -> 0.1, '05' -> 5, '9.600' -> 9600."""
    return float(text.replace(".", "").replace(",", "."))

def amount_to_grams(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0; 'gần 406g' -> 406.0; '5 viên' -> None (not a weight)."""
    match = AMOUNT.search(amount or "")
    if not match:
        return None
    value = to_number(match.group(1))
    return value * 1000 if match.group(2).lower() in {"kg", "kilôgam", "kilogam", "kilogram", "ký"} else value

def threshold_substances(subject: str) -> list[str]:
    """Which Substance nodes a weight-threshold point of the law is about."""
    lowered = subject.lower()
    if "ở thể lỏng" in lowered:
        return []                                           # millilitres: news never reports volume here
    if "ở thể rắn" in lowered:
        return OTHER_SOLID_SUBSTANCES
    if lowered.startswith("nhựa thuốc phiện"):
        return ["nhựa thuốc phiện", "nhựa cần sa", "cao côca"]
    if "cây cần sa" in lowered:
        return ["lá côca", "cần sa"]
    if "quả thuốc phiện khô" in lowered:
        return ["quả thuốc phiện khô"]
    if "quả thuốc phiện tươi" in lowered:
        return ["quả thuốc phiện tươi"]
    return [name for name in NAMED_SYNTHETICS if name.lower() in lowered]

def parse_thresholds(clause_text: str) -> list[dict]:
    """Weight thresholds of one clause: one row per (point, substance), weights in grams."""
    rows = []
    for point, line in POINT_LINE.findall(clause_text):
        match = THRESHOLD.match(line)
        if not match:
            continue
        scale = lambda value, unit: to_number(value) * (1000 if unit == "kilôgam" else 1)
        low = scale(match["low"], match["unit"])
        high = scale(match["high"], match["high_unit"]) if match["high"] else None
        text = line[match.start("low") - 3 if line[match.start("low") - 3:match.start("low")] == "từ " else match.start("low"):match.end()]
        rows += [{"point": point, "substance": s, "min_g": low, "max_g": high, "text": text.strip()}
                 for s in threshold_substances(match["subject"])]
    return rows

def penalty_rank(penalty: str) -> float:
    """Order penalty frames: tử hình > chung thân > the largest number of years."""
    if "tử hình" in penalty:
        return 1000
    if "chung thân" in penalty:
        return 500
    return max((float(y) for y in re.findall(r"(\d+) năm", penalty)), default=0)

def parse_law_article_own(doc: Document) -> dict[str, Any]:
    """parse_law_article + weight thresholds per clause + the highest penalty frame of the article."""
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["thresholds"] = parse_thresholds(clause["text"])
        clause["supplementary"] = "còn có thể bị" in clause["text"].splitlines()[0]
    main = [c for c in article["clauses"] if c["penalty"] and not c["supplementary"]]
    top = max(main, key=lambda c: penalty_rank(c["penalty"]), default=None)
    article["max_clause"] = top["number"] if top else None
    article["max_penalty"] = top["penalty"] if top else ""
    return article

OWN_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài, không suy đoán. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt, nêu khối lượng ma túy và kết quả tố tụng mới nhất",
  "date": "ngày xảy ra/xét xử dạng YYYY-MM-DD (suy ra năm từ NGÀY ĐĂNG BÀI), chuỗi rỗng nếu không rõ",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất cụ thể; dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp, chất không có trong danh sách (ví dụ etomidate) vẫn ghi nguyên tên",
                   "amount": "khối lượng kèm đơn vị đúng như bài viết, ví dụ: hơn 9,6kg; để \"\" nếu bài không nêu"}}],
  "people": [{{"name": "họ tên đầy đủ của MỘT cá nhân", "aliases": ["biệt danh"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "stage": "giai đoạn tố tụng mới nhất của người này: bắt giữ|khởi tố|truy tố|xét xử sơ thẩm|xét xử phúc thẩm, hoặc chuỗi rỗng",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án đã tuyên nếu có, ví dụ: tử hình, 36 tháng tù; chuỗi rỗng nếu chưa xét xử"}}]
}}]}}
Quy tắc:
- Một vụ việc ngoài đời = một phần tử trong "cases". Không tách một vụ thành nhiều phần tử.
- "people" chỉ gồm cá nhân có họ tên; bỏ qua nhóm người không nêu tên ("7 người", "nhóm nghi phạm").
- Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

NGÀY ĐĂNG BÀI: {published}
Tiêu đề: {title}
Nội dung:
{content}"""

def clean(value: Any) -> str:
    """LLM output -> text; the model sometimes writes the words 'chuỗi rỗng' instead of an empty string."""
    text = str(value or "").strip()
    return "" if text.lower() in {"chuỗi rỗng", "không rõ", "không có", "null", "none"} else text

def person_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("\"'“”‘’").lower())

def is_person_name(name: str) -> bool:
    """Drop what the LLM sometimes returns as a 'person': '7 công dân Trung Quốc', 'Chưa rõ danh tính'."""
    key = person_key(name)
    return bool(key) and not re.search(r"\d|chưa rõ|không rõ|nhóm |các ", key)

def extract_news_cases_own(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one article, then everything that is a graph key is re-linked in code."""
    prompt = OWN_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCE_ALIASES),
        published=doc.metadata.get("document_version", "")[:10],
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        for field in ("name", "summary", "date", "location"):
            case[field] = clean(case.get(field))
        people = []
        for person in case.get("people") or []:
            if not is_person_name(person.get("name") or ""):
                continue
            aliases = [a for a in person.get("aliases") or [] if a and person_key(a) != person_key(person["name"])]
            people.append({
                "name": person["name"].strip(), "key": person_key(person["name"]), "aliases": aliases,
                "role": clean(person.get("role")), "sentence": clean(person.get("sentence")),
                "stage": person.get("stage") if person.get("stage") in STAGES else "",
                "crime": link_crime(person.get("charge") or "", known_crimes),
            })
        substances: dict[str, str] = {}
        for item in case.get("substances") or []:
            name = link_substance(item.get("name") or "")
            if name and not substances.get(name):
                substances[name] = clean(item.get("amount"))
        case["people"] = people
        case["substances"] = [{"name": n, "amount": a, "grams": amount_to_grams(a)} for n, a in substances.items()]
        case["charges"] = sorted({c for c in (link_crime(x, known_crimes) for x in case.get("charges") or []) if c}
                                 | {p["crime"] for p in people if p["crime"]})
        case["doc_id"], case["published"] = doc.id, doc.metadata.get("document_version", "")
    return cases

def resolve_cases(cases: list[dict]) -> list[dict]:
    """Entity resolution across articles: one Person per real person, one Case per real case.

    - A person named by an alias in one article ('Phannhibeauty') is folded into the person who carries that alias.
    - Two extracted cases that share a named, non-official person are the same real case (union-find).
    - A Charge is keyed by (person, crime, case); the newest article wins for stage and sentence.
    """
    alias_owner = {person_key(a): p["key"] for c in cases for p in c["people"] for a in p["aliases"]}
    for case in cases:
        for person in case["people"]:
            person["key"] = alias_owner.get(person["key"], person["key"])

    parent = list(range(len(cases)))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    first_seen: dict[str, int] = {}
    for index, case in enumerate(cases):
        for person in case["people"]:
            if person["role"] == "cán bộ":
                continue
            if person["key"] in first_seen:
                parent[find(index)] = find(first_seen[person["key"]])
            else:
                first_seen[person["key"]] = index

    groups: dict[int, list[dict]] = {}
    for index, case in enumerate(cases):
        groups.setdefault(find(index), []).append(case)

    resolved = []
    for members in groups.values():
        members.sort(key=lambda c: (c["published"], c["doc_id"]))       # oldest article first
        first = members[0]
        merged = {
            "id": f"case-{first['doc_id'].removeprefix('news-')}-{sum(r['id'].startswith('case-' + first['doc_id'].removeprefix('news-')) for r in resolved) + 1}",
            "name": first.get("name") or first["doc_id"],
            "summary": members[-1].get("summary") or first.get("summary") or "",
            "date": next((c["date"] for c in members if c.get("date")), ""),
            "locations": sorted({c["location"] for c in members if c.get("location")}),
            "charges": sorted({crime for c in members for crime in c["charges"]}),
            "doc_ids": list(dict.fromkeys(c["doc_id"] for c in members)),
            "substances": [], "people": [], "accusations": [],
        }
        substances: dict[str, dict] = {}
        people: dict[str, dict] = {}
        accusations: dict[tuple[str, str], dict] = {}
        for case in members:
            for item in case["substances"]:
                kept = substances.setdefault(item["name"], dict(item))
                if item["grams"] and (kept["grams"] or 0) < item["grams"]:      # keep the largest reported weight
                    kept.update(amount=item["amount"], grams=item["grams"])
                elif not kept["amount"]:
                    kept["amount"] = item["amount"]
            for person in case["people"]:
                kept = people.setdefault(person["key"], {"key": person["key"], "name": person["name"],
                                                         "aliases": [], "role": person["role"]})
                if person_key(person["name"]) != person["key"]:                 # this mention used the alias
                    kept["aliases"].append(person["name"])
                kept["aliases"] = list(dict.fromkeys(kept["aliases"] + person["aliases"]))
                if person["role"] in ACCUSED_ROLES or not kept["role"]:
                    kept["role"] = person["role"]
                if person["crime"]:
                    charge = accusations.setdefault((person["key"], person["crime"]), {
                        "person": person["key"], "crime": person["crime"], "role": "", "stage": "", "sentence": ""})
                    for field in ("role", "stage", "sentence"):
                        charge[field] = person[field] or charge[field]          # newest non-empty value wins
                    charge["doc_id"] = case["doc_id"]
        for person in people.values():                                          # real name beats an alias-only mention
            names = [p["name"] for c in members for p in c["people"]
                     if p["key"] == person["key"] and person_key(p["name"]) == p["key"]]
            person["name"] = names[0] if names else person["name"]
        for charge in accusations.values():
            charge["id"] = f"{charge['person']}|{charge['crime']}|{merged['id']}"
            charge["name"] = f"{people[charge['person']]['name']}: {charge['crime']}"
        merged.update(substances=list(substances.values()), people=list(people.values()),
                      accusations=list(accusations.values()))
        resolved.append(merged)
    return resolved

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if use_hint_ontology():
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_own(question, doc_ids, max_facts)

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, seed_facts = self.seed_facts(question, doc_ids)

        # a. Cases that are a seed or next to one (a Person/Substance/Crime named in the question).
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        facts = [f"Vụ việc '{c['name']}': {c['summary']}" for c in cases]

        # b. Case -> Crime (bridge) -> Article -> Clause: base clause + clauses naming a substance of the case.
        # c. Articles named in the question: base clause + clauses naming a substance of the question.
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { MATCH (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $substances })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[c["id"] for c in cases], numbers=re.findall(r"[Đđ]iều (\d+)", question),
            substances=find_substances(question),
        )
        # d. One fact per clause.
        for cl in sorted(clauses, key=lambda c: (c["article"], c["number"])):
            facts.append(f"[{cl['article']} - {cl['title']}] khoản {cl['number']}: {cl['text']}")

        # Multi-hop facts first so the max_facts cut never drops the legal basis.
        return list(dict.fromkeys(facts + seed_facts))[:max_facts]

    # ---------------------------------------------------------------- OWN — my ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Substance", "name"),
                           ("Report", "doc_id"), ("Case", "id"), ("Person", "key"), ("Charge", "id"),
                           ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_substances(self, aliases: dict[str, list[str]]) -> None:
        self.run("UNWIND $rows AS row MERGE (s:Substance {name: row.name}) SET s.aliases = row.aliases",
                 rows=[{"name": name, "aliases": names} for name, names in aliases.items()])

    def add_law_article_own(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.title = $title, a.law = $law, a.doc_id = $doc_id,
                  a.max_clause = $max_clause, a.max_penalty = $max_penalty
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text,
                  cl.supplementary = clause.supplementary, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (t IN clause.thresholds |
                MERGE (s:Substance {name: t.substance})
                MERGE (cl)-[r:HAS_THRESHOLD {point: t.point}]->(s)
                  SET r.min_g = t.min_g, r.max_g = t.max_g, r.text = t.text)
            """,
            **article,
        )

    def add_report(self, doc: Document) -> None:
        self.run("MERGE (r:Report {doc_id: $doc_id}) SET r.name = $title, r.published = $published",
                 doc_id=doc.id, title=doc.metadata.get("title", ""), published=doc.metadata.get("document_version", ""))

    def add_case(self, case: dict) -> None:
        """One resolved case (see resolve_cases): the case, its reports, people, and per-person charges."""
        self.run(
            """
            MERGE (k:Case {id: $id}) SET k.name = $name, k.summary = $summary, k.date = $date
            FOREACH (d IN CASE WHEN size($doc_ids) = 1 THEN $doc_ids ELSE [] END | SET k.doc_id = d)
            WITH k
            CALL (k) { UNWIND $doc_ids AS doc_id MATCH (r:Report {doc_id: doc_id}) MERGE (r)-[:REPORTS]->(k) }
            FOREACH (loc IN $locations | MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.grams = s.grams)
            FOREACH (p IN $people | MERGE (person:Person {key: p.key})
                SET person.name = p.name, person.aliases = apoc_free_union(person.aliases, p.aliases)
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role)
            FOREACH (a IN $accusations |
                MERGE (person:Person {key: a.person}) MERGE (c:Crime {name: a.crime})
                MERGE (ch:Charge {id: a.id})
                  SET ch.name = a.name, ch.role = a.role, ch.stage = a.stage, ch.sentence = a.sentence, ch.doc_id = a.doc_id
                MERGE (person)-[:FACES]->(ch) MERGE (ch)-[:FOR_CRIME]->(c) MERGE (ch)-[:IN_CASE]->(k))
            """.replace("apoc_free_union(person.aliases, p.aliases)",
                        "reduce(acc = coalesce(person.aliases, []), x IN p.aliases | "
                        "CASE WHEN x IN acc THEN acc ELSE acc + x END)"),
            **{key: case[key] for key in ("id", "name", "summary", "date", "doc_ids", "locations", "charges",
                                          "substances", "people", "accusations")},
        )

    # ---------------------------------------------------------------- KG-3 (OWN)

    def _context_own(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        """Seeds -> cases -> per-person Charge -> Crime (bridge) -> Article -> penalty frames and weight thresholds.

        Sends compact, pre-computed facts (penalty ladder, highest frame, the clause a seized weight falls into)
        instead of full clause texts: fewer tokens, and the comparison is done by Cypher, not by the LLM.
        """
        # Clause and Charge edges are rendered below in readable form, so keep them out of the raw 1-hop facts.
        seed_ids, seed_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause", "Charge"), limit=40)
        people = [row["id"] for row in self.run(
            "MATCH (p:Person) WHERE elementId(p) IN $ids RETURN elementId(p) AS id", ids=seed_ids)]

        # a. Focus cases: those of the people named in the question; otherwise every case that is a seed or next
        #    to one (a Report hit by vector search, a Substance / Location / Crime named in the question).
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE CASE WHEN size($people) > 0
                       THEN EXISTS { MATCH (p:Person)-[:INVOLVED_IN]->(k) WHERE elementId(p) IN $people }
                       ELSE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids } END
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.date AS date,
                   [(k)-[:LOCATED_IN]->(l) | l.name] AS locations,
                   [(k)-[i:INVOLVES]->(s) | s.name + CASE WHEN i.amount <> '' THEN ' ' + i.amount ELSE '' END] AS substances,
                   [(p:Person)-[i:INVOLVED_IN]->(k) WHERE i.role IN $accused | p.name] AS accused,
                   COUNT { (:Report)-[:REPORTS]->(k) } AS reports
            ORDER BY k.id
            """,
            ids=seed_ids, people=people, accused=sorted(ACCUSED_ROLES),
        )
        case_ids = [c["id"] for c in cases]
        facts = []
        for c in cases:
            details = [f"ngày {c['date']}" if c["date"] else "", ", ".join(c["locations"]),
                       f"{c['reports']} bài báo đưa tin"]
            fact = f"Vụ việc '{c['name']}' ({'; '.join(d for d in details if d)})"
            if c["accused"]:
                fact += f", người bị buộc tội: {', '.join(c['accused'][:10])}"
            if c["substances"]:
                fact += f", chất ma túy: {', '.join(c['substances'])}"
            facts.append(f"{fact}. Tóm tắt: {c['summary']}")

        # b. Person -> Charge -> Crime <- Article: who is accused of what, at which stage, under which article.
        charges = self.run(
            """
            MATCH (p:Person)-[:FACES]->(ch:Charge)-[:IN_CASE]->(k:Case), (ch)-[:FOR_CRIME]->(c:Crime)
            WHERE elementId(k) IN $case_ids AND (size($people) = 0 OR elementId(p) IN $people)
            OPTIONAL MATCH (a:Article)-[:DEFINES]->(c)
            RETURN p.name AS person, p.aliases AS aliases, ch.role AS role, ch.stage AS stage,
                   ch.sentence AS sentence, c.name AS crime, a.id AS article, k.name AS case
            ORDER BY k.id, p.name
            """,
            case_ids=case_ids, people=people,
        )
        for ch in charges[:25]:
            who = ch["person"] + (f" (biệt danh: {', '.join(ch['aliases'])})" if ch["aliases"] else "")
            fact = f"{who} - {ch['role'] or 'người liên quan'}"
            fact += f", giai đoạn tố tụng: {ch['stage']}" if ch["stage"] else ""
            fact += f", tội {ch['crime']}" + (f" ({ch['article']})" if ch["article"] else "")
            fact += f", mức án: {ch['sentence']}" if ch["sentence"] else ", chưa có mức án"
            facts.append(fact + f" [vụ '{ch['case']}']")

        # c. Articles to explain: those of the charges above; cases without a named accused fall back to the
        #    case-level CHARGED_WITH edge; plus any article the question names ("Điều 251").
        articles = {ch["article"] for ch in charges if ch["article"]}
        if not articles:
            articles = {row["id"] for row in self.run(
                "MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article) WHERE elementId(k) IN $case_ids "
                "RETURN DISTINCT a.id AS id", case_ids=case_ids)}
        asked = {row["id"] for row in self.run(
            "MATCH (a:Article) WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ') RETURN a.id AS id",
            numbers=re.findall(r"[Đđ]iều (\d+)", question))}

        # d. Which clause does the seized weight fall into? Compared in Cypher on grams, not left to the LLM.
        for row in self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance)<-[t:HAS_THRESHOLD]-(cl:Clause)<-[:HAS_CLAUSE]-(a:Article)
            WHERE elementId(k) IN $case_ids AND a.id IN $articles
              AND i.grams >= t.min_g AND (t.max_g IS NULL OR i.grams < t.max_g)
            RETURN k.name AS case, s.name AS substance, i.amount AS amount, a.id AS article, cl.number AS number,
                   t.point AS point, t.text AS threshold, cl.penalty AS penalty
            ORDER BY a.id, cl.number DESC
            """,
            case_ids=case_ids, articles=sorted(articles),
        ):
            facts.append(f"Đối chiếu khối lượng: {row['amount']} {row['substance']} trong vụ '{row['case']}' thuộc "
                         f"điểm {row['point']} khoản {row['number']} {row['article']} ({row['substance']} "
                         f"{row['threshold']}), khung hình phạt: {row['penalty']}")

        # e. One line per article: every penalty frame + the highest one.
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.id IN $articles AND cl.penalty <> ''
            WITH a, cl ORDER BY cl.number
            RETURN a.id AS id, a.title AS title, a.max_clause AS max_clause, a.max_penalty AS max_penalty,
                   collect('khoản ' + cl.number + CASE WHEN cl.supplementary THEN ' (hình phạt bổ sung)' ELSE '' END
                           + ': ' + cl.penalty) AS frames
            ORDER BY id
            """,
            articles=sorted(articles | asked),
        ):
            facts.append(f"[{row['id']} - {row['title']}] khung hình phạt: {'; '.join(row['frames'])}. "
                         f"Khung cao nhất: khoản {row['max_clause']} ({row['max_penalty']})")

        # f. Question names an article and a substance: the weight thresholds of that substance in that article.
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[t:HAS_THRESHOLD]->(s:Substance)
            WHERE a.id IN $asked AND s.name IN $substances
            RETURN a.id AS article, cl.number AS number, t.point AS point, s.name AS substance, t.text AS threshold,
                   cl.penalty AS penalty
            ORDER BY article, number, substance
            """,
            asked=sorted(asked), substances=find_canonical_substances(question),
        ):
            facts.append(f"[{row['article']}] khoản {row['number']} điểm {row['point']}: {row['substance']} "
                         f"{row['threshold']} -> {row['penalty']}")

        # Multi-hop facts first so the max_facts cut never drops the legal basis.
        return list(dict.fromkeys(facts + seed_facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if use_hint_ontology():
        return _build_graph_hint(graph, law_docs, news_docs, llm_fn)
    return _build_graph_own(graph, law_docs, news_docs, llm_fn)

def _build_graph_hint(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                      llm_fn: Callable[..., str]) -> None:
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]                 # law KB: regex, no LLM
    for article in articles:
        graph.add_law_article(article)
    crimes = [article["crime"] for article in articles if article["crime"]]  # canonical names of the bridge
    for doc in news_docs:                                                    # news KB: one LLM call per article
        for case in extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes):
            graph.add_news_case(case, doc)

def _build_graph_own(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                     llm_fn: Callable[..., str]) -> None:
    graph.own_constraints()
    graph.add_substances(SUBSTANCE_ALIASES)                                  # canonical nodes before anything links
    articles = [parse_law_article_own(doc) for doc in law_docs]              # law KB: regex, no LLM
    for article in articles:
        graph.add_law_article_own(article)
    crimes = [article["crime"] for article in articles if article["crime"]]  # canonical names of the bridge
    extracted = []
    for doc in news_docs:                                                    # news KB: one LLM call per article
        graph.add_report(doc)
        extracted += extract_news_cases_own(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes)
    for case in resolve_cases(extracted):                                    # merge across articles, then write
        graph.add_case(case)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts),
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
