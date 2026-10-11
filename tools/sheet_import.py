#!/usr/bin/env python3
"""大会定義シート → contests/<id>/contest.yaml と catalog/。

シートの形: タブ「大会」（行 = 項目、列 = 大会。A 項目 / B キー / C 説明 / D 用途 / E 列以降が大会で 1 行目が id）、
「大会_en」（同じ形で英語の項目）、「トラック」「トラック_en」「ルール」（大会横断）。
読むのは B 列のキーと 1 行目の id だけ。A 列の文言や行の並びは自由に変えてよい。

    python tools/sheet_import.py --payload-env SHEET_PAYLOAD      # シートのボタンが渡す中身（gzip+base64 の JSON）を読む（GitHub Actions が使う）
    python tools/sheet_import.py --gids '{"大会": 123, ...}'      # 公開シートを CSV で読む（シートを公開しているときだけ）
    python tools/sheet_import.py --gids ... --dry-run               # 書かずに差分だけ

ほかのプログラムからは import_grids(grids) を呼ぶ（grids はタブ名 → 2 次元配列）。依存は PyYAML だけ。
"""
import argparse, base64, csv, datetime, difflib, gzip, io, json, os, re, sys, urllib.request
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ROOT / "schema" / "fields.yaml"
CATALOG_TRACKS = ROOT / "catalog" / "tracks.yaml"
CATALOG_RULES = ROOT / "catalog" / "rules.yaml"
TAB_JA, TAB_EN = "大会", "大会_en"
FIRST_COL = 5                                  # E 列から大会
SITE_LANG = {"日本語": "ja", "英語": "en", "両方": "both"}
HEADER = "# 大会定義シートから生成（tools/sheet_import.py）。直すときはシートを直して「大会データを更新」を押す。\n"
ORDER = ["id", "kind", "name", "name_en", "language", "site_language", "owners", "form_url", "paper_submission", "venue", "sponsors",
         "rules_profile", "rules", "tracks", "rounds", "texts", "extra"]

def load_fields() -> dict:
    return yaml.safe_load(FIELDS.read_text(encoding="utf-8"))

def load_catalog():
    return (yaml.safe_load(CATALOG_TRACKS.read_text(encoding="utf-8"))["tracks"],
            yaml.safe_load(CATALOG_RULES.read_text(encoding="utf-8"))["profiles"])

# ---------- セルの解釈 ----------
def parse_date_cell(s):
    """2026/07/04 → date / 2026/07/06〜 → open / 2026/10/17〜10/21 → date,end"""
    s = str(s).strip().replace("～", "〜").replace("~", "〜").replace("-", "/")
    if not s: return None
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})\s*(〜\s*(?:(\d{4})/)?(\d{1,2})/(\d{1,2})|〜)?$", s)
    if not m: return {"date": s, "_bad": True}
    out = {"date": datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))}
    if m.group(4) and m.group(4).strip() == "〜": out["open"] = True
    elif m.group(4): out["end"] = datetime.date(int(m.group(5) or m.group(1)), int(m.group(6)), int(m.group(7)))
    return out

def cast(v, t):
    if v is None or str(v).strip() == "": return None
    s = str(v).strip()
    if t == "int": return int(float(s))
    if t == "bool": return s.upper() == "TRUE"
    if t == "date": return (parse_date_cell(s) or {}).get("date")
    if t == "list": return [x.strip() for x in re.split(r"[,、\s　]+", s) if x.strip()]
    return s

def _set(d, path, value):
    parts = re.findall(r"([A-Za-z_]+)(?:\[([^\]]+)\])?", path); cur = d
    for i, (name, idx) in enumerate(parts):
        last = i == len(parts) - 1
        if idx and idx.isdigit():
            lst = cur.setdefault(name, []); k = int(idx) - 1
            while len(lst) <= k: lst.append({})
            if last: lst[k] = value
            else: cur = lst[k]
        elif idx:
            dct = cur.setdefault(name, {})
            if last: dct[idx] = value
            else: cur = dct.setdefault(idx, {})
        else:
            if last: cur[name] = value
            else: cur = cur.setdefault(name, {})

# ---------- 格子 → 辞書 ----------
def grid_columns(grid) -> list[str]:
    return [str(x).strip() for x in (grid[0] if grid else [])[FIRST_COL - 1:] if str(x).strip()]

def column_items(grid, cid):
    hdr = [str(x).strip() for x in (grid[0] if grid else [])]
    if cid not in hdr: return []
    j = hdr.index(cid)
    return [(str(r[0]) if r else "", (list(r) + [""] * (j + 1))[j], str((list(r) + ["", ""])[1]).strip()) for r in grid[1:]]

