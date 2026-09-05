#!/usr/bin/env python3
"""
update_data.py  --  keeps data.json current for the Indigenous Financial Inclusion Monitor.

What it does
  1. Reads the Council of Indigenous Peoples (CIP) listing page for the
     原住民族綜合發展基金貸款 monthly reports (原住民族綜合發展基金貸款NNN年M月月報).
  2. For every month that is not yet in data.json, opens the item page,
     finds the PDF, downloads it and parses the table 原住民族綜合發展基金貸款案件月報表.
  3. Validates every parsed month (program totals, bank totals and
     mountain + plains must all reconcile to the grand total).
  4. Appends the month to data.json and stamps meta.updated.

Usage
  python update_data.py                      # fetch anything new and update data.json
  python update_data.py --pdf report.pdf --month 2025-09 --dry-run   # parse one local PDF and print it
  python update_data.py --max-pages 6        # how many listing pages to scan (default 6)

Requirements
  pip install requests pdfplumber

Notes
  * The CIP site paginates its listing with JavaScript (doPage(n)). The
    script tries the conventional "&page=N" query parameter; if the site
    changes, adjust LISTING_URL / page_url() below.
  * Report layout is a fixed template. The parser reads numbers by their
    x-position under the header cells, so blank cells cannot shift columns.
    If the layout changes the validation step will refuse the month and
    say why, rather than silently storing wrong figures.
"""
import argparse, datetime as dt, io, json, re, sys, unicodedata
from pathlib import Path

import requests

BASE = "https://www.cip.gov.tw"
LISTING_URL = BASE + "/zh-tw/news/data-list/23DD6FC526F7465A/index.html?cumid=23DD6FC526F7465A"
DATA_PATH = Path(__file__).with_name("data.json")
TITLE_RE = re.compile(r"原住民族綜合發展基金貸款\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*月報")
HEADERS = {"User-Agent": "IFIM-monitor/1.0 (+data refresh script)"}

# Program (貸款類別) blocks in the order they appear in the report, with the
# number of bank sub-blocks each one has, and the text label used in the PDF.
PROGRAMS = [
    ("economic",   "經濟事業", 5),
    ("housing",    "住宅貸款", 3),
    ("youth",      "青年創業", 4),
    ("micro",      "微型經濟", 5),
    ("enterprise", "原住民族", 4),
]
BANK_KEYS = {"合庫": "coop", "土銀": "land", "臺企": "tbb", "農會": "farm", "農金": "farm", "高銀": "kh"}

# ----------------------------------------------------------------------------
# listing / download
# ----------------------------------------------------------------------------
def page_url(n):
    return LISTING_URL if n == 1 else LISTING_URL + f"&page={n}"

def find_reports(max_pages):
    """Return {'YYYY-MM': item_page_url} for every monthly report found in the listing."""
    found = {}
    seen_pages = set()
    for n in range(1, max_pages + 1):
        html = requests.get(page_url(n), headers=HEADERS, timeout=30).text
        items = re.findall(r'href="(https://www\.cip\.gov\.tw/zh-tw/news/data-list/23DD6FC526F7465A/[A-F0-9]+-info\.html)"[^>]*>([^<]*?月報[^<]*)<', html)
        if not items:
            items = [(m.group(1), m.group(2)) for m in re.finditer(r'<a[^>]+href="([^"]+-info\.html)"[^>]*>(.*?)</a>', html, re.S) if "月報" in m.group(2)]
        key = tuple(u for u, _ in items)
        if not items or key in seen_pages:
            break
        seen_pages.add(key)
        for url, title in items:
            m = TITLE_RE.search(re.sub(r"\s+", "", title))
            if not m:
                continue
            roc_year, month = int(m.group(1)), int(m.group(2))
            ym = f"{roc_year + 1911:04d}-{month:02d}"
            found.setdefault(ym, url if url.startswith("http") else BASE + url)
    return found

