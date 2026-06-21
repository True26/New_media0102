# -*- coding: utf-8 -*-
import json
import math
import re
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import (BASE_URL, SELECTORS, CATALOG, HEADLESS, TIMEOUT,
                    DELAY_BETWEEN, MAX_PAGES, MAX_ITEMS,
                    TMDB_API_KEY, TMDB_SEARCH, TMDB_LANG)

# ─────────────────────────────────────────────────────────────────────────────
# Estructura de cada item en links_recolectados.json:
# {
#   "url":        URL de la página en el sitio,
#   "title":      Título tal como aparece en el sitio,
#   "cover":      URL de la carátula HD,
#   "tmdb_id":    ID en TMDB (para obtener descripción, géneros, rating),
#   "tmdb_type":  "movie" | "tv",
#   "video_links": [ lista de links de reproducción ],
#   "fecha_scrape": "YYYY-MM-DD HH:MM"
# }
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_FILE = "links_recolectados.json"


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
    from datetime import datetime
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
    """'Avatar: Fuego y ceniza (2025)' -> ('Avatar: Fuego y ceniza', 2025)"""
    m = re.search(r"\((\d{4})\)\s*$", raw_title)
    if m:
        year = int(m.group(1))
        title = raw_title[:m.start()].strip()
        return title, year
    return raw_title.strip(), None

def get_total_pages(soup):
    el = soup.select_one(".page__title")
    if el:
        m = re.search(r"of(\d+)results", el.get_text().replace(" ", ""))
        if m:
            return math.ceil(int(m.group(1)) / 24)
    return None


# ── TMDB lookup ───────────────────────────────────────────────────────────────

def tmdb_lookup(raw_title, section):
    """
    Busca el título en TMDB y devuelve (tmdb_id, tmdb_type).
    Usa el año del título para afinar la búsqueda.
    """
    title, year = parse_title_year(raw_title)

    params = {
        "api_key":  TMDB_API_KEY,
        "query":    title,
        "language": TMDB_LANG,
    }
    if year:
        params["year"] = year

    try:
        resp = requests.get(TMDB_SEARCH, params=params, timeout=10)
        data = resp.json()
    except Exception:
        return None, None

    results = data.get("results", [])
    if not results:
        # Segundo intento sin año por si el título no coincide exactamente
        params.pop("year", None)
        try:
            resp = requests.get(TMDB_SEARCH, params=params, timeout=10)
            results = resp.json().get("results", [])
        except Exception:
            return None, None

    if not results:
        return None, None

    # Preferir el tipo según la sección del sitio
    prefer_type = "movie" if section == "peliculas" else "tv"

    # 1. Buscar coincidencia exacta de año y tipo preferido
    for r in results:
        mtype = r.get("media_type", "")
        date  = r.get("release_date") or r.get("first_air_date") or ""
        r_year = int(date[:4]) if len(date) >= 4 else None
        if mtype == prefer_type and r_year == year:
            return r["id"], mtype

    # 2. Cualquier resultado del tipo preferido
    for r in results:
        if r.get("media_type") == prefer_type:
            return r["id"], r["media_type"]

    # 3. Primer resultado de cualquier tipo
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


# ── Recolección de links del listado ─────────────────────────────────────────

def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for article in soup.select(SELECTORS["cards"]):
        link_tag  = article.select_one(SELECTORS["card_link"])
        title_tag = article.select_one(SELECTORS["card_title"])
        img_tag   = article.select_one(SELECTORS["card_cover"])

        href  = abs_url(link_tag.get("href", "") if link_tag else "")
        title = title_tag.get_text(strip=True) if title_tag else ""
        cover = ""
        if img_tag:
            src = (img_tag.get("src") or img_tag.get("data-src") or
                   img_tag.get("data-lazy-src") or "")
            if src and "logo" not in src.lower():
                cover = abs_url(src)

        if href and href != BASE_URL:
            items.append({"url": href, "title": title, "cover_thumb": cover})

    return soup, items


PHASE1_FILE = "fase1_progreso.json"


