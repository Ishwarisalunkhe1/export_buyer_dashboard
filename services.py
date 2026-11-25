"""Domain services: validation, classification, search workflows and demo data."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from config import EMAIL_VALIDATION_MODE, GEMINI_API_KEY, GEMINI_MODEL, GEMINI_TIMEOUT_SECONDS

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

LEGAL_SUFFIXES = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|limited|llp|plc)\b",
    re.IGNORECASE,
)

BUSINESS_TERMS = (
    "store", "showroom", "shop", "gallery", "importer", "import", "wholesale",
    "wholesaler", "retailer", "retail", "distributor", "distribution", "wellness",
    "studio", "home decor", "home décor", "furnish", "trading", "supply", "supplies",
    "marketplace", "boutique", "interior", "design", "llc", "inc", "corp", "ltd",
    "purchasing", "procurement", "sales", "orders", "support", "contact", "info",
)

INDIVIDUAL_TERMS = (
    "practitioner", "individual", "personal", "freelancer", "collector", "hobbyist",
)

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "proton.me", "protonmail.com", "live.com", "msn.com", "example.com",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_email(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"^mailto:", "", text)
    match = re.search(r"<([^>]+)>", text)
    if match:
        text = match.group(1).strip()
    text = text.strip(" \"'")
    return text


def normalize_website(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.sub(r"^(www\.)+", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1]
    return text.strip(" .")


def normalize_company(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    text = LEGAL_SUFFIXES.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def validate_email_address(value: str | None) -> dict[str, Any]:
    """Validate email syntax and, when configured, MX records.

    The default mode is a syntax check; the UI states this clearly so the demo is
    never presented as a guaranteed deliverability check.
    """
    email = normalize_email(value)
    if not email:
        return {"valid": False, "email": "", "reason": "Email is empty"}

    if not EMAIL_RE.match(email) or ".." in email:
        return {"valid": False, "email": email, "reason": "Invalid email format"}

    local, domain = email.rsplit("@", 1)
    if local.startswith(".") or local.endswith("."):
        return {"valid": False, "email": email, "reason": "Invalid local part"}

    result: dict[str, Any] = {
        "valid": True,
        "email": email,
        "reason": "Format valid (syntax check)",
    }

    if EMAIL_VALIDATION_MODE == "mx":
        try:
            import dns.resolver  # type: ignore

            answers = dns.resolver.resolve(domain, "MX")
            if not answers:
                result.update(valid=False, reason="No MX records found")
            else:
                result["reason"] = "Format valid; MX record found"
        except ImportError:
            result["reason"] = "Format valid (dnspython not installed; MX skipped)"
        except Exception as exc:  # DNS failures should not crash the workflow
            result.update(valid=False, reason=f"MX lookup failed: {type(exc).__name__}")

    return result


def _short_error(exc: Exception) -> str:
    """Return a concise error label without echoing request headers or keys."""
    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return f"{name}: timed out"
    detail = " ".join(str(exc).split())
    return f"{name}: {detail[:90]}"


def _parse_gemini_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    return round(max(0.0, min(1.0, confidence)), 2)


def _parse_gemini_payload(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    label = " ".join(str(parsed.get("buyer_type") or "").strip().lower().replace("_", " ").split())
    label_map = {
        "business": "business",
        "business buyer": "business",
        "b2b": "business",
        "company": "business",
        "individual": "individual",
        "individual buyer": "individual",
        "person": "individual",
        "b2c": "individual",
        "uncertain": "uncertain",
        "unknown": "uncertain",
        "unclear": "uncertain",
        "unsure": "uncertain",
    }
    buyer_type = label_map.get(label)
    if not buyer_type:
        return None

    reason = " ".join(str(parsed.get("reason") or "").split())[:180] or "Gemini classification"
    return {
        "buyer_type": buyer_type,
        "reason": reason,
        "confidence": _parse_gemini_confidence(parsed.get("confidence")),
    }


def _gemini_classify(lead: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Call Gemini. Returns (result, error); the key is never logged or returned."""
    if not GEMINI_API_KEY:
        return None, "Gemini key not configured"

    prompt = (
        "You are classifying export sales leads for an India-based exporter. "
        "Choose exactly one label: 'business' for a company, importer, wholesaler, retailer, "
        "showroom, distributor, wellness store or purchasing staff; 'individual' for a private "
        "person with no clear business buyer signals; 'uncertain' when the evidence is insufficient. "
        "Return ONLY compact JSON with keys buyer_type, reason and confidence. "
        "reason must be one short sentence of at most 20 words. confidence must be a number from 0 to 1. "
        "Lead data: " + json.dumps(lead, ensure_ascii=False)
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            # Leave headroom for reasoning tokens on Gemini 3.x models.
            "maxOutputTokens": 512,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        return None, _short_error(exc)

    parsed = _parse_gemini_payload(str(text))
    if parsed is None:
        return None, "Gemini invalid response"
    return parsed, ""


def _rule_classify(lead: dict[str, Any]) -> dict[str, Any]:
    haystack = " ".join(
        str(lead.get(field) or "") for field in
        ("contact_name", "company_name", "role", "email", "website", "source")
    ).lower()
    email = normalize_email(lead.get("email"))
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    company = normalize_company(lead.get("company_name"))

    score = 0
    hits: list[str] = []
    for term in BUSINESS_TERMS:
        if term in haystack:
            score += 2
            hits.append(term)
    for term in INDIVIDUAL_TERMS:
        if term in haystack:
            score -= 2
            hits.append(term)
    if domain in PERSONAL_EMAIL_DOMAINS:
        score -= 1
        hits.append("personal email domain")
    if company:
        score += 2
        hits.append("company name present")

    if score >= 2:
        buyer_type = "business"
    elif score <= -2:
        buyer_type = "individual"
    else:
        buyer_type = "uncertain"

    evidence = ", ".join(dict.fromkeys(hits[:4])) or "insufficient evidence"
    confidence = 0.45 if buyer_type == "uncertain" else round(min(0.92, 0.55 + abs(score) * 0.07), 2)
    return {
        "buyer_type": buyer_type,
        "reason": f"Demo rules: {evidence}",
        "confidence": confidence,
    }


def classify_buyer(lead: dict[str, Any], force_demo: bool = False) -> dict[str, Any]:
    """Classify with Gemini when configured; otherwise use clearly labelled demo rules."""
    if not force_demo:
        gemini_result, error = _gemini_classify(lead)
        if gemini_result:
            return {
                **gemini_result,
                "method": f"Gemini AI ({GEMINI_MODEL})",
                "classification_error": "",
            }
    else:
        error = "forced demo"

    result = _rule_classify(lead)
    if force_demo:
        method = "Demo rules (forced demo)"
    elif not GEMINI_API_KEY:
        method = "Demo rules (Gemini key not configured)"
    elif error == "Gemini invalid response":
        method = "Demo rules (Gemini invalid response)"
    else:
        method = f"Demo rules (Gemini unavailable: {error})"
    return {**result, "method": method, "classification_error": error}


def _encode(text: str) -> str:
    return urllib.parse.quote_plus(text)


SOURCE_DEFINITIONS = [
    {
        "key": "google_search",
        "name": "Google Search",
        "category": "Search engine",
        "description": "Find showrooms, small shops, importers and contact pages. Narrow from country to state to city.",
        "url": "https://www.google.com/search?q={query}",
    },
    {
        "key": "google_maps",
        "name": "Google Maps",
        "category": "Local businesses",
        "description": "Open map results for phone numbers, websites and showroom names, then visit contact pages for email IDs.",
        "url": "https://www.google.com/maps/search/{query}",
    },
    {
        "key": "linkedin_people",
        "name": "LinkedIn People",
        "category": "Professional network",
        "description": "Search buyer, assistant buyer, purchaser, import manager, CEO and owner titles. Connect with a professional profile.",
        "url": "https://www.linkedin.com/search/results/people/?keywords={query}",
    },
    {
        "key": "linkedin_companies",
        "name": "LinkedIn Companies",
        "category": "Professional network",
        "description": "Find companies selling related products in the target country and identify their staff.",
        "url": "https://www.linkedin.com/search/results/companies/?keywords={query}",
    },
    {
        "key": "facebook",
        "name": "Facebook",
        "category": "Social network",
        "description": "Find local shops, groups and pages selling related home decor products.",
        "url": "https://www.facebook.com/search/top?q={query}",
    },
    {
        "key": "company_websites",
        "name": "Company websites / Contact us",
        "category": "Direct website",
        "description": "Use site: searches to locate contact pages, generic email IDs and phone numbers.",
        "url": "https://www.google.com/search?q={query}",
    },
    {
        "key": "importyeti",
        "name": "ImportYeti",
        "category": "Import records",
        "description": "Research US importers buying home decor products from international suppliers and review shipment data.",
        "url": "https://www.importyeti.com/search?q={query}",
    },
    {
        "key": "trade_shows",
        "name": "Trade show / exhibitor directories",
        "category": "Exhibitor lists",
        "description": "Research exhibitor directories and trade show lists for buyers and distributors.",
        "url": "https://www.google.com/search?q={query}",
    },
    {
        "key": "thomas",
        "name": "Thomas",
        "category": "Business directory",
        "description": "Search wholesalers, distributors and home furnishing businesses.",
        "url": "https://www.thomasnet.com/search.html?what={query}",
    },
    {
        "key": "alibaba",
        "name": "Alibaba",
        "category": "Marketplace",
        "description": "Study sellers and buyers in the same product category.",
        "url": "https://www.alibaba.com/trade/search?SearchText={query}",
    },
    {
        "key": "indiamart",
        "name": "IndiaMART",
        "category": "Marketplace",
        "description": "Study related listings, product terms and potential trade contacts.",
        "url": "https://dir.indiamart.com/search.mp?ss={query}",
    },
    {
        "key": "yelp",
        "name": "Yelp",
        "category": "US business directory",
        "description": "Find local showrooms and shops with contact details in a specific city.",
        "url": "https://www.yelp.com/search?find_desc={query}&find_loc={location}",
    },
    {
        "key": "yellow_pages",
        "name": "US Yellow Pages",
        "category": "US business directory",
        "description": "Look up local business names, phone numbers and websites.",
        "url": "https://www.yellowpages.com/search?search_terms={query}&geo_location_terms={location}",
    },
    {
        "key": "chamber_of_commerce",
        "name": "Chamber of Commerce directories",
        "category": "Local business directory",
        "description": "Use city-level chamber directories to find member businesses.",
        "url": "https://www.google.com/search?q={query}",
    },
    {
        "key": "cold_calling",
        "name": "Phone / cold calling list",
        "category": "Manual follow-up",
        "description": "Store every phone number found on Maps and websites; call and request an email address when it is not public.",
        "url": "https://www.google.com/search?q={query}",
    },
]


def _location_parts(country: str, state: str, city: str) -> str:
    return ", ".join(part for part in (city, state, country) if part)


def build_query(product: str, country: str, state: str, city: str, intent: str = "") -> str:
    parts = [part for part in (product, intent, city, state, country) if part]
    return " ".join(parts)


def build_search_links(product: str, country: str, state: str, city: str) -> list[dict[str, str]]:
    """Create practical manual-search links for every source named in the video."""
    location = _location_parts(country, state, city)
    intent_map = {
        "google_search": "buyers importers wholesalers retailers showrooms",
        "google_maps": "showroom store",
        "linkedin_people": 'buyer OR purchaser OR "import manager" OR owner',
        "linkedin_companies": "importer distributor retailer showroom",
        "facebook": "shop showroom",
        "company_websites": f'site:contact "{product}"',
        "importyeti": "home decor imports",
        "trade_shows": "trade show exhibitors buyers",
        "thomas": "home decor wholesaler distributor",
        "alibaba": "buyers importers",
        "indiamart": "singing bowls exporters buyers",
        "yelp": f"{product} home decor store",
        "yellow_pages": f"{product} home decor store",
        "chamber_of_commerce": f'"{product}" showroom chamber of commerce',
        "cold_calling": f'"{product}" showroom phone number',
    }
    links: list[dict[str, str]] = []
    for source in SOURCE_DEFINITIONS:
        intent = intent_map.get(source["key"], "buyers importers")
        query = build_query(product, country, state, city, intent)
        url = source["url"].format(query=_encode(query), location=_encode(location))
        links.append({**source, "query": query, "url": url})
    return links


def build_search_ladder(product: str, country: str, state: str, city: str) -> list[dict[str, str]]:
    """Demonstrate the video's country -> state -> city narrowing method."""
    levels = [
        ("Country level", country, "", ""),
        ("State level", country, state, ""),
        ("City level", country, state, city),
    ]
    ladder: list[dict[str, str]] = []
    for label, level_country, level_state, level_city in levels:
        if not level_country:
            continue
        query = build_query(product, level_country, level_state, level_city, "buyers showrooms")
        ladder.append(
            {
                "label": label,
                "scope": _location_parts(level_country, level_state, level_city),
                "google_url": "https://www.google.com/search?q=" + _encode(query),
                "maps_url": "https://www.google.com/maps/search/" + _encode(query),
                "query": query,
            }
        )
    return ladder


def build_manual_playbooks(product: str, country: str, state: str, city: str) -> list[dict[str, Any]]:
    links = {item["key"]: item for item in build_search_links(product, country, state, city)}
    return [
        {
            "title": "LinkedIn: find the actual buyer",
            "source": "LinkedIn",
            "steps": [
                "Create a complete, presentable profile that explains the products you supply.",
                'Search People for "buyer", "assistant buyer", "purchaser", "import manager", "CEO" and "owner" with the product and location.',
                "Review profiles such as buyer, assistant buyer and home decor buyer; connect with 2-3 relevant people daily.",
                "Where permitted, try contact-data browser extensions such as Lusha or Apollo (including free monthly limits) to fetch public contact details, then store verified details in the leads table.",
            ],
            "links": [links["linkedin_people"], links["linkedin_companies"]],
        },
        {
            "title": "Google Search + Maps: narrow the geography",
            "source": "Google",
            "steps": [
                f'Start with "{product} buyers in {country}".',
                f'Narrow to a state such as "{state}", then to a city such as "{city}".',
                "Open map results for showrooms and shops; collect the business name and phone number.",
                "Visit the website's Contact Us page and copy the email ID, phone number and website into the database.",
                "Use phone numbers for cold calling when no email is published; ask for the right buyer's email.",
            ],
            "links": [links["google_search"], links["google_maps"], links["company_websites"]],
        },
        {
            "title": "Import and trade directories",
            "source": "ImportYeti / directories",
            "steps": [
                "Research importers buying related products from international suppliers on ImportYeti.",
                "Review recent shipments and company names, then look for their websites and contacts.",
                "Check trade show and exhibitor directories, Thomas, Alibaba, IndiaMART, Yelp, Yellow Pages and Chamber of Commerce directories.",
                "Study each platform before paying for a plan; add useful prospects to the same lead database.",
            ],
            "links": [
                links["importyeti"], links["trade_shows"], links["thomas"],
                links["alibaba"], links["indiamart"], links["yelp"],
                links["yellow_pages"], links["chamber_of_commerce"],
            ],
        },
        {
            "title": "Facebook and local communities",
            "source": "Facebook",
            "steps": [
                "Search pages and groups for local shops and wellness stores selling related products.",
                "Open business pages to find websites, phone numbers and public email addresses.",
                "Record the source URL against each lead so the team can verify it later.",
            ],
            "links": [links["facebook"]],
        },
        {
            "title": "Cold email outreach",
            "source": "Email",
            "steps": [
                "Use only validated business buyer emails.",
                "Personalize the email, introduce the export business and attach the product presentation PDF.",
                "Keep a history of every email and avoid repeat contact to prevent spam.",
            ],
            "links": [links["cold_calling"]],
        },
    ]


# Fictional sample records. example.com/example.net domains are reserved for documentation.
DEMO_LEADS: list[dict[str, Any]] = [
    {
        "contact_name": "Aarav Mehta", "company_name": "Lumen Decor LLC",
        "email": "hello@lumendecor.example.com", "phone": "+1 713 555 0101",
        "website": "https://lumendecor.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "source": "Google Maps", "role": "Showroom owner",
    },
    {
        "contact_name": "Sarah Chen", "company_name": "Houston Wellness Gallery",
        "email": "sales@houstonwellness.example.com", "phone": "+1 713 555 0102",
        "website": "https://houstonwellness.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "source": "Google Search", "role": "Purchasing manager",
    },
    {
        "contact_name": "Michael Reed", "company_name": "Texas Home Imports",
        "email": "info@texashomeimports.example.com", "phone": "+1 214 555 0103",
        "website": "https://texashomeimports.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Dallas",
        "source": "ImportYeti", "role": "Import manager",
    },
    {
        "contact_name": "Priya Nair", "company_name": "Aroma Sound Studio",
        "email": "contact@aromasound.example.com", "phone": "+1 512 555 0104",
        "website": "https://aromasound.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Austin",
        "source": "LinkedIn", "role": "Owner",
    },
    {
        "contact_name": "Jonathan Lee", "company_name": "Harmony Home Showroom",
        "email": "jonathan[at]harmonyhome.example.com", "phone": "+1 713 555 0105",
        "website": "https://harmonyhome.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "source": "Yelp", "role": "Store manager",
    },
    {
        "contact_name": "Emily Carter", "company_name": "",
        "email": "emily.carter@example.com", "phone": "",
        "website": "", "product": "singing bowls",
        "country": "United States", "state": "California", "city": "Los Angeles",
        "source": "Facebook", "role": "Sound healing practitioner",
    },
    {
        "contact_name": "Daniel Ortiz", "company_name": "Golden Light Trading Inc",
        "email": "orders@goldenlight.example.com", "phone": "+1 212 555 0107",
        "website": "https://goldenlight.example.com", "product": "singing bowls",
        "country": "United States", "state": "New York", "city": "New York",
        "source": "Thomas", "role": "Wholesaler",
    },
    {
        "contact_name": "Nina Patel", "company_name": "Serene Spaces Retail",
        "email": "nina@serenespaces.example.net", "phone": "+1 305 555 0108",
        "website": "https://serenespaces.example.net", "product": "singing bowls",
        "country": "United States", "state": "Florida", "city": "Miami",
        "source": "Trade show exhibitor directory", "role": "Retail buyer",
    },
    {
        "contact_name": "Oliver Brooks", "company_name": "Brooks Home Furnishing",
        "email": "support@brooksfurnishing.example.com", "phone": "+1 312 555 0109",
        "website": "https://brooksfurnishing.example.com", "product": "home decor",
        "country": "United States", "state": "Illinois", "city": "Chicago",
        "source": "Yellow Pages", "role": "Store owner",
    },
    {
        "contact_name": "Meera Iyer", "company_name": "Lotus Wellness Supply",
        "email": "meera@lotuswellness.example.com", "phone": "+1 206 555 0110",
        "website": "https://lotuswellness.example.com", "product": "singing bowls",
        "country": "United States", "state": "Washington", "city": "Seattle",
        "source": "Alibaba", "role": "Distributor",
    },
]

# Additional fictional prospects used by the labelled demo discovery run.
DISCOVERY_CANDIDATES: list[dict[str, Any]] = [
    {
        "contact_name": "Rachel Green", "company_name": "Blue Lotus Gifts",
        "email": "info@bluelotusgifts.example.com", "phone": "+1 713 555 0201",
        "website": "https://bluelotusgifts.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "role": "Retail buyer",
    },
    {
        "contact_name": "Omar Hassan", "company_name": "Houston Sound Healing Center",
        "email": "hello@houstonhealing.example.com", "phone": "+1 713 555 0202",
        "website": "https://houstonhealing.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "role": "Wellness store owner",
    },
    {
        "contact_name": "Sofia Martinez", "company_name": "Dallas Interior Studio",
        "email": "sales@dallasinterior.example.com", "phone": "+1 214 555 0203",
        "website": "https://dallasinterior.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Dallas",
        "role": "Interior designer",
    },
    {
        "contact_name": "Arjun Shah", "company_name": "Austin Imports Co",
        "email": "import@austinimports.example.com", "phone": "+1 512 555 0204",
        "website": "https://austinimports.example.com", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Austin",
        "role": "Importer",
    },
    {
        "contact_name": "Clara Miller", "company_name": "Mystic Home Decor",
        "email": "clara@mystichome.example.com", "phone": "+1 303 555 0205",
        "website": "https://mystichome.example.com", "product": "singing bowls",
        "country": "United States", "state": "Colorado", "city": "Denver",
        "role": "Showroom manager",
    },
    {
        "contact_name": "Rohan Verma", "company_name": "Pacific Sound Distributors",
        "email": "orders@pacificsound.example.com", "phone": "+1 415 555 0206",
        "website": "https://pacificsound.example.com", "product": "singing bowls",
        "country": "United States", "state": "California", "city": "San Francisco",
        "role": "Distributor",
    },
    {
        # Deliberate duplicate of Lumen Decor LLC to demonstrate duplicate prevention.
        "contact_name": "Aarav Mehta", "company_name": "Lumen Decor LLC",
        "email": "HELLO@lumendecor.example.com", "phone": "+1 713 555 0101",
        "website": "www.lumendecor.example.com/", "product": "singing bowls",
        "country": "United States", "state": "Texas", "city": "Houston",
        "role": "Showroom owner",
    },
]
