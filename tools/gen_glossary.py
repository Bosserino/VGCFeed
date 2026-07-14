#!/usr/bin/env python3
"""Genera glossary_ja.json: glossario giapponese -> inglese dei termini Pokémon.

Scarica i CSV ufficiali di PokeAPI (repo PokeAPI/pokeapi, data/v2/csv/) e
costruisce una mappa nome_giapponese -> nome_inglese per specie, mosse,
abilità, strumenti e nature. Le chiavi giapponesi includono sia la forma
katakana/hiragana (local_language_id 1, ja-Hrkt) sia quella kanji
(local_language_id 11, ja); il valore è il nome inglese (local_language_id 9).

In caso di collisione (stesso termine JP con inglesi diversi tra categorie)
vince la priorità: specie > mossa > abilità > item > natura.

Uso:  python3 tools/gen_glossary.py
Output: glossary_ja.json nella root del repo (chiavi ordinate per lunghezza
decrescente, così chi lo usa può fare replace longest-first iterando in ordine).
"""

import csv
import io
import json
import os
import sys

import requests

# URL base dei CSV raw su GitHub
BASE_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"

# File CSV da scaricare, in ordine di priorità decrescente
# (specie > mossa > abilità > item > natura)
SOURCES = [
    ("specie", "pokemon_species_names.csv"),
    ("mossa", "move_names.csv"),
    ("abilita", "ability_names.csv"),
    ("item", "item_names.csv"),
    ("natura", "nature_names.csv"),
]

# id lingua nei CSV di PokeAPI
LANG_JA_HRKT = "1"   # giapponese katakana/hiragana
LANG_EN = "9"        # inglese
LANG_JA_KANJI = "11" # giapponese kanji

# Percorso di output: root del repo (cartella padre di tools/)
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "glossary_ja.json")


def scarica_csv(nome_file):
    """Scarica un CSV da GitHub raw e ritorna le righe come lista di dict."""
    url = BASE_URL + nome_file
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    # I CSV di PokeAPI sono in UTF-8
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))


def estrai_mappa(righe):
    """Da righe (<id>, local_language_id, name, ...) costruisce ja -> en.

    Raggruppa per id entità, poi per ogni entità con nome inglese associa
    entrambe le forme giapponesi (kana e kanji) al nome inglese.
    """
    # La prima colonna è l'id dell'entità (nome diverso per ogni CSV:
    # pokemon_species_id, move_id, ecc.) -> la individuiamo dinamicamente
    per_id = {}
    for riga in righe:
        chiavi = list(riga.keys())
        id_entita = riga[chiavi[0]]
        lang = riga["local_language_id"]
        nome = riga["name"].strip()
        if not nome:
            continue
        per_id.setdefault(id_entita, {})[lang] = nome

    mappa = {}
    for nomi in per_id.values():
        en = nomi.get(LANG_EN)
        if not en:
            continue  # senza nome inglese non possiamo tradurre
        for lang_ja in (LANG_JA_HRKT, LANG_JA_KANJI):
            ja = nomi.get(lang_ja)
            if ja:
                mappa[ja] = en
    return mappa


def main():
    glossario = {}
    collisioni = 0

    for categoria, nome_file in SOURCES:
        righe = scarica_csv(nome_file)
        mappa = estrai_mappa(righe)
        aggiunte = 0
        for ja, en in mappa.items():
            if ja in glossario:
                if glossario[ja] != en:
                    # Collisione tra categorie: vince chi è arrivato prima
                    # (le categorie sono processate in ordine di priorità)
                    collisioni += 1
                    print(
                        "AVVISO collisione: '%s' -> '%s' (%s) scartato, "
                        "tengo '%s'" % (ja, en, categoria, glossario[ja]),
                        file=sys.stderr,
                    )
                continue
            glossario[ja] = en
            aggiunte += 1
        print("%s: %d voci aggiunte da %s" % (categoria, aggiunte, nome_file))

    # Ordina le chiavi per lunghezza decrescente (a parità, alfabetico per
    # avere output deterministico): chi usa il glossario può iterare in
    # ordine e fare replace longest-first senza riordinare.
    ordinato = {k: glossario[k] for k in sorted(glossario, key=lambda s: (-len(s), s))}

    out_path = os.path.normpath(OUT_PATH)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ordinato, f, ensure_ascii=False, indent=0)
        f.write("\n")

    print("Totale voci: %d" % len(ordinato))
    print("Collisioni rilevate: %d" % collisioni)
    print("Scritto: %s" % out_path)


if __name__ == "__main__":
    main()