def items_to_contest(cid, ja_items, en_items=None, catalog=None) -> dict:
    f = load_fields(); types = {q["key"]: q["type"] for q in f["basic"]}
    for c in f["rounds"]["columns"]: types["rounds." + c["key"]] = c["type"]
    out = {"id": cid}
    def eat(items):
        for label, value, key in items:
            if not key or (key.endswith(".") and not label.strip()): continue
            if key.startswith("extra."):
                if label.strip() and str(value).strip(): out.setdefault("extra", {})[key[6:] or label.strip()] = value
                continue
            if key.startswith("tracks_enabled."):
                if str(value).upper() == "TRUE": out.setdefault("_enabled", []).append(key.split(".", 1)[1])
                continue
            if key.endswith(".date") and "milestones[" in key:
                d = parse_date_cell(value)
                if d:
                    for k2, v2 in d.items(): _set(out, key[:-5] + "." + k2, v2)
                continue
            if key == "site_language":
                if str(value).strip(): out["site_language"] = SITE_LANG.get(str(value).strip(), str(value).strip())
                continue
            base = re.sub(r"\[[^\]]+\]", "", key)
            v = cast(value, types.get(base) or types.get(base.split(".")[-1]) or "text")
            if v is None: continue
            _set(out, key, v)
    eat(ja_items)
    if en_items: eat(en_items)
    tracks, profiles = catalog or load_catalog()
    enabled = out.pop("_enabled", [])
    out["tracks"] = [dict(t) for t in tracks if t["id"] in enabled]
    if out.get("rules_profile") in profiles: out["rules"] = dict(profiles[out["rules_profile"]])
    rounds = []
    for r in out.get("rounds", []):
        if not r.get("id"): continue
        r["tracks"] = [t for t in (r.get("tracks") or enabled) if t in enabled]
        ms = r.get("milestones") or {}
        for k, m in ms.items():
            if m.pop("_bad", False): out.setdefault("_warn", []).append(f"日程「{k}」の日付が読めません: {m.get('date')}（例: 2026/07/04、2026/07/06〜、2026/10/17〜10/21）")
        def d(k): return str((ms.get(k) or {}).get("date") or "")
        vs = str((out.get("venue") or {}).get("start") or "")
        if vs and d("main_start") and vs < d("main_start"):
            out.setdefault("_warn", []).append(f"学会期間（開始）{vs} が本戦開始 {d('main_start')} より前です（年の書き間違い？）")
        if d("registration") and d("main_start") and d("registration") > d("main_start"):
            out.setdefault("_warn", []).append(f"登録締切 {d('registration')} が本戦開始 {d('main_start')} より後です")
        lab, lab_en = f.get("milestone_keys", {}), f.get("milestone_keys_en", {})
        for k, m in ms.items():                       # 表示文が空なら種類の名前（登録締切など）を出す
            if m.get("date") and not m.get("name"): m["name"] = lab.get(k, k)
            if m.get("date") and out.get("site_language") in ("en", "both") and not m.get("name_en"): m["name_en"] = lab_en.get(k, k)
        r["milestones"] = sorted(({"key": k, **{kk: vv for kk, vv in m.items() if vv not in (None, False, "")}} for k, m in ms.items() if m.get("date")),
                                 key=lambda m: str(m["date"]))
        rounds.append(r)
    out["rounds"] = rounds
    if "sponsors" in out:
        out["sponsors"] = [s for s in out["sponsors"] if s.get("name")]
        if not out["sponsors"]: out.pop("sponsors")
    return {k: out[k] for k in ORDER if k in out} | {k: v for k, v in out.items() if k not in ORDER}

def catalog_from_tabs(tracks_rows, tracks_en_rows, rules_rows):
    tracks, profiles = load_catalog()
    if tracks_rows and len(tracks_rows) > 2:
        en_map = {r[0]: (r[1] if len(r) > 1 else "") for r in (tracks_en_rows or [])[2:] if r and r[0]}
        tracks = []
        for r in tracks_rows[2:]:
            r = (list(r) + [""] * 7)[:7]
            if not str(r[0]).strip(): continue
            t = {"id": r[0].strip(), "name": r[1], "name_en": en_map.get(r[0].strip(), ""), "size": int(float(r[2])), "mode": r[3]}
            if str(r[4]).strip(): t["games_per_team"] = [int(float(r[4])), int(float(r[5] or r[4]))]
            tracks.append(t)
    if rules_rows and len(rules_rows) > 1:
        header = rules_rows[0]; ki = header.index("キー") if "キー" in header else len(header) - 2; names = header[1:ki]
        profiles = {n: {} for n in names}
        for r in rules_rows[1:]:
            r = (list(r) + [""] * len(header))[:len(header)]
            if not r[ki]: continue
            for j, n in enumerate(names, start=1): profiles[n][r[ki]] = int(float(r[j])) if str(r[j]).strip() else None
    return tracks, profiles

def is_active(c, today=None) -> bool:
    """学会期間が終わっていない（または未記入の）大会。必須項目の警告はこれだけに出す"""
    today = today or datetime.date.today()
    e = (c.get("venue") or {}).get("end")
    if not e: return True
    try: return datetime.date.fromisoformat(str(e)[:10]) >= today
    except ValueError: return True

