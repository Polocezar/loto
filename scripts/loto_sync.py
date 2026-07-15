#!/usr/bin/env python3
"""
Robot de synchronisation des statistiques Loto FDJ.

Ce script :
1. Télécharge l'archive officielle FDJ de l'historique des tirages Loto
   (période novembre 2019 -> aujourd'hui, mise à jour par FDJ elle-même).
2. Extrait le CSV du ZIP.
3. Calcule des statistiques DESCRIPTIVES sur l'historique passé :
   - fréquence d'apparition de chaque numéro (1-49) et numéro chance (1-10)
   - écart (nombre de tirages) depuis la dernière sortie de chaque numéro
   - top 5 des numéros les plus sortis : sur tout l'historique, le dernier
     an, le dernier trimestre, le dernier mois
4. Écrit tout ça dans data/loto-stats.json, lu ensuite par la page du site.

IMPORTANT : ce script ne calcule et n'affiche AUCUNE "probabilité de sortie
future". Le Loto est un jeu de hasard pur ; chaque tirage est indépendant.
Ces statistiques ne concernent que le passé.
"""

import csv
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

# URL officielle FDJ (archive Loto, novembre 2019 -> aujourd'hui).
# FDJ met à jour le contenu de ce lien au fil du temps ; si le lien change un
# jour, va sur https://www.fdj.fr/jeux-de-tirage/loto/historique et remplace
# l'URL ci-dessous par celle du bloc "Historique Loto" le plus récent.
FDJ_ARCHIVE_URL = (
    "https://www.sto.api.fdj.fr/anonymous/service-draw-info/v3/"
    "documentations/1a2b3c4d-9876-4562-b3fc-2c963f66afp6"
)

OUTPUT_PATH = "data/loto-stats.json"

DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%Y%m%d", "%d-%m-%Y")


def download_archive(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ALCF-loto-sync/1.0)"})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def extract_csv_text(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("Aucun fichier CSV trouvé dans l'archive ZIP.")
        raw = zf.read(csv_names[0])
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def detect_delimiter(sample: str) -> str:
    first_line = sample.split("\n", 1)[0]
    return ";" if first_line.count(";") >= first_line.count(",") else ","


def parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_draws(csv_text: str):
    delimiter = detect_delimiter(csv_text)
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    fieldnames = reader.fieldnames or []

    main_cols = [f for f in fieldnames if f and "boule" in f.lower() and "chance" not in f.lower()]
    chance_cols = [f for f in fieldnames if f and "chance" in f.lower()]
    date_cols = [f for f in fieldnames if f and "date" in f.lower() and "forclusion" not in f.lower()]
    date_col = date_cols[0] if date_cols else None

    draws = []
    for row in reader:
        try:
            nums = [int(row[c]) for c in main_cols if row.get(c)]
        except (ValueError, TypeError):
            continue
        nums = [n for n in nums if 1 <= n <= 49]
        if len(nums) < 5:
            continue
        chance = None
        if chance_cols:
            try:
                c = int(row[chance_cols[0]])
                if 1 <= c <= 10:
                    chance = c
            except (ValueError, TypeError):
                pass
        date_obj = parse_date(row.get(date_col, "")) if date_col else None
        draws.append({
            "date_raw": row.get(date_col, "") if date_col else "",
            "date_obj": date_obj,
            "numbers": sorted(nums[:5]),
            "chance": chance,
        })
    return draws