def collect_all_links(page):
    """
    Recorre todas las secciones paginadas.
    Guarda progreso en fase1_progreso.json después de cada página
    para que si se cancela pueda continuar desde donde quedó.
    """
    # Cargar progreso previo de fase 1
    progreso = load_json(PHASE1_FILE) or {"done_sections": [], "items": []}
    all_items  = progreso["items"]
    seen_urls  = {it["url"] for it in all_items}
    done_sects = set(progreso["done_sections"])

    for section, max_pages_cfg in CATALOG.items():
        if section in done_sects:
            print(f"\n  [{section.upper()}] ya completado ({sum(1 for it in all_items if it.get('section')==section)} items) — saltando")
            continue

        section_url = f"{BASE_URL}/{section}/"
        print(f"\n  [{section.upper()}] escaneando...")

        try:
            page.goto(section_url, wait_until="networkidle", timeout=TIMEOUT)
            time.sleep(1)
        except PWTimeout:
            print(f"    Timeout en {section_url}, saltando.")
            continue

        soup, items = parse_listing(page.content())
        total_pages = get_total_pages(soup) or max_pages_cfg
        if MAX_PAGES > 0:
            total_pages = min(total_pages, MAX_PAGES)

        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_items.append({**item, "section": section})

        # Guardar después de la página 1
        save_json({"done_sections": list(done_sects), "items": all_items}, PHASE1_FILE)

        for p in range(2, total_pages + 1):
            url = f"{BASE_URL}/{section}/{p}"
            try:
                page.goto(url, wait_until="networkidle", timeout=TIMEOUT)
                time.sleep(DELAY_BETWEEN)
            except PWTimeout:
                continue

            _, items = parse_listing(page.content())
            for item in items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_items.append({**item, "section": section})

            if not items:
                break

            # Guardar progreso después de cada página
            save_json({"done_sections": list(done_sects), "items": all_items}, PHASE1_FILE)

            if p % 25 == 0:
                print(f"    pag {p}/{total_pages} — {len(all_items)} links acumulados")

        done_sects.add(section)
        save_json({"done_sections": list(done_sects), "items": all_items}, PHASE1_FILE)
        print(f"    {section}: done ({sum(1 for it in all_items if it.get('section')==section)} items)")

    return all_items


# ── Scraping de detalle: cover + video links ──────────────────────────────────

def _get_server_names(page):
    """Devuelve lista de nombres de servidores del selector."""
    names = []
    for li in page.query_selector_all(".subselect li[data-server]"):
        spans = li.query_selector_all("span")
        name = spans[0].inner_text().strip() if spans else li.inner_text().strip()
        names.append(name)
    return names


_SKIP = ("disqus", "googleapis", "gstatic", "facebook", "twitter",
         "amazon-adsystem", "doubleclick", "cloudflare", "amung.us",
         "rtmark", "jnbhi", "jcphi", "play.png", "jwplayer.js",
         "vast.js", ".css", "translations/")

_KEEP = (r"\.m3u8", r"\.mp4", r"/player/")


def _is_useful(url):
    if any(s in url for s in _SKIP):
        return False
    return any(re.search(p, url, re.I) for p in _KEEP)


def _build_entry(name, raw_hits):
    """Construye un dict {server, embed?, stream_m3u8?} filtrando las URLs útiles."""
    entry = {"server": name}
    for u in raw_hits:
        if re.search(r"\.m3u8", u, re.I) and "stream_m3u8" not in entry:
            entry["stream_m3u8"] = u
        elif f"{BASE_URL}/player/" in u and "embed" not in entry:
            entry["embed"] = u
    return entry if len(entry) > 1 else None