def pdf_link(item_url):
    html = requests.get(item_url, headers=HEADERS, timeout=30).text
    m = re.search(r'href="([^"]+\.pdf[^"]*)"', html)
    if not m:
        raise RuntimeError(f"no PDF link on {item_url}")
    url = m.group(1).replace("&amp;", "&")
    return url if url.startswith("http") else BASE + url

def download(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.content

# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------
NUM_RE = re.compile(r"^-?[\d,]+$")
DASHES = {"-", "－", "—", "–"}

def _num(tok):
    if tok in DASHES:
        return 0
    return int(tok.replace(",", ""))

def parse_pdf(pdf_bytes, ym):
    """Parse one monthly report into the data.json month schema. Raises on any inconsistency."""
    import pdfplumber
    words = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        offset = 0.0
        for page in pdf.pages:          # the report is normally one page; stack pages if it spills
            for w in page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5, y_tolerance=2):
                w["top"] += offset; w["bottom"] += offset
                words.append(w)
            offset += page.height
    # normalise text: full-width digits/commas, Kangxi-radical lookalikes (⼭ ⾦ ⽉ ...) and odd dashes
    for w in words:
        w["text"] = unicodedata.normalize("NFKC", w["text"]).replace("－", "-").replace("—", "-")

    # group words into rows by vertical position
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(rows[-1]["top"] - w["top"]) <= 3:
            rows[-1]["words"].append(w)
        else:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])

    # header: the row that contains four pairs of 戶數 / 金額 gives the 8 numeric column centres
    is_hdr = lambda t: t.endswith("數") or t == "金額"
    header = next((r for r in rows if sum(1 for w in r["words"] if is_hdr(w["text"])) >= 8), None)
    if not header:
        raise RuntimeError(f"{ym}: header row (戶數/金額 x4) not found; layout changed?")
    cols = [((w["x0"] + w["x1"]) / 2) for w in header["words"] if is_hdr(w["text"])][:8]
    col_names = ["new_n", "new_amt", "ytd_n", "ytd_amt", "rep_n", "rep_amt", "n", "bal"]

    def nearest_col(x):
        return min(range(8), key=lambda i: abs(cols[i] - x))

    # numeric rows: label + 8 values by column
    data_rows = []
    for r in rows:
        if r is header or r["top"] < header["top"]:
            continue
        label = "".join(w["text"] for w in r["words"] if not NUM_RE.match(w["text"]) and w["text"] not in DASHES)
        nums = [w for w in r["words"] if NUM_RE.match(w["text"]) or w["text"] in DASHES]
        if not nums:
            continue
        vals = [0] * 8
        for w in nums:
            vals[nearest_col((w["x0"] + w["x1"]) / 2)] = _num(w["text"])
        data_rows.append({"top": r["top"], "label": label, "vals": vals})

    def rec(vals):
        return dict(zip(col_names, vals))

    # walk the rows: mountain / plains / subtotal triplets form bank sub-blocks;
    # a row without one of those labels closes a program block (its total row).
    i = 0
    blocks = []
    cur = []
    while i < len(data_rows):
        r = data_rows[i]
        if "山地" in r["label"] and i + 2 < len(data_rows) and "平地" in data_rows[i + 1]["label"]:
            sub = {"mtn": rec(r["vals"]), "pln": rec(data_rows[i + 1]["vals"]), "sub": rec(data_rows[i + 2]["vals"]), "top": r["top"], "bottom": data_rows[i + 2]["top"]}
            cur.append(sub)
            i += 3
            continue
        if ("合計" in r["label"] or "總計" in r["label"] or not any(k in r["label"] for k in ("山地", "平地", "小計"))) and cur:
            blocks.append({"subs": cur, "total": rec(r["vals"]), "top": cur[0]["top"], "bottom": r["top"]})
            cur = []
        i += 1
        if len(blocks) == len(PROGRAMS):
            break

    if len(blocks) != len(PROGRAMS):
        raise RuntimeError(f"{ym}: expected {len(PROGRAMS)} program blocks, found {len(blocks)}")

    # remaining rows: bank totals (合庫/土銀/臺企/農會/高銀) then 山地 / 平地 / 總計
    tail = data_rows[i:]
    bank = {}
    grand = {}
    for r in tail:
        lab = r["label"]
        for k, key in BANK_KEYS.items():
            if k in lab and "山地" not in lab and "平地" not in lab:
                bank[key] = rec(r["vals"])
        if "山地" in lab:
            grand["mtn"] = rec(r["vals"])
        elif "平地" in lab:
            grand["pln"] = rec(r["vals"])
        elif "mtn" in grand and "pln" in grand and "tot" not in grand and not any(k in lab for k in BANK_KEYS):
            grand["tot"] = rec(r["vals"])
    if not {"mtn", "pln", "tot"} <= set(grand) or len(bank) < 4:
        raise RuntimeError(f"{ym}: grand-total or bank rows not found")

    # optional: confirm program labels by position (vertical text in merged cells)
    label_words = [w for w in words if any(w["text"].startswith(p[1][:2]) for p in PROGRAMS)]
    for (key, label, nsubs), b in zip(PROGRAMS, blocks):
        if len(b["subs"]) != nsubs:
            raise RuntimeError(f"{ym}: program {key} has {len(b['subs'])} bank sub-blocks, expected {nsubs}")
        near = [w for w in label_words if b["top"] - 6 <= w["top"] <= b["bottom"] + 6]
        if near and not any(w["text"].startswith(label[:2]) for w in near):
            raise RuntimeError(f"{ym}: block order differs from template near y={b['top']:.0f} ({[w['text'] for w in near]})")

    # ---- assemble ----
    cat = {}
    for (key, label, nsubs), b in zip(PROGRAMS, blocks):
        mtn_bal = sum(s["mtn"]["bal"] for s in b["subs"]); pln_bal = sum(s["pln"]["bal"] for s in b["subs"])
        mtn_n = sum(s["mtn"]["n"] for s in b["subs"]); pln_n = sum(s["pln"]["n"] for s in b["subs"])
        c = {"n": b["total"]["n"], "bal": b["total"]["bal"], "mtn": mtn_bal, "pln": pln_bal, "mtn_n": mtn_n, "pln_n": pln_n,
             "new_n": b["total"]["new_n"], "new_amt": b["total"]["new_amt"], "ytd_n": b["total"]["ytd_n"], "ytd_amt": b["total"]["ytd_amt"]}
        if key == "housing":
            # per-bank detail, in template order 合庫, 土銀, 臺企
            names = ["coop", "land", "tbb"]
            c["banks"] = {names[k]: {"n": s["sub"]["n"], "bal": s["sub"]["bal"], "mtn": s["mtn"]["bal"], "pln": s["pln"]["bal"]}
                          for k, s in enumerate(b["subs"]) if s["sub"]["bal"]}
        cat[key] = c

    month = {
        "d": ym,
        "tot": grand["tot"]["bal"], "mtn": grand["mtn"]["bal"], "pln": grand["pln"]["bal"],
        "accts": {"tot": grand["tot"]["n"], "mtn": grand["mtn"]["n"], "pln": grand["pln"]["n"]},
        "flow": {k: grand["tot"][k] for k in ("new_n", "new_amt", "ytd_n", "ytd_amt", "rep_n", "rep_amt")},
        "cat": cat,
        "bank": {k: {"n": v["n"], "bal": v["bal"]} for k, v in bank.items()},
    }
    validate(month)
    return month

