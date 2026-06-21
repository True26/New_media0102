# -*- coding: utf-8 -*-
"""
actualizador.py
---------------
Complemento del scraper principal. Hace DOS cosas:

  FASE 1 — CATALOGO NUEVO
    Detecta items que NO están en links_recolectados.json y los agrega
    con cover, TMDB ID y video links.

  FASE 2 — REFRESH DE LINKS
    Para items YA registrados, re-visita la página y fusiona los links
    de reproducción nuevos con los existentes (sin borrar los viejos).
    Solo procesa items cuyo último refresh fue hace más de REFRESH_DAYS días.

Uso:
    python actualizador.py                 -> ambas fases
    python actualizador.py --solo-nuevo    -> solo catálogo nuevo
    python actualizador.py --solo-links    -> solo refresca links
"""

import json
import math
import re
import time
import argparse
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import (BASE_URL, SELECTORS, CATALOG, HEADLESS, TIMEOUT,
                    DELAY_BETWEEN, TMDB_API_KEY, TMDB_SEARCH, TMDB_LANG)

OUTPUT_FILE  = "links_recolectados.json"
REFRESH_DAYS = 7      # refresca links de items sin actualizar en X días
BATCH_SIZE   = 200    # items a refrescar por corrida (0 = todos)
SAVE_EVERY   = 25     # guardar cada N items procesados


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_json(data, path=OUTPUT_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── Helpers ───────────────────────────────────────────────────────────────────

def abs_url(href):
    if not href:
        return ""
    return href if href.startswith("http") else BASE_URL.rstrip("/") + "/" + href.lstrip("/")

def extract_bg_url(tag):
    if not tag:
        return ""
    m = re.search(r"url\(['\"]?(.*?)['\"]?\)", tag.get("style", ""))
    return m.group(1) if m else ""

def parse_title_year(raw_title):
    m = re.search(r"\((\d{4})\)\s*$", raw_title)
    if m:
        return raw_title[:m.start()].strip(), int(m.group(1))
    return raw_title.strip(), None

def get_total_pages(soup):
    el = soup.select_one(".page__title")
    if el:
        m = re.search(r"of(\d+)results", el.get_text().replace(" ", ""))
        if m:
            return math.ceil(int(m.group(1)) / 24)
    return None

def merge_links(existing, new):
    """Fusiona listas de dicts {server, embed?, stream_m3u8?} sin duplicar por server."""
    existing_servers = {e.get("server") for e in existing}
    merged = list(existing)
    for entry in new:
        if entry.get("server") not in existing_servers:
            merged.append(entry)
            existing_servers.add(entry.get("server"))
        else:
            # Si el servidor ya existe, actualizar/agregar campos faltantes
            for ex in merged:
                if ex.get("server") == entry.get("server"):
                    if "stream_m3u8" in entry and "stream_m3u8" not in ex:
                        ex["stream_m3u8"] = entry["stream_m3u8"]
                    if "embed" in entry and "embed" not in ex:
                        ex["embed"] = entry["embed"]
                    break
    return merged


# ── TMDB ──────────────────────────────────────────────────────────────────────

def tmdb_lookup(raw_title, section):
    title, year = parse_title_year(raw_title)
    prefer_type = "movie" if section == "peliculas" else "tv"

    params = {"api_key": TMDB_API_KEY, "query": title, "language": TMDB_LANG}
    if year:
        params["year"] = year

    def search(p):
        try:
            return requests.get(TMDB_SEARCH, params=p, timeout=10).json().get("results", [])
        except Exception:
            return []

    results = search(params)
    if not results:
        params.pop("year", None)
        results = search(params)
    if not results:
        return None, None

    for r in results:
        date  = r.get("release_date") or r.get("first_air_date") or ""
        r_year = int(date[:4]) if len(date) >= 4 else None
        if r.get("media_type") == prefer_type and r_year == year:
            return r["id"], r["media_type"]
    for r in results:
        if r.get("media_type") == prefer_type:
            return r["id"], r["media_type"]
    first = results[0]
    return first["id"], first.get("media_type", "movie")


# ── Browser ───────────────────────────────────────────────────────────────────

def get_context(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="es-ES",
        timezone_id="America/Bogota",
    )
    return browser, context


# ── Recolección del catálogo completo ────────────────────────────────────────

def collect_site_links(page):
    all_urls = {}

    for section, max_pages_cfg in CATALOG.items():
        section_url = f"{BASE_URL}/{section}/"
        print(f"\n  [{section.upper()}] escaneando...")

        try:
            page.goto(section_url, wait_until="networkidle", timeout=TIMEOUT)
            time.sleep(1)
        except PWTimeout:
            continue

        def parse_page(html):
            soup = BeautifulSoup(html, "html.parser")
            for article in soup.select(SELECTORS["cards"]):
                link_tag  = article.select_one(SELECTORS["card_link"])
                title_tag = article.select_one(SELECTORS["card_title"])
                img_tag   = article.select_one(SELECTORS["card_cover"])
                href  = abs_url(link_tag.get("href", "") if link_tag else "")
                title = title_tag.get_text(strip=True) if title_tag else ""
                cover = ""
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    if src and "logo" not in src.lower():
                        cover = abs_url(src)
                if href and href != BASE_URL:
                    all_urls[href] = {"title": title, "cover_thumb": cover, "section": section}

        soup_first = BeautifulSoup(page.content(), "html.parser")
        total_pages = get_total_pages(soup_first) or max_pages_cfg
        parse_page(page.content())

        for p in range(2, total_pages + 1):
            try:
                page.goto(f"{BASE_URL}/{section}/{p}", wait_until="networkidle", timeout=TIMEOUT)
                time.sleep(DELAY_BETWEEN)
                parse_page(page.content())
            except PWTimeout:
                continue
            if p % 50 == 0:
                print(f"    pag {p}/{total_pages}")

        print(f"    {section}: done")

    return all_urls


# ── Scraping de detalle ───────────────────────────────────────────────────────

def _get_server_names(page):
    names = []
    for li in page.query_selector_all(".subselect li[data-server]"):
        spans = li.query_selector_all("span")
        name = spans[0].inner_text().strip() if spans else li.inner_text().strip()
        names.append(name)
    return names


def scrape_detail(page, url):
    cover = ""
    player_hits = []
    stream_hits = []

    def on_req(req):
        u = req.url
        if f"{BASE_URL}/player/" in u and u not in player_hits:
            player_hits.append(u)
        elif re.search(r"\.m3u8|\.mp4", u, re.I) and u not in stream_hits:
            stream_hits.append(u)

    _SKIP = ("disqus", "googleapis", "gstatic", "facebook", "twitter",
             "amazon-adsystem", "doubleclick", "cloudflare", "amung.us",
             "rtmark", "jnbhi", "jcphi", "play.png", "jwplayer.js",
             "vast.js", ".css", "translations/")
    _KEEP = (r"\.m3u8", r"\.mp4", r"/player/")

    all_hits = []

    def on_req(req):
        u = req.url
        if any(s in u for s in _SKIP):
            return
        if any(re.search(p, u, re.I) for p in _KEEP) and u not in all_hits:
            all_hits.append(u)

    def build_entry(name, hits):
        entry = {"server": name}
        for u in hits:
            if re.search(r"\.m3u8", u, re.I) and "stream_m3u8" not in entry:
                entry["stream_m3u8"] = u
            elif f"{BASE_URL}/player/" in u and "embed" not in entry:
                entry["embed"] = u
        return entry if len(entry) > 1 else None

    page.on("request", on_req)
    try:
        page.goto(url, wait_until="networkidle", timeout=TIMEOUT)
        page.wait_for_timeout(1500)
    except PWTimeout:
        page.remove_listener("request", on_req)
        return cover, []

    soup = BeautifulSoup(page.content(), "html.parser")
    bg_tag = soup.select_one(SELECTORS["detail_cover"])
    cover  = extract_bg_url(bg_tag)
    if not cover:
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and "logo" not in src.lower():
                cover = abs_url(src)
                break

    server_names = _get_server_names(page)
    server_lis   = page.query_selector_all(".subselect li[data-server]")
    video_links  = []

    try:
        btn = page.query_selector(".pab, .video-html.playrn, #player-tr")
        if btn:
            btn.click()
            page.wait_for_timeout(8000)
    except Exception:
        pass

    entry1 = build_entry(server_names[0] if server_names else "Servidor 1", list(all_hits))
    if entry1:
        video_links.append(entry1)

    dropdown_btn = page.query_selector(".bg-tabs button, .tabs button.button")
    for i, li in enumerate(server_lis[1:], 1):
        prev = set(all_hits)
        try:
            if dropdown_btn:
                dropdown_btn.click()
                page.wait_for_timeout(500)
            li.click(force=True)
            page.wait_for_timeout(6000)
        except Exception:
            continue
        new_hits = [u for u in all_hits if u not in prev]
        name  = server_names[i] if i < len(server_names) else f"Servidor {i+1}"
        entry = build_entry(name, new_hits)
        if entry:
            video_links.append(entry)

    page.remove_listener("request", on_req)
    return cover, video_links


# ── FASE 1: Agregar catálogo nuevo ────────────────────────────────────────────

def fase_catalogo_nuevo(page, results):
    print("\n" + "="*60)
    print("FASE 1 — CATALOGO NUEVO")
    print("="*60)

    done_urls = {r["url"] for r in results}
    site_links = collect_site_links(page)
    nuevos = {url: meta for url, meta in site_links.items() if url not in done_urls}

    if not nuevos:
        print("\nCatalogo al dia. No hay items nuevos.")
        return results

    print(f"\n{len(nuevos)} items nuevos encontrados.")
    nuevos_list = list(nuevos.items())
    agregados = 0

    for i, (url, meta) in enumerate(nuevos_list, 1):
        label = meta["title"] or url
        print(f"  [{i}/{len(nuevos_list)}] {label}")

        cover, video_links = scrape_detail(page, url)
        if not cover and meta.get("cover_thumb"):
            cover = meta["cover_thumb"]

        tmdb_id, tmdb_type = tmdb_lookup(meta["title"], meta["section"])
        print(f"    TMDB: {tmdb_type} id={tmdb_id} | Links: {len(video_links)}")

        results.append({
            "url":          url,
            "title":        meta["title"],
            "cover":        cover,
            "tmdb_id":      tmdb_id,
            "tmdb_type":    tmdb_type,
            "video_links":  video_links,
            "fecha_scrape": now_str(),
            "fecha_links":  now_str(),
        })
        agregados += 1

        if agregados % SAVE_EVERY == 0:
            save_json(results)
            print(f"    >> Guardado parcial ({len(results)} total)")

        time.sleep(DELAY_BETWEEN)

    save_json(results)
    print(f"\nFase 1 completada. {agregados} items nuevos agregados.")
    return results


# ── FASE 2: Refresh de links ──────────────────────────────────────────────────

def fase_refresh_links(page, results):
    print("\n" + "="*60)
    print("FASE 2 — REFRESH DE LINKS")
    print("="*60)

    cutoff = datetime.now() - timedelta(days=REFRESH_DAYS)
    pendientes = []

    for item in results:
        fecha_str = item.get("fecha_links", "")
        if not fecha_str:
            pendientes.append(item)
            continue
        try:
            if datetime.strptime(fecha_str, "%Y-%m-%d %H:%M") < cutoff:
                pendientes.append(item)
        except ValueError:
            pendientes.append(item)

    if not pendientes:
        print(f"Todos los links actualizados en los ultimos {REFRESH_DAYS} dias.")
        return results

    if BATCH_SIZE > 0:
        pendientes = pendientes[:BATCH_SIZE]

    print(f"{len(pendientes)} items para refrescar (>{REFRESH_DAYS} dias sin actualizar).")
    actualizados = 0
    links_nuevos_total = 0

    for i, item in enumerate(pendientes, 1):
        label = item.get("title") or item["url"]
        print(f"  [{i}/{len(pendientes)}] {label}")

        _, nuevos_links = scrape_detail(page, item["url"])
        antes   = len(item.get("video_links", []))
        item["video_links"] = merge_links(item.get("video_links", []), nuevos_links)
        item["fecha_links"] = now_str()
        despues = len(item["video_links"])

        diff = despues - antes
        links_nuevos_total += diff
        actualizados += 1
        print(f"    Links: {antes} -> {despues}  ({'+' + str(diff) if diff > 0 else 'sin cambios'})")

        if actualizados % SAVE_EVERY == 0:
            save_json(results)

        time.sleep(DELAY_BETWEEN)

    save_json(results)
    print(f"\nFase 2 completada. {actualizados} items refrescados, {links_nuevos_total} links nuevos.")
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--solo-nuevo",  action="store_true")
    parser.add_argument("--solo-links",  action="store_true")
    args = parser.parse_args()

    hacer_nuevo = not args.solo_links
    hacer_links = not args.solo_nuevo

    results = load_json(OUTPUT_FILE) or []
    print(f"Items existentes: {len(results)}")

    with sync_playwright() as pw:
        browser, context = get_context(pw)
        page = context.new_page()

        if hacer_nuevo:
            results = fase_catalogo_nuevo(page, results)
        if hacer_links:
            results = fase_refresh_links(page, results)

        browser.close()

    print(f"\nActualizador finalizado. Total en catalogo: {len(results)} items.")


if __name__ == "__main__":
    main()