def missing_required(c) -> list[str]:
    f = load_fields(); out = []
    def get(d, dotted):
        for k in dotted.split("."):
            if not isinstance(d, dict) or k not in d: return None
            d = d[k]
        return d
    for q in f["basic"]:
        if q.get("required") and get(c, q["key"]) in (None, "", []): out.append(q["label"])
        if q.get("required_en") and c.get("site_language") in ("en", "both") and get(c, q["key"]) in (None, "", []): out.append(q["label"])
    if not c.get("tracks"): out.append("開催トラック")
    if not c.get("rounds"): out.append("回 1 の識別子")
    return out

# ---------- 書き出し ----------
def dump_yaml(d) -> str:
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=200)

def save_catalog(tracks, profiles) -> list[str]:
    changed = []
    for path, key, val in ((CATALOG_TRACKS, "tracks", tracks), (CATALOG_RULES, "profiles", profiles)):
        old = path.read_text(encoding="utf-8")
        new = old.split(f"{key}:")[0] + dump_yaml({key: val})
        if yaml.safe_load(new) != yaml.safe_load(old):
            path.write_text(new, encoding="utf-8"); changed.append(path.name)
    return changed

def import_grids(grids: dict, write=True, only=None, log=print):
    """返り値: {"changed": [大会 id], "warnings": [文], "diffs": {id: 差分}}"""
    catalog = catalog_from_tabs(grids.get("トラック"), grids.get("トラック_en"), grids.get("ルール"))
    ja, en = grids.get(TAB_JA, []), grids.get(TAB_EN, [])
    res = {"changed": [], "warnings": [], "diffs": {}}
    if write:
        for n in save_catalog(*catalog): res["changed"].append(f"catalog/{n}")
    for cid in grid_columns(ja):
        if only and cid not in only: continue
        if not re.match(r"^[a-z0-9_]+$", cid):
            res["warnings"].append(f"{cid}: 大会 id は半角英小文字・数字・_ だけにしてください（この列は読みませんでした）"); continue
        c = items_to_contest(cid, column_items(ja, cid), column_items(en, cid), catalog)
        for w in c.pop("_warn", []): res["warnings"].append(f"{cid}: {w}")
        if is_active(c):
            miss = missing_required(c)
            if miss: res["warnings"].append(f"{cid}: 未記入の必須項目 — " + "、".join(miss))
        dst = ROOT / "contests" / cid / "contest.yaml"
        old_text = dst.read_text(encoding="utf-8") if dst.exists() else ""
        old = yaml.safe_load(old_text) if old_text else None
        if old == c: continue
        new_text = HEADER + dump_yaml(c)
        res["diffs"][cid] = "\n".join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), "現在", "シート", lineterm=""))
        res["changed"].append(cid)
        if write:
            dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(new_text, encoding="utf-8")
    return res

def fetch_csv(book_id: str, gid) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{book_id}/export?format=csv&gid={gid}"
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode("utf-8")
    if text.lstrip().startswith("<"): raise SystemExit(f"シートを CSV で読めませんでした（公開設定を確認）: gid={gid}")
    return list(csv.reader(io.StringIO(text)))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", help="スプレッドシート id（既定は sheet.yaml）")
    ap.add_argument("--gids", help='タブ名 → gid の JSON（公開シートを CSV で読むとき）')
    ap.add_argument("--payload-env", help="シートの中身（gzip+base64 の JSON。タブ名 → 2 次元配列）が入った環境変数の名前")
    ap.add_argument("--only", nargs="*"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    book = yaml.safe_load((ROOT / "sheet.yaml").read_text(encoding="utf-8"))["book_id"]
    if a.book and a.book != book: raise SystemExit(f"sheet.yaml の book_id と違うシートです: {a.book}")
    names = (TAB_JA, TAB_EN, "トラック", "トラック_en", "ルール")
    payload = os.environ.get(a.payload_env or "", "") if a.payload_env else ""
    if payload:
        grids = json.loads(gzip.decompress(base64.b64decode(payload)).decode("utf-8"))
        grids = {k: [[str(c) for c in row] for row in v] for k, v in grids.items() if k in names}
    elif a.gids:
        grids = {name: fetch_csv(book, gid) for name, gid in json.loads(a.gids).items() if name in names}
    else:
        raise SystemExit("--payload-env か --gids のどちらかが要ります")
    if TAB_JA not in grids: raise SystemExit("タブ「大会」がありません")
    res = import_grids(grids, write=not a.dry_run, only=a.only)
    for w in res["warnings"]: print(f"::warning::{w}")
    for cid, d in res["diffs"].items(): print(f"===== {cid}\n{d}")
    print("変更:", ", ".join(res["changed"]) or "なし")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh: fh.write(f"changed={','.join(res['changed'])}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write("## 大会定義シートからの更新\n\n" + (f"変更: {', '.join(res['changed'])}\n\n" if res["changed"] else "変更なし\n\n"))
            for w in res["warnings"]: fh.write(f"- ⚠ {w}\n")
            for cid, d in res["diffs"].items(): fh.write(f"\n<details><summary>{cid}</summary>\n\n```diff\n{d}\n```\n</details>\n")

if __name__ == "__main__":
    main()
