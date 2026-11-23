#!/usr/bin/env python3
"""
Wurfquoten aller Spieler der Männer NLB (Saison 2026/27) von handball.ch.
Rohdaten pro Spiel werden in ./cache/ gespeichert; ein erneuter Lauf holt nur neue Spiele.
Hinweis: "Tore" auf handball.ch = Treffer/Versuche INKLUSIVE 7m. Feldwürfe = Total minus 7m.
"""
import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd

BASE = "https://www.handball.ch/de/matchcenter"
GROUP_ID = 17526  # Männer NLB, Hauptrunde MNLB-01, Saison 2026/27
CACHE = Path("cache")

# JS: true, sobald beide Statistik-Tabellen Spielerzeilen enthalten
READY_JS = """
() => { const ts = [...document.querySelectorAll('table.c-table--statistic')];
        return ts.length >= 2 && ts.every(t => [...t.rows].some(
            r => /^\\d{1,3}$/.test(((r.cells[0] && r.cells[0].textContent) || '').trim()))); }
"""

EXTRACT_JS = """
() => [...document.querySelectorAll('table.c-table--statistic')].map(t => ({
    team: t.rows[0] ? t.rows[0].cells[0].textContent.replace(/\\s+/g, ' ').trim() : '',
    rows: [...t.rows].map(r => [...r.cells].map(c => c.textContent.replace(/\\s+/g, ' ').trim()))
}))
"""


def ratio(cell):
    """'9/10 9' -> (9, 10); '0' oder '' -> (0, 0). Format: Treffer/Versuche."""
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", cell or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def parse_team_table(table):
    players, goalies = [], []
    section = "players"
    for cells in table["rows"][4:]:
        cells = cells + [""] * (9 - len(cells))
        if not any(cells):
            section = {"players": "goalies", "goalies": "staff"}.get(section, "staff")
            continue
        jersey, name = cells[0], cells[1]
        if section == "players" and jersey.isdigit():
            goals, shots = ratio(cells[2])
            g7, s7 = ratio(cells[3])
            players.append({"team": table["team"], "name": name, "jersey": int(jersey),
                            "tore": goals, "wuerfe": shots, "tore_7m": g7, "wuerfe_7m": s7})
        elif section == "goalies" and jersey.isdigit():
            saves, faced = ratio(cells[2])
            s7g, f7g = ratio(cells[3])
            goalies.append({"team": table["team"], "name": name, "jersey": int(jersey),
                            "paraden": saves, "gegenwuerfe": faced,
                            "paraden_7m": s7g, "gegenwuerfe_7m": f7g})
    return players, goalies


def parse_game(tables):
    players, goalies = [], []
    for t in tables:
        p, g = parse_team_table(t)
        players += p
        goalies += g
    return players, goalies


def aggregate(game_players):
    df = pd.DataFrame(game_players)
    if df.empty:
        return df
    df["spiel_mit_wurf"] = (df["wuerfe"] > 0).astype(int)
    df["spiele_kader"] = 1
    agg = (df.groupby(["team", "name"], as_index=False)
             .agg(spiele_kader=("spiele_kader", "sum"), spiele_mit_wurf=("spiel_mit_wurf", "sum"),
                  tore=("tore", "sum"), wuerfe=("wuerfe", "sum"),
                  tore_7m=("tore_7m", "sum"), wuerfe_7m=("wuerfe_7m", "sum")))
    agg["tore_feld"] = agg["tore"] - agg["tore_7m"]
    agg["wuerfe_feld"] = agg["wuerfe"] - agg["wuerfe_7m"]
    agg["quote_total_%"] = (100 * agg["tore"] / agg["wuerfe"]).round(1)
    agg["quote_feld_%"] = (100 * agg["tore_feld"] / agg["wuerfe_feld"].where(agg["wuerfe_feld"] > 0)).round(1)
    agg["quote_7m_%"] = (100 * agg["tore_7m"] / agg["wuerfe_7m"].where(agg["wuerfe_7m"] > 0)).round(1)
    return agg