def validate(m):
    ym = m["d"]
    def close(a, b, what):
        if a != b:
            raise RuntimeError(f"{ym}: {what} do not reconcile ({a:,} vs {b:,})")
    close(m["mtn"] + m["pln"], m["tot"], "mountain + plains balances")
    close(sum(c["bal"] for c in m["cat"].values()), m["tot"], "program balances vs grand total")
    close(sum(c["n"] for c in m["cat"].values()), m["accts"]["tot"], "program accounts vs grand total")
    close(sum(b["bal"] for b in m["bank"].values()), m["tot"], "bank balances vs grand total")
    for k, c in m["cat"].items():
        close(c["mtn"] + c["pln"], c["bal"], f"{k}: mountain + plains")
        close(c["mtn_n"] + c["pln_n"], c["n"], f"{k}: mountain + plains accounts")
    close(sum(c["mtn"] for c in m["cat"].values()), m["mtn"], "mountain balances across programs")

# ----------------------------------------------------------------------------
# monthly population feed (Ministry of the Interior, household registration)
# API: https://www.ris.gov.tw/rs-opendata/api/v1/datastore/ODRP009/{ROC yyymm}
# One row per village: aborigine_total, aborigine_total_m/_f and one m/f pair per people.
# ----------------------------------------------------------------------------
RIS_URL = "https://www.ris.gov.tw/rs-opendata/api/v1/datastore/ODRP009/{yyymm}"
MTN_AREAS = {("新北市","烏來區"),("桃園市","復興區"),("臺中市","和平區"),("高雄市","茂林區"),("高雄市","桃源區"),("高雄市","那瑪夏區"),
             ("宜蘭縣","大同鄉"),("宜蘭縣","南澳鄉"),("新竹縣","尖石鄉"),("新竹縣","五峰鄉"),("苗栗縣","泰安鄉"),("南投縣","信義鄉"),("南投縣","仁愛鄉"),
             ("嘉義縣","阿里山鄉"),("屏東縣","三地門鄉"),("屏東縣","霧臺鄉"),("屏東縣","瑪家鄉"),("屏東縣","泰武鄉"),("屏東縣","來義鄉"),("屏東縣","春日鄉"),
             ("屏東縣","獅子鄉"),("屏東縣","牡丹鄉"),("臺東縣","海端鄉"),("臺東縣","延平鄉"),("臺東縣","金峰鄉"),("臺東縣","達仁鄉"),("臺東縣","蘭嶼鄉"),
             ("花蓮縣","秀林鄉"),("花蓮縣","萬榮鄉"),("花蓮縣","卓溪鄉")}