def top5_for_window(draws, cutoff_date):
    """Fréquence des numéros pour les tirages dont la date >= cutoff_date.
    Si cutoff_date est None, prend tout l'historique."""
    freq = {}
    count = 0
    for d in draws:
        if cutoff_date is not None:
            if d["date_obj"] is None or d["date_obj"] < cutoff_date:
                continue
        count += 1
        for n in d["numbers"]:
            freq[n] = freq.get(n, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
    top5 = [{"number": n, "count": c} for n, c in ranked[:5]]
    return top5, count


def compute_stats(draws):
    freq_main = {n: 0 for n in range(1, 50)}
    freq_chance = {n: 0 for n in range(1, 11)}

    for d in draws:
        for n in d["numbers"]:
            if n in freq_main:
                freq_main[n] += 1
        if d["chance"] in freq_chance:
            freq_chance[d["chance"]] += 1

    # Écart depuis la dernière sortie (0 = sorti au dernier tirage connu).
    gap_main = {n: None for n in range(1, 50)}
    for idx in range(len(draws) - 1, -1, -1):
        for n in draws[idx]["numbers"]:
            if gap_main.get(n) is None:
                gap_main[n] = len(draws) - 1 - idx
    for n in gap_main:
        if gap_main[n] is None:
            gap_main[n] = len(draws)

    gap_chance = {n: None for n in range(1, 11)}
    for idx in range(len(draws) - 1, -1, -1):
        c = draws[idx]["chance"]
        if c in gap_chance and gap_chance[c] is None:
            gap_chance[c] = len(draws) - 1 - idx
    for n in gap_chance:
        if gap_chance[n] is None:
            gap_chance[n] = len(draws)

    total = len(draws)
    last_draw = draws[-1] if draws else None

    # Dernière date connue (référence pour les fenêtres temporelles).
    dated = [d["date_obj"] for d in draws if d["date_obj"] is not None]
    reference_date = max(dated) if dated else None

    top5_all_time, n_all = top5_for_window(draws, None)

    if reference_date is not None:
        top5_month, n_month = top5_for_window(draws, reference_date - timedelta(days=31))
        top5_quarter, n_quarter = top5_for_window(draws, reference_date - timedelta(days=92))
        top5_year, n_year = top5_for_window(draws, reference_date - timedelta(days=366))
        dates_reliable = True
    else:
        # Repli si aucune date n'a pu être lue : approximation par nombre de
        # tirages (Loto = ~3 tirages/semaine -> ~13/mois, ~39/trimestre, ~156/an).
        top5_month, n_month = top5_for_window(draws[-13:], None)
        top5_quarter, n_quarter = top5_for_window(draws[-39:], None)
        top5_year, n_year = top5_for_window(draws[-156:], None)
        dates_reliable = False

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_draws_analyzed": total,
        "last_draw": {
            "date": last_draw["date_raw"] if last_draw else None,
            "numbers": last_draw["numbers"] if last_draw else None,
            "chance": last_draw["chance"] if last_draw else None,
        } if last_draw else None,
        "frequency_main": freq_main,
        "frequency_chance": freq_chance,
        "gap_since_last_seen_main": gap_main,
        "gap_since_last_seen_chance": gap_chance,
        "top5": {
            "dates_reliable": dates_reliable,
            "all_time": {"draws_counted": n_all, "top5": top5_all_time},
            "last_year": {"draws_counted": n_year, "top5": top5_year},
            "last_quarter": {"draws_counted": n_quarter, "top5": top5_quarter},
            "last_month": {"draws_counted": n_month, "top5": top5_month},
        },
        "disclaimer": (
            "Statistiques descriptives sur l'historique passe des tirages. "
            "Le Loto est un jeu de hasard : chaque tirage est independant et "
            "equiprobable. Ces chiffres ne predisent en rien les tirages a venir."
        ),
    }


def main():
    print(f"Téléchargement de l'archive : {FDJ_ARCHIVE_URL}")
    try:
        zip_bytes = download_archive(FDJ_ARCHIVE_URL)
    except Exception as e:
        print(f"Erreur de téléchargement : {e}", file=sys.stderr)
        sys.exit(1)

    csv_text = extract_csv_text(zip_bytes)
    draws = parse_draws(csv_text)

    if not draws:
        print("Aucun tirage valide extrait du CSV.", file=sys.stderr)
        sys.exit(1)

    stats = compute_stats(draws)

    import os
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"OK : {stats['total_draws_analyzed']} tirages analysés, écrit dans {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