def scrape_detail(page, url):
    """
    Devuelve cover (str) y video_links (list of dicts).
    Cada dict: {server, embed?, stream_m3u8?}
    """
    cover    = ""
    all_hits = []          # acumula TODAS las requests útiles

    def on_req(req):
        u = req.url
        if _is_useful(u) and u not in all_hits:
            all_hits.append(u)

    page.on("request", on_req)

    try:
        page.goto(url, wait_until="networkidle", timeout=TIMEOUT)
        page.wait_for_timeout(1500)
    except PWTimeout:
        page.remove_listener("request", on_req)
        return cover, []

    # ── Carátula ──
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

    # ── Click play → servidor 1 carga + m3u8 del iframe ──
    try:
        btn = page.query_selector(".pab, .video-html.playrn, #player-tr")
        if btn:
            btn.click()
            page.wait_for_timeout(8000)   # necesario para que el iframe cargue el m3u8
    except Exception:
        pass

    hits_s1 = list(all_hits)
    name_s1 = server_names[0] if server_names else "Servidor 1"
    entry1  = _build_entry(name_s1, hits_s1)
    video_links = [entry1] if entry1 else []

    # ── Servidores adicionales: abrir dropdown → click ──
    dropdown_btn = page.query_selector(".bg-tabs button, .tabs button.button")
    for i, li in enumerate(server_lis[1:], 1):
        prev_hits = set(all_hits)
        try:
            # Abrir dropdown si está cerrado
            if dropdown_btn:
                dropdown_btn.click()
                page.wait_for_timeout(500)
            # Click en el servidor (force=True para saltear visibilidad)
            li.click(force=True)
            page.wait_for_timeout(6000)
        except Exception:
            continue

        new_hits = [u for u in all_hits if u not in prev_hits]
        name  = server_names[i] if i < len(server_names) else f"Servidor {i+1}"
        entry = _build_entry(name, new_hits)
        if entry:
            video_links.append(entry)

    page.remove_listener("request", on_req)
    return cover, video_links


# ── Scraping de series: cover + episodios con sus video links ─────────────────

def _collect_episodes_from_page(page, seen_urls):
    """Lee el HTML actual y extrae links de episodios no vistos antes."""
    eps = []
    soup = BeautifulSoup(page.content(), "html.parser")
    for a in soup.select("a[href*='/episode/']"):
        href = abs_url(a.get("href", ""))
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)
        label = a.get_text(strip=True)
        m = re.search(r"/season/(\d+)/episode/(\d+)", href)
        season_num = int(m.group(1)) if m else 1
        ep_num     = int(m.group(2)) if m else 0
        eps.append({"url": href, "label": label, "season": season_num, "episode": ep_num})
    return eps


def scrape_series(page, series_url):
    """
    Visita la página principal de una serie/anime/dorama.
    Devuelve (cover, episodes) donde episodes es lista de dicts:
      {label, url, season, episode, video_links}
    """
    cover = ""

    try:
        page.goto(series_url, wait_until="networkidle", timeout=TIMEOUT)
        page.wait_for_timeout(1500)
    except PWTimeout:
        return cover, []

    # Carátula desde la página principal de la serie
    soup = BeautifulSoup(page.content(), "html.parser")
    bg_tag = soup.select_one(SELECTORS["detail_cover"])
    cover  = extract_bg_url(bg_tag)
    if not cover:
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and "logo" not in src.lower():
                cover = abs_url(src)
                break

    # Recolectar episodios de la temporada inicial
    seen_ep_urls    = set()
    all_episode_meta = _collect_episodes_from_page(page, seen_ep_urls)

    # Hacer click en cada temporada adicional para cargar sus episodios
    season_btns = page.query_selector_all(".seasons .season, .season")
    for btn in season_btns[1:]:
        try:
            btn.click()
            page.wait_for_timeout(1500)
            all_episode_meta.extend(_collect_episodes_from_page(page, seen_ep_urls))
        except Exception:
            pass

    if not all_episode_meta:
        # Fallback: si no hay episodios detectados, tratar como película
        _, video_links = scrape_detail(page, series_url)
        return cover, video_links

    # Ordenar: temporada asc, episodio asc
    all_episode_meta.sort(key=lambda x: (x["season"], x["episode"]))

    # Scrapear cada episodio
    episodes = []
    for ep in all_episode_meta:
        print(f"      {ep['label']}")
        _, video_links = scrape_detail(page, ep["url"])
        episodes.append({
            "label":       ep["label"],
            "url":         ep["url"],
            "season":      ep["season"],
            "episode":     ep["episode"],
            "video_links": video_links,
        })
        time.sleep(DELAY_BETWEEN)

    return cover, episodes


# ── Main ──────────────────────────────────────────────────────────────────────

# Secciones que tienen episodios en vez de video directo
_SERIES_SECTIONS = {"series", "animes", "doramas"}