def aggregate_goalies(game_goalies):
    df = pd.DataFrame(game_goalies)
    if df.empty:
        return df
    agg = (df.groupby(["team", "name"], as_index=False)
             [["paraden", "gegenwuerfe", "paraden_7m", "gegenwuerfe_7m"]].sum())
    agg["paradenquote_%"] = (100 * agg["paraden"] / agg["gegenwuerfe"].where(agg["gegenwuerfe"] > 0)).round(1)
    return agg


def dismiss_cookies(page):
    try:
        page.get_by_role("button", name=re.compile("Ablehnen", re.I)).click(timeout=3000)
    except Exception:
        pass


def discover_game_ids(page):
    page.goto(f"{BASE}/gruppen/{GROUP_ID}", wait_until="networkidle")
    dismiss_cookies(page)
    page.evaluate("""() => { const t=[...document.querySelectorAll('[role=tab]')]
                              .find(x=>x.textContent.trim()==='Resultate'); if(t) t.click(); }""")
    page.wait_for_timeout(2500)
    ids = set(re.findall(r"spiele/(\d{5,7})", page.content()))
    return sorted(ids)


def fetch_game(page, game_id):
    cache_file = CACHE / f"{game_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    page.goto(f"{BASE}/spiele/{game_id}", wait_until="networkidle")
    dismiss_cookies(page)
    page.evaluate("""() => { const t=[...document.querySelectorAll('[role=tab]')]
                              .find(x=>x.textContent.trim()==='Stats'); if(t) t.click(); }""")
    try:
        # Die Tabellen erscheinen zuerst als leeres Gerüst. Warten, bis in beiden
        # Team-Tabellen echte Spielerzeilen (Rückennummer in der ersten Zelle) stehen.
        page.wait_for_function(READY_JS, timeout=20000)
    except Exception:
        return None  # Spiel noch nicht gespielt / keine Statistik
    tables = page.evaluate(EXTRACT_JS)
    CACHE.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(tables, ensure_ascii=False))
    return tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="Komma-getrennte Spiel-IDs (statt automatischer Suche)")
    ap.add_argument("--min-wuerfe", type=int, default=0, help="Mindestanzahl Würfe für die Ausgabe")
    ap.add_argument("--delay", type=float, default=1.0, help="Pause zwischen Spielen (Sekunden)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    all_players, all_goalies, skipped = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        ids = args.ids.split(",") if args.ids else discover_game_ids(page)
        print(f"{len(ids)} Spiele gefunden")
        for i, gid in enumerate(ids, 1):
            tables = fetch_game(page, gid)
            if not tables:
                skipped.append(gid)
                print(f"[{i}/{len(ids)}] {gid}: keine Statistik")
                continue
            pl, go = parse_game(tables)
            for r in pl + go:
                r["spiel_id"] = gid
            all_players += pl
            all_goalies += go
            print(f"[{i}/{len(ids)}] {gid}: {len(pl)} Spieler")
            time.sleep(args.delay)
        browser.close()

    if not all_players:
        print("Keine Spielerdaten gefunden, Abbruch. Übersprungen:", skipped)
        return

    res = aggregate(all_players)
    res = res[res["wuerfe"] >= args.min_wuerfe].sort_values(["quote_total_%", "wuerfe"], ascending=False)
    res.to_csv("nlb_wurfquoten.csv", index=False, encoding="utf-8-sig")
    gk = aggregate_goalies(all_goalies)
    if not gk.empty:
        gk.sort_values("paradenquote_%", ascending=False).to_csv("nlb_goalies.csv", index=False, encoding="utf-8-sig")
    print(f"\n{len(res)} Spieler -> nlb_wurfquoten.csv; übersprungen: {skipped}")
    print(res.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