PLN_AREAS = {("新竹縣","關西鎮"),("苗栗縣","南庄鄉"),("苗栗縣","獅潭鄉"),("南投縣","魚池鄉"),("屏東縣","滿州鄉"),("臺東縣","臺東市"),("臺東縣","成功鎮"),
             ("臺東縣","關山鎮"),("臺東縣","卑南鄉"),("臺東縣","鹿野鄉"),("臺東縣","池上鄉"),("臺東縣","東河鄉"),("臺東縣","長濱鄉"),("臺東縣","太麻里鄉"),
             ("臺東縣","大武鄉"),("花蓮縣","花蓮市"),("花蓮縣","鳳林鎮"),("花蓮縣","玉里鎮"),("花蓮縣","新城鄉"),("花蓮縣","吉安鄉"),("花蓮縣","壽豐鄉"),
             ("花蓮縣","光復鄉"),("花蓮縣","豐濱鄉"),("花蓮縣","瑞穗鄉"),("花蓮縣","富里鄉")}
TRIBES = ["amis","paiwan","atayal","bunun","truku","puyuma","rukai","sediq","saisiyat","tsou","yami","kavalan","sakizaya","thao","kanakanavu","hlaaluaavu","siraya"]

def _area(site_id):
    site = site_id.replace("台", "臺")
    for county, town in MTN_AREAS:
        if site.startswith(county) and town in site: return "mtn"
    for county, town in PLN_AREAS:
        if site.startswith(county) and town in site: return "pln"
    return "other"