def run():
    import os

    # Cargar resultados ya completados
    results   = load_json(OUTPUT_FILE) or []
    done_urls = {item["url"] for item in results}
    print(f"Items ya completos: {len(results)}")

    # Cargar checkpoint de paginacion
    progreso    = load_json(PHASE1_FILE) or {"done_sections": []}
    done_sects  = set(progreso["done_sections"])

    with sync_playwright() as pw:
        browser, context = get_context(pw)
        page = context.new_page()

        for section, max_pages_cfg in CATALOG.items():
            if section in done_sects:
                count = sum(1 for r in results if r.get("section") == section)
                print(f"\n[{section.upper()}] ya completado ({count} items) — saltando")
                continue

            is_series = section in _SERIES_SECTIONS
            section_url = f"{BASE_URL}/{section}/"
            print(f"\n[{section.upper()}] escaneando y scrapeando {'(con episodios)' if is_series else ''}...")

            try:
                page.goto(section_url, wait_until="networkidle", timeout=TIMEOUT)
                time.sleep(1)
            except PWTimeout:
                print(f"  Timeout en {section_url}, saltando.")
                continue

            soup, page1_items = parse_listing(page.content())
            total_pages = get_total_pages(soup) or max_pages_cfg
            if MAX_PAGES > 0:
                total_pages = min(total_pages, MAX_PAGES)

            all_pages_items = [page1_items]

            # Recolectar URLs de todas las páginas del listado primero
            for p in range(2, total_pages + 1):
                try:
                    page.goto(f"{BASE_URL}/{section}/{p}", wait_until="networkidle", timeout=TIMEOUT)
                    time.sleep(DELAY_BETWEEN)
                    _, items = parse_listing(page.content())
                    all_pages_items.append(items)
                    if not items:
                        break
                    if p % 25 == 0:
                        print(f"  pag {p}/{total_pages} recolectada...")
                except PWTimeout:
                    all_pages_items.append([])

            # Scrapear cada URL descubierta en esta sección
            section_done = 0
            section_new  = 0

            for page_items in all_pages_items:
                for basic in page_items:
                    if basic["url"] in done_urls:
                        section_done += 1
                        continue

                    section_new  += 1
                    global_count  = len(results) + 1
                    label = basic["title"] or basic["url"]
                    print(f"  [{global_count}] {label}")

                    tmdb_id, tmdb_type = tmdb_lookup(basic["title"], section)

                    if is_series:
                        cover, episodes = scrape_series(page, basic["url"])
                        if not cover and basic.get("cover_thumb"):
                            cover = basic["cover_thumb"]
                        ep_count = len(episodes) if isinstance(episodes, list) else 0
                        print(f"    TMDB: {tmdb_type} id={tmdb_id} | Episodios: {ep_count}")
                        item = {
                            "url":          basic["url"],
                            "title":        basic["title"],
                            "cover":        cover,
                            "tmdb_id":      tmdb_id,
                            "tmdb_type":    tmdb_type,
                            "section":      section,
                            "episodes":     episodes if isinstance(episodes, list) else [],
                            "fecha_scrape": now_str(),
                        }
                    else:
                        cover, video_links = scrape_detail(page, basic["url"])
                        if not cover and basic.get("cover_thumb"):
                            cover = basic["cover_thumb"]
                        print(f"    TMDB: {tmdb_type} id={tmdb_id} | Links: {len(video_links)}")
                        item = {
                            "url":          basic["url"],
                            "title":        basic["title"],
                            "cover":        cover,
                            "tmdb_id":      tmdb_id,
                            "tmdb_type":    tmdb_type,
                            "section":      section,
                            "video_links":  video_links,
                            "fecha_scrape": now_str(),
                        }

                    results.append(item)
                    done_urls.add(basic["url"])

                    # Guardar después de cada item — nunca se pierde nada
                    save_json(results)

                    time.sleep(DELAY_BETWEEN)

            # Sección completada
            done_sects.add(section)
            save_json({"done_sections": list(done_sects)}, PHASE1_FILE)
            print(f"  {section} completado: {section_new} nuevos, {section_done} ya existentes")

        browser.close()

    # Limpiar checkpoint al terminar todo el catálogo
    if os.path.exists(PHASE1_FILE):
        os.remove(PHASE1_FILE)

    print(f"\nScraping completado. {len(results)} items en {OUTPUT_FILE}")
    return results


if __name__ == "__main__":
    run()
