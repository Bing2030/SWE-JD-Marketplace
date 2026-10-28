#!/usr/bin/env python3
"""Build unified JD dataset (data/jobs.json) from three local sources.

Sources (paths in a JSON config, see sources.example.json):
  A linkedin    raw LinkedIn JSON (867)
  B structured  China multi-platform CSV (427, no jd_text -> assembled from sections)
  C official    China official-site CSV (1062, salary all empty, 78 internal dupes)

Pipeline: parse & map -> derive level/role/is_senior/ai_related -> dedup
(C-internal by jd_text hash, B-in-C by company+title key) -> banned-field
self-check -> hybrid JSON (meta envelope + one job per line).

Stdlib only. Deterministic. Prints a full build report.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, OrderedDict
from datetime import date

# ---------------------------------------------------------------- constants

TODAY = date.today().isoformat()

SENIOR_LEVELS = {"senior", "lead", "staff", "principal", "architect",
                 "manager", "director", "cto"}
JUNIOR_LEVELS = {"campus", "entry", "mid"}

# B/C level_guess & role_family -> unified enums
LEVEL_MAP_B = {"中级/其他": "mid", "高级/资深": "senior", "架构师": "architect",
               "专家": "staff", "负责人": "lead", "经理": "manager",
               "总监": "director", "CTO/首席": "cto"}
ROLE_MAP = {"AI/Agent": "ai_agent", "AI/算法/平台": "ai_platform",
            "架构/专家": "architecture", "Java/后端": "backend",
            "管理/负责人": "management", "支付/风控": "payment_risk",
            "其他": "other"}

LEVEL_MAP_A = {"senior": "senior", "staff": "staff", "principal": "principal",
               "lead": "lead", "architect": "architect", "manager": "manager",
               "mid": "mid", "entry": "entry", "校招": "campus",
               "unknown": "unknown"}

# C: title keyword -> level (longer first via ordered list)
TITLE_LEVEL_RULES = [
    (r"校招|应届|实习", "campus"),
    (r"总监", "director"),
    (r"CTO|首席", "cto"),
    (r"架构师", "architect"),
    (r"专家", "staff"),
    (r"资深|高级|[Ss]enior|[Ss]taff|[Pp]rincipal", "senior"),
    (r"经理|[Mm]anager|[Ll]ead|负责人", "manager"),
]

AI_KEYWORDS = re.compile(
    r"agent|智能体|agentic|\bllm\b|大模型|大语言模型|\brag\b|\bmcp\b|"
    r"langchain|langgraph|gen ?ai|生成式|\baigc\b|copilot|多模态|multimodal",
    re.I)

CITY_PREFIX = ("中国-",)
CITY_SUFFIX = ("市",)
COUNTRY_NAMES = {"China", "中国", "United States", "USA", "US",
                 "Singapore, Singapore"}
KNOWN_CITIES = {"北京": "北京", "上海": "上海", "深圳": "深圳", "杭州": "杭州",
                "成都": "成都", "广州": "广州", "南京": "南京", "武汉": "武汉",
                "西安": "西安", "长沙": "长沙", "重庆": "重庆", "苏州": "苏州",
                "厦门": "厦门", "合肥": "合肥", "珠海": "珠海", "佛山": "佛山",
                "东莞": "东莞", "新加坡": "新加坡", "Singapore": "新加坡",
                "Shanghai": "上海", "Beijing": "北京", "Shenzhen": "深圳",
                "Hangzhou": "杭州", "Chengdu": "成都", "Guangzhou": "广州",
                "Nanjing": "南京", "Wuhan": "武汉", "Xi'an": "西安", "Suzhou": "苏州",
                "New York": "New York", "San Francisco": "San Francisco",
                "Sunnyvale": "Sunnyvale", "Seattle": "Seattle",
                "Santa Clara": "Santa Clara", "San Jose": "San Jose",
                "Mountain View": "Mountain View", "Austin": "Austin",
                "Boston": "Boston", "Los Angeles": "Los Angeles",
                "Palo Alto": "Palo Alto", "Chicago": "Chicago"}

BANNED_FIELDS = ["is_target", "keep", "filter_reason", "senior_fit",
                 "is_relevant", "jd_quality", "verification", "status",
                 "_file", "source_file", "jd_len", "city_clean", "city_norm",
                 "jd_hash", "category_url", "query", "_source_file"]

# 偏离岗位剔除（2026-10-28 用户审阅线上列表后指定；标题小写子串匹配）
TITLE_EXCLUDE_KEYWORDS = [
    "外包", "嵌入式", "项目经理", "camera", "cmu", "解决方案", "客户端",
    "consultant", "产品经理", "自动化设备", "digital architect",
]


# ---------------------------------------------------------------- helpers

def norm_key(s):
    """Company+title dedup key: lowercase, strip all whitespace & punctuation."""
    return re.sub(r"[\s\W]+", "", (s or "").lower(), flags=re.UNICODE)


def normalize_cities(raw):
    """'北京市、上海市' / '中国-深圳' / 'New York, NY' / '中国 广东省 宝安区'
    -> ['北京','上海'] ... CJK segments split on spaces; ASCII stays whole."""
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    # LinkedIn-style 'City, Country': keep the comma-separated head
    if "," in raw:
        raw = raw.split(",")[0].strip()
    out = []
    for seg in re.split(r"[、,，/;；]", raw):
        if not seg.strip():
            continue
        parts = (re.split(r"\s+", seg.strip())
                 if re.search(r"[一-鿿]", seg) else [seg.strip()])
        for p in parts:
            p = p.strip()
            for pre in list(CITY_PREFIX) + ["中国"]:
                if p.startswith(pre):
                    p = p[len(pre):].strip()
            for suf in ("市", "省"):
                if p.endswith(suf) and len(p) > 2:
                    p = p[:-1]
            p = KNOWN_CITIES.get(p, p)
            if not p or p in COUNTRY_NAMES or p.startswith("United States"):
                continue
            # substring scan for known city inside granular text (e.g. '广东省 宝安区')
            if p not in KNOWN_CITIES.values():
                hit = next((v for k, v in KNOWN_CITIES.items() if k in p), None)
                if hit:
                    p = hit
            if p not in out:
                out.append(p)
    return out


def region_from_cities(cities):
    if any("新加坡" in c or "Singapore" in c for c in cities):
        return "sg"
    return "cn"


def parse_experience(raw):
    """'5-10年' -> (5,10); '10年以上' -> (10,None); 不限/空 -> (None,None)."""
    if not raw or not raw.strip():
        return None, None
    t = raw.strip()
    if "不限" in t or "在校" in t or "应届" in t:
        return None, None
    m = re.search(r"(\d+)\s*[-–~]\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*年?(?:以上|\+)", t)
    if m:
        return int(m.group(1)), None
    return None, None


def parse_salary_text(text):
    """LinkedIn salary text -> {currency, period, min, max} or minimal dict."""
    if not text or not text.strip():
        return None
    out = {"currency": "CNY" if ("¥" in text or "CNY" in text) else
           ("USD" if ("$" in text or "USD" in text) else None),
           "period": None, "min": None, "max": None}
    t = text.replace(",", "")
    if re.search(r"/\s*mo(?:nth)?", t, re.I) or "每月" in t:
        out["period"] = "monthly"
    elif re.search(r"/\s*y(?:ea)?r", t, re.I) or "每年" in t or "gross" in t.lower():
        out["period"] = "annual"
    nums = []
    for m in re.finditer(r"(¥|\$)?\s*(\d+(?:\.\d+)?)\s*([KMkm])?", t):
        val = float(m.group(2))
        unit = (m.group(3) or "").upper()
        if unit == "K":
            val *= 1000
        elif unit == "M":
            val *= 1_000_000
        nums.append(int(val))
    nums = [n for n in nums if n >= 1000]  # skip薪数 like "18薪" noise
    if len(nums) >= 2:
        out["min"], out["max"] = nums[0], nums[-1]
    elif len(nums) == 1:
        out["min"] = out["max"] = nums[0]
    if out["period"] is None and out["min"] and out["min"] > 100_000:
        out["period"] = "annual"
    return out


def make_salary_b(r):
    """B: salary_min/max_cny are annual totals; months from salary_raw."""
    lo, hi, raw = r.get("salary_min_cny"), r.get("salary_max_cny"), r.get("salary_raw")
    if not (lo and hi):
        return {"text": raw} if raw and raw.strip() else None
    m = re.search(r"(\d+)\s*薪", raw or "")
    months = int(m.group(1)) if m else 12
    return {"text": (raw or "").strip(), "currency": "CNY", "period": "annual",
            "min": int(float(lo)), "max": int(float(hi)),
            "months": months, "months_est": not m,
            "annual_min": int(float(lo)), "annual_max": int(float(hi))}


def derive_level_from_title(title):
    for pat, level in TITLE_LEVEL_RULES:
        if re.search(pat, title):
            return level
    return "unknown"


def is_senior_of(level):
    if level in SENIOR_LEVELS:
        return True
    if level in JUNIOR_LEVELS:
        return False
    return None  # unknown


def exclude_by_title(jobs):
    """Drop jobs whose title contains an excluded keyword (lowercase substring).
    Returns (kept_jobs, Counter by first matching keyword)."""
    kept, counts = Counter(), Counter()
    kept = []
    for j in jobs:
        t = j["title"].lower()
        hit = next((k for k in TITLE_EXCLUDE_KEYWORDS if k in t), None)
        if hit:
            counts[hit] += 1
        else:
            kept.append(j)
    return kept, counts


def ai_related(job):
    blob = " ".join([job.get("title") or "", " ".join(job.get("skills") or []),
                     (job.get("description") or "")])
    return bool(AI_KEYWORDS.search(blob))


def src(platform, url, collected_at):
    return {"platform": platform, "url": url, "collected_at": collected_at}


def clean_date(raw):
    if not raw:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(raw))
    return m.group(0) if m else None


# ---------------------------------------------------------------- parsers

def parse_linkedin(rec):
    level = LEVEL_MAP_A.get(rec.get("seniority"), "unknown")
    cities = normalize_cities(rec.get("location"))
    region = (rec.get("region") or "").lower() or region_from_cities(cities)
    query_title = (rec.get("query") or "") + " " + (rec.get("job_title") or "")
    if re.search(r"agent|智能体", query_title, re.I):
        role = "ai_agent"
    elif re.search(r"架构|architect", query_title, re.I):
        role = "architecture"
    elif re.search(r"经理|管理|manager|director", query_title, re.I):
        role = "management"
    elif re.search(r"java|后端|backend", query_title, re.I):
        role = "backend"
    else:
        role = "other"
    wt = rec.get("workplace_type") or ""
    workplace = ("onsite" if "现场" in wt or "nsite" in wt.lower()
                 else "remote" if "远程" in wt or "emote" in wt.lower()
                 else "hybrid" if "混合" in wt or "ybrid" in wt.lower()
                 else "unknown")
    sal = parse_salary_text(rec.get("salary"))
    m = re.search(r"(\d+)", str(rec.get("applicants") or ""))
    return {
        "id": "linkedin:" + str(rec.get("job_id")),
        "title": rec.get("job_title") or "",
        "company": rec.get("company") or "",
        "level": level, "level_inferred": False,
        "role": role, "role_inferred": True,
        "region": region, "cities": cities,
        "location_raw": rec.get("location"),
        "workplace": workplace,
        "salary": sal, "experience": None, "years_min": None, "years_max": None,
        "education": None, "skills": [],
        "description": rec.get("description") or "",
        "notes": rec.get("notes") or "",
        "alt_titles": [],
        "sources": [src("linkedin", rec.get("link"), clean_date(rec.get("fetched_at")))],
        "posted_ago": rec.get("posted_ago"),
        "applicants": int(m.group(1)) if m else None,
        "collected_at": clean_date(rec.get("fetched_at")),
    }


def parse_structured(r):
    level = LEVEL_MAP_B.get((r.get("level_guess") or "").strip(), "unknown")
    role = ROLE_MAP.get((r.get("direction") or "").strip(), "other")
    y_min, y_max = parse_experience(r.get("experience"))
    desc = "\n".join(filter(None, [
        "【职责】" + (r.get("responsibilities") or "").strip() if (r.get("responsibilities") or "").strip() else "",
        "【要求】" + ((r.get("requirements_full") or "").strip()
                     or (r.get("hard_requirements") or "").strip()),
        "【加分】" + (r.get("nice_to_have") or "").strip() if (r.get("nice_to_have") or "").strip() else "",
    ]))
    skills = [s.strip() for s in (r.get("must_have_skills") or "").split("|") if s.strip()]
    if skills and "【技能】" not in desc:
        desc += "\n【技能】" + "、".join(skills)
    cities = normalize_cities(r.get("city"))
    return {
        "id": (r.get("source_platform") or "mp") + ":" + str(r.get("job_id")),
        "title": r.get("title") or "",
        "company": r.get("company") or "",
        "level": level, "level_inferred": False,
        "role": role, "role_inferred": False,
        "region": region_from_cities(cities), "cities": cities,
        "location_raw": r.get("city"),
        "workplace": "unknown",
        "salary": make_salary_b(r),
        "experience": (r.get("experience") or "").strip() or None,
        "years_min": y_min, "years_max": y_max,
        "education": (r.get("education") or "").strip() or None,
        "skills": skills, "description": desc, "notes": "",
        "alt_titles": [],
        "sources": [src(r.get("source_platform"), r.get("source_url"),
                        clean_date(r.get("collected_at")))],
        "posted_ago": None, "applicants": None,
        "collected_at": clean_date(r.get("collected_at")),
    }


def parse_official(r):
    title = r.get("title") or ""
    level = derive_level_from_title(title)
    role = ROLE_MAP.get((r.get("role_family") or "").strip(), "other")
    y_min, y_max = parse_experience(r.get("experience"))
    cities = normalize_cities(r.get("city"))
    return {
        "id": (r.get("source_platform") or "official") + ":" + str(r.get("job_id")),
        "title": title,
        "company": r.get("company") or "",
        "level": level, "level_inferred": True,
        "role": role, "role_inferred": False,
        "region": region_from_cities(cities), "cities": cities,
        "location_raw": r.get("city"),
        "workplace": "unknown",
        "salary": None,
        "experience": (r.get("experience") or "").strip() or None,
        "years_min": y_min, "years_max": y_max,
        "education": None, "skills": [],
        "description": (r.get("jd_text") or "").strip(),
        # C-source notes are scraper metadata ("Tencent Query API", "更新于…部门…"),
        # not role analysis — drop them; only LinkedIn notes (role analysis) ship.
        "notes": "",
        "alt_titles": [],
        "sources": [src(r.get("source_platform"), r.get("source_url"),
                        clean_date(r.get("collected_at")))],
        "posted_ago": None, "applicants": None,
        "collected_at": clean_date(r.get("collected_at")),
    }


# ---------------------------------------------------------------- pipeline

def load_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def dedup_official_internal(records):
    """Merge same-jd_text records (multi-city postings). Returns kept, removed."""
    groups = OrderedDict()
    for rec in records:
        h = hashlib.md5(rec["description"].encode("utf-8")).hexdigest()
        groups.setdefault(h, []).append(rec)
    kept, removed = [], 0
    for h, recs in groups.items():
        main = recs[0]
        for other in recs[1:]:
            removed += 1
            for c in normalize_cities(other.get("location_raw")):
                if c not in main["cities"]:
                    main["cities"].append(c)
            if other["title"] != main["title"] and other["title"] not in main["alt_titles"]:
                main["alt_titles"].append(other["title"])
            if other["sources"][0]["url"] not in {s["url"] for s in main["sources"]}:
                main["sources"].append(other["sources"][0])
        kept.append(main)
    return kept, removed


def absorb_structured_into_official(official, structured):
    """B∩C by company+title key: keep official, append B's url. Returns (kept_b, absorbed_n)."""
    by_key = {}
    for rec in official:
        by_key.setdefault(norm_key(rec["company"]) + "|" + norm_key(rec["title"]), rec)
    kept, absorbed = [], 0
    for rec in structured:
        k = norm_key(rec["company"]) + "|" + norm_key(rec["title"])
        if k in by_key:
            target = by_key[k]
            url = rec["sources"][0]["url"]
            if url and url not in {s["url"] for s in target["sources"]}:
                target["sources"].append(rec["sources"][0])
            absorbed += 1
        else:
            kept.append(rec)
    return kept, absorbed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("-o", "--output", default="data/jobs.json")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))

    li_raw = json.load(open(cfg["linkedin"], encoding="utf-8"))
    st_raw = load_csv(cfg["structured"])
    of_raw = load_csv(cfg["official"])

    li = [parse_linkedin(r) for r in li_raw]
    st = [parse_structured(r) for r in st_raw]
    of = [parse_official(r) for r in of_raw]

    of, c_removed = dedup_official_internal(of)
    st, b_absorbed = absorb_structured_into_official(of, st)

    jobs = li + of + st
    for j in jobs:
        j["is_senior"] = is_senior_of(j["level"])
        j["ai_related"] = ai_related(j)
        j.pop("posted_ago", None) if j.get("posted_ago") is None else None

    jobs, excluded = exclude_by_title(jobs)

    # banned-field self-check on serialized output
    blob = "\n".join(json.dumps(j, ensure_ascii=False) for j in jobs)
    leaked = [f for f in BANNED_FIELDS if f'"{f}"' in blob]
    if leaked:
        sys.exit(f"FATAL: banned fields leaked: {leaked}")

    meta = {
        "version": "1.0",
        "generated_at": TODAY,
        "counts": {
            "total": len(jobs),
            "by_source_read": {"linkedin": len(li_raw), "structured": len(st_raw),
                               "official": len(of_raw)},
            "dedup": {"official_internal_merged": c_removed,
                      "structured_absorbed": b_absorbed},
            "title_excluded": {"total": sum(excluded.values()),
                               "by_keyword": dict(excluded)},
            "is_senior": dict(Counter(str(j["is_senior"]) for j in jobs)),
            "region": dict(Counter(j["region"] for j in jobs)),
            "ai_related": sum(1 for j in jobs if j["ai_related"]),
            "level": dict(Counter(j["level"] for j in jobs)),
        },
    }

    with open(args.output, "w", encoding="utf-8") as f:
        f.write('{"meta":')
        f.write(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
        f.write(',"jobs": [\n')
        f.write(",\n".join(json.dumps(j, ensure_ascii=False,
                                      separators=(",", ":")) for j in jobs))
        f.write("\n]}\n")

    # report
    print(f"read: linkedin={len(li_raw)} structured={len(st_raw)} official={len(of_raw)}")
    print(f"dedup: official internal removed={c_removed}, structured absorbed={b_absorbed}")
    print(f"title-excluded: total={sum(excluded.values())} {dict(excluded)}")
    print(f"final jobs = {len(jobs)}")
    for k, v in meta["counts"].items():
        if k not in ("total", "by_source_read", "dedup"):
            print(f"  {k}: {v}")
    print(f"banned-field check: OK")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