def fetch_population(yyymm):
    """Return one population record for ROC yyymm (e.g. 11507), or None if the month is not published yet."""
    rows = []
    for page in range(1, 300):
        r = requests.get(RIS_URL.format(yyymm=yyymm), params={"page": page}, headers=HEADERS, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        data = j.get("responseData") or j.get("result") or (j if isinstance(j, list) else [])
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000 and page >= int(j.get("totalPage", page) or page):
            break
    if not rows:
        return None
    def num(v):
        try: return int(str(v).replace(",", "") or 0)
        except ValueError: return 0
    rec = {"d": f"{int(str(yyymm)[:-2]) + 1911}-{str(yyymm)[-2:]}", "total": 0, "m": 0, "f": 0, "mtn_area": 0, "pln_area": 0, "other_area": 0, "tribes": {}}
    for row in rows:
        t = num(row.get("aborigine_total")); rec["total"] += t
        rec["m"] += num(row.get("aborigine_total_m")); rec["f"] += num(row.get("aborigine_total_f"))
        rec[_area(str(row.get("site_id", ""))) + "_area"] += t
        for k, v in row.items():
            if k.startswith("aborigine_") and (k.endswith("_m") or k.endswith("_f")) and not k.startswith("aborigine_total"):
                name = k[len("aborigine_"):-2]
                rec["tribes"][name] = rec["tribes"].get(name, 0) + num(v)
    if rec["total"] <= 0:
        raise RuntimeError(f"population {yyymm}: rows fetched but totals are zero; field names changed?")
    if abs(rec["m"] + rec["f"] - rec["total"]) > 5:
        raise RuntimeError(f"population {yyymm}: male + female ({rec['m'] + rec['f']:,}) does not reconcile with total ({rec['total']:,})")
    return rec

def latest_population(existing):
    """Try the most recent months (ROC calendar) not already stored; return a list of new records."""
    today = dt.date.today()
    have = {p["d"] for p in existing}
    new = []
    y, m = today.year, today.month
    for _ in range(4):                       # look back up to four months for the newest published file
        m -= 1
        if m == 0: y, m = y - 1, 12
        key = f"{y}-{m:02d}"
        if key in have:
            break
        rec = fetch_population(int(f"{y - 1911}{m:02d}"))
        if rec:
            new.append(rec)
            break                            # one new month per run is enough; the workflow runs twice a month
    return new

# ----------------------------------------------------------------------------
# NHI: 全國原住民納保分布 (National Health Insurance enrolment of Indigenous people by insured category)
# resource A21030000I-B03005. The download path is discovered from the agency's OpenAPI document,
# because the platform does not publish a stable direct URL. Columns are matched by keyword.
# ----------------------------------------------------------------------------
NHI_OAS = ["https://data.nhi.gov.tw/openapi.json", "https://info.nhi.gov.tw/IODE0000/openapi.json"]
NHI_RESOURCE = "A21030000I-B03005"

def fetch_nhi():
    import csv
    path = None
    for oas in NHI_OAS:
        try:
            j = requests.get(oas, headers=HEADERS, timeout=30).json()
        except Exception:
            continue
        for p, spec in (j.get("paths") or {}).items():
            if NHI_RESOURCE in p or NHI_RESOURCE in json.dumps(spec, ensure_ascii=False):
                base = (j.get("servers") or [{"url": oas.rsplit("/", 1)[0]}])[0]["url"]
                path = base.rstrip("/") + p
                break
        if path:
            break
    if not path:
        raise RuntimeError("NHI: resource path not found in the OpenAPI documents")
    r = requests.get(path, headers=HEADERS, timeout=60); r.raise_for_status()
    text = r.content.decode("utf-8-sig", "replace")
    try:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("result") or next((v for v in rows.values() if isinstance(v, list)), [])
    except json.JSONDecodeError:
        rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("NHI: empty dataset")
    cols = list(rows[0].keys())
    cat_col = next((c for c in cols if "類別" in c or "類目" in c), None)
    n_col = next((c for c in cols if "人數" in c), None)
    if not (cat_col and n_col):
        raise RuntimeError(f"NHI: could not identify category / count columns among {cols}")
    def num(v):
        try: return int(str(v).replace(",", "") or 0)
        except ValueError: return 0
    by_cat = {}
    for row in rows:
        by_cat[str(row[cat_col]).strip()] = by_cat.get(str(row[cat_col]).strip(), 0) + num(row[n_col])
    total = sum(by_cat.values())
    low_income = sum(v for k, v in by_cat.items() if "第五類" in k or "五類" in k or "低收入" in k)
    employer = sum(v for k, v in by_cat.items() if "第一類" in k or "一類" in k)
    return {"d": dt.date.today().strftime("%Y-%m"), "total": total, "low_income": low_income, "employer_insured": employer, "categories": by_cat}

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="parse a local PDF instead of scraping")
    ap.add_argument("--month", help="YYYY-MM for --pdf")
    ap.add_argument("--dry-run", action="store_true", help="print the parsed month, do not write data.json")
    ap.add_argument("--max-pages", type=int, default=6)
    a = ap.parse_args()

    data = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else {"meta": {}, "months": []}
    have = {m["d"]: m for m in data["months"]}
    changed = False

    if a.pdf:
        if not a.month:
            sys.exit("--month YYYY-MM is required with --pdf")
        month = parse_pdf(Path(a.pdf).read_bytes(), a.month)
        print(json.dumps(month, ensure_ascii=False, indent=1))
        if a.dry_run:
            return
        have[a.month] = month
        changed = True
    else:
        reports = find_reports(a.max_pages)
        print(f"listing: {len(reports)} monthly reports found; {sum(1 for ym in reports if ym not in have or 'cat' not in have[ym])} new or without program detail")
        for ym in sorted(reports):
            if ym in have and "cat" in have[ym]:
                continue
            try:
                url = pdf_link(reports[ym])
                month = parse_pdf(download(url), ym)
            except Exception as e:  # keep going; report at the end
                print(f"  {ym}: SKIPPED  {e}")
                continue
            if ym in have:   # legacy row with totals only: check they agree, then enrich
                old = have[ym]
                if old["tot"] != month["tot"]:
                    print(f"  {ym}: WARNING legacy total {old['tot']:,} differs from report {month['tot']:,}; report wins")
            have[ym] = month
            changed = True
            print(f"  {ym}: ok  total NT${month['tot']:,}  accounts {month['accts']['tot']:,}")

    # ---- monthly population (MOI) ----
    if not a.pdf:
        try:
            pop = data.setdefault("population", [])
            new_pop = latest_population(pop)
            if new_pop:
                pop.extend(new_pop); pop.sort(key=lambda p: p["d"]); changed = True
                for p in new_pop:
                    print(f"  population {p['d']}: {p['total']:,} people, {p['other_area'] / p['total']:.1%} outside Indigenous areas")
            else:
                print("  population: nothing new")
        except Exception as e:
            print(f"  population: SKIPPED  {e}")
        # ---- NHI enrolment (best effort until the first run confirms the columns) ----
        try:
            nhi = fetch_nhi()
            prev = data.get("nhi") or {}
            if nhi and nhi != prev:
                data["nhi"] = nhi; changed = True
                print(f"  NHI: {nhi['total']:,} Indigenous insured; low-income category {nhi['low_income']:,}")
        except Exception as e:
            print(f"  NHI: SKIPPED  {e}")

    if changed and not a.dry_run:
        data["months"] = [have[k] for k in sorted(have)]
        data["meta"]["updated"] = dt.date.today().isoformat()
        data["meta"].setdefault("source", "Council of Indigenous Peoples, 原住民族綜合發展基金貸款案件月報表")
        DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"data.json written: {len(data['months'])} months, updated {data['meta']['updated']}")
    elif not changed:
        print("nothing new")

if __name__ == "__main__":
    main()
