# scripts/post_shop.py
import os
import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import fortnite_api
import tweepy

# ------------------ Helpers ------------------

def normalize_rarity(r):
    if r is None:
        return "common"
    for attr in ("value", "api_value", "apiValue", "name"):
        v = getattr(r, attr, None)
        if v:
            s = str(v).lower()
            break
    else:
        s = str(r).lower()
    if "." in s:
        s = s.split(".")[-1]
    for k in ("common", "uncommon", "rare", "epic", "legendary", "mythic"):
        if k in s:
            return k
    return s

def clean_url(u):
    if not u:
        return None
    if isinstance(u, dict):
        u = (u.get("url") or u.get("icon") or next(iter(u.values()), None))
    if hasattr(u, "url"):
        u = u.url
    u = str(u).strip().strip("'").strip('"')
    if u.lower().startswith("asset url=") or u.lower().startswith("asset_url="):
        u = u.split("=", 1)[1].strip().strip("'").strip('"')
    if u.startswith("//"):
        u = "https:" + u
    if not (u.startswith("http://") or u.startswith("https://")):
        return None
    return u

RARITY_EMOJI = {
    "common": "⚪ Común",
    "uncommon": "🟢 Poco común",
    "rare": "🔵 Raro",
    "epic": "🟣 Épico",
    "legendary": "🟠 Legendario",
    "mythic": "🔴 Mítico",
}

def make_line(item):
    nombre = item.get("name", "???")
    rare_leg = RARITY_EMOJI.get((item.get("rarity") or "").lower(), "?")
    price = str(item.get("price") or "?")
    if price.isdigit():
        price += " V-Bucks"
    salida = item.get("expires", "Próxima rotación")
    return f"• {nombre} ({rare_leg}) - {price} | Sale: {salida}"

def chunk_lines_into_tweets(header, lines, footer, max_chars=270):
    tweets = []
    base1 = header.rstrip() + "\n\n" + footer.rstrip()
    tweets.append(base1[:max_chars])
    current = ""
    for line in lines:
        add = line + "\n"
        if len(current) + len(add) > max_chars:
            tweets.append(current.rstrip())
            current = add
        else:
            current += add
    if current.strip():
        tweets.append(current.rstrip())
    return tweets

# ----------- tipo, sección, grupo, serie -----------

TYPE_MAP = {
    "outfit": "traje",
    "emote": "gesto",
    "emoji": "gesto",
    "backpack": "mochila",
    "backbling": "mochila",
    "pickaxe": "pico",
    "glider": "ala_delta",
    "wrap": "envoltorio",
    "pet": "companero",
    "petcarrier": "companero",
    "companion": "companero",
    "music": "pista",
    "musicpack": "pista",
    "jam": "pista",
    "jamtrack": "pista",
    "festival_track": "pista",
    "loading_screen": "pantalla_carga",
}

def map_api_type(v: str) -> str | None:
    if not v:
        return None
    t = str(v).lower().replace("cosmetictype.", "")
    t = t.replace("athenacharacter", "outfit").replace("athenabackpack", "backpack")
    t = t.replace("weaponwrap", "wrap").replace("musicpack", "music")
    t = t.replace("loading", "loading_screen")
    return TYPE_MAP.get(t) or TYPE_MAP.get(t.split(".")[-1])

def infer_type_by_name(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ["compañero","companero","companion","buddy","pet"]): return "companero"
    if any(k in n for k in ["pista","jam","track","music","música","musica"]):  return "pista"
    if any(k in n for k in ["gesto","emote","baile","dance"]):                  return "gesto"
    if any(k in n for k in ["pico","hacha","pickaxe"]):                         return "pico"
    if any(k in n for k in ["ala delta","ala","planeador","glider"]):           return "ala_delta"
    if any(k in n for k in ["envoltorio","wrap","camo"]):                       return "envoltorio"
    if any(k in n for k in ["mochila","back bling","back","accesorio"]):        return "mochila"
    return "traje"

def from_series(obj):
    if not obj: return None
    try:
        v = getattr(obj, "value", None) or getattr(obj, "name", None)
        if v: return str(v)
    except Exception:
        pass
    if isinstance(obj, dict):
        return obj.get("value") or obj.get("name")
    return None

def human_section(key: str | None) -> str | None:
    if not key: return None
    return {
        "featured": "Featured",
        "specialFeatured": "Special Featured",
        "specialDaily": "Special Daily",
        "daily": "Daily",
        "votes": "Votes",
        "voteWinners": "Vote Winners",
    }.get(key, key)

def safe_id(value_hint):
    return hashlib.sha1(str(value_hint).encode("utf-8")).hexdigest()[:16]

# ------------------ Facebook ------------------

def fb_upload_unpublished_photo(page_id, page_token, image_url, caption=None, timeout=120):
    url = f"https://graph.facebook.com/v24.0/{page_id}/photos"
    data = {"published": "false", "url": image_url, "access_token": page_token}
    if caption:
        data["caption"] = caption
    r = requests.post(url, data=data, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"FB upload photo failed: {r.status_code} {r.text}")
    return r.json()["id"]

def fb_create_multiimage_post(page_id, page_token, message, media_fbids, timeout=120):
    url = f"https://graph.facebook.com/v24.0/{page_id}/feed"
    data = {"message": message, "access_token": page_token}
    for i, fid in enumerate(media_fbids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": str(fid)})
    r = requests.post(url, data=data, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"FB create post failed: {r.status_code} {r.text}")
    return r.json()["id"]

def fb_build_message_for_shop(fecha_larga_fb, items, max_headlines=6):
    nombres = [it.get("name", "???") for it in items[:max_headlines]]
    idx = " · ".join(nombres)
    return (
        f"🛒 Tienda de Fortnite - {fecha_larga_fb}\n\n"
        f"{idx}\n\n"
        "Precios en V-Bucks, rareza y rotación diaria aquí mismo todos los días. 🎮🔥"
    )

def post_multi_image_facebook(page_id, page_token, items, base_message, per_image_caption=True, max_images=40):
    media_fbids = []
    for count, it in enumerate(items, start=1):
        if count > max_images:
            break
        img_url = it.get("img_url")
        if not img_url:
            continue
        cap = None
        if per_image_caption:
            pr = it.get("price", "?")
            if str(pr).isdigit():
                pr = f"{pr} V-Bucks"
            rare = RARITY_EMOJI.get((it.get("rarity") or "").lower(), it.get("rarity", "?"))
            cap = f"{it.get('name','???')} — {pr} {f'({rare})' if rare else ''}".strip()
        fid = fb_upload_unpublished_photo(page_id, page_token, img_url, caption=cap)
        media_fbids.append(fid)
    if not media_fbids:
        raise RuntimeError("No se pudieron subir imágenes para el post multi-imagen.")
    post_id = fb_create_multiimage_post(page_id, page_token, base_message, media_fbids)
    print(f"✅ Publicado en Facebook multi-imagen. post_id={post_id} imágenes={len(media_fbids)}")
    return post_id

# ------------------ Fallback requests ------------------

API_URL = "https://fortnite-api.com/v2/shop/br?language=es-MX"

def session_with_retries(total=3, backoff=0.5):
    s = requests.Session()
    retry = Retry(
        total=total,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# ------------------ Fetch + Agrupación por lote ------------------

def fetch_shop_items(FN_API_KEY):
    """
    Devuelve:
      items  -> lista plana (compat) con groupId/groupPrice
      groups -> dicts por lote: id, name, price, expires, items[]
      shop_date_str
    """
    items = []
    groups = {}
    shop_date_str = None

    # -------- Intento 1: librería ----------
    try:
        with fortnite_api.SyncClient(
            api_key=FN_API_KEY,
            default_language=fortnite_api.GameLanguage.SPANISH_LATIN,
        ) as fn_client:
            shop = fn_client.fetch_shop()
            try:
                if getattr(shop, "date", None):
                    shop_date_str = str(shop.date)
            except Exception:
                pass

            entries = getattr(shop, "entries", []) or []
            for entry in entries:
                entry_price = (
                    getattr(entry, "final_price", None)
                    or getattr(entry, "regular_price", None)
                    or getattr(entry, "price", None)
                )
                if hasattr(entry_price, "final"): entry_price = entry_price.final
                if hasattr(entry_price, "total"): entry_price = entry_price.total
                if entry_price is not None and not isinstance(entry_price, (int, float, str)):
                    entry_price = str(entry_price)

                raw_exp = (
                    getattr(entry, "expiry", None)
                    or getattr(entry, "expires_at", None)
                    or getattr(entry, "expiration", None)
                    or getattr(entry, "offer_expires", None)
                    or getattr(entry, "offer_ends", None)
                    or getattr(entry, "end", None)
                )
                expire_txt = None
                if raw_exp:
                    try: expire_txt = str(raw_exp)
                    except Exception: expire_txt = None
                if expire_txt is None:
                    try:
                        rota = (shop.date + timedelta(days=1)).date()
                        expire_txt = rota.strftime("%d/%m/%Y")
                    except Exception:
                        expire_txt = "Próxima rotación"

                # sección y bundle name (si trae)
                section = None
                sec_obj = getattr(entry, "section", None)
                if sec_obj:
                    section = getattr(sec_obj, "display_name", None) or getattr(sec_obj, "name", None)
                group_name = None
                bundle = getattr(entry, "bundle", None)
                if bundle and hasattr(bundle, "name"):
                    group_name = bundle.name

                # offer id u otra clave para agrupar
                offer_id = getattr(entry, "offer_id", None) or getattr(entry, "offerId", None) or getattr(entry, "id", None)

                # lista de cosméticos
                try:
                    cosmetics = list(entry)
                except TypeError:
                    cosmetics = (
                        getattr(entry, "items", None)
                        or getattr(entry, "br_items", None)
                        or []
                    )

                # recolecta ids para fallback de group_id
                ids_for_key = []
                tmp_items = []
                for itm in cosmetics:
                    itm_id = getattr(itm, "id", None) or getattr(itm, "templateId", None)
                    if itm_id: ids_for_key.append(str(itm_id))

                    url = clean_url(getattr(getattr(itm, "images", None), "icon", None))
                    if not url:
                        continue

                    name = getattr(itm, "name", None) or "Sin nombre"
                    rty  = normalize_rarity(getattr(itm, "rarity", None))
                    ser  = from_series(getattr(itm, "series", None))

                    api_type = None
                    try:
                        t = getattr(itm, "type", None)
                        if t:
                            api_type = map_api_type(getattr(t, "value", None) or getattr(t, "name", None))
                    except Exception:
                        pass
                    typ = api_type or infer_type_by_name(name)

                    tmp_items.append({
                        "id": itm_id or safe_id(name + url),
                        "name": name,
                        "img_url": url,
                        "rarity": rty,
                        "type": typ,
                        "series": ser,
                        "indiv_price": None,  # la librería casi nunca trae precio individual
                    })

                group_id = offer_id or safe_id("|".join(sorted(ids_for_key)) + f"|{entry_price}|{expire_txt}")
                grp = groups.get(group_id)
                if not grp:
                    groups[group_id] = grp = {
                        "id": group_id,
                        "name": group_name,
                        "price": entry_price,
                        "expires": expire_txt,
                        "items": [],
                    }

                for ti in tmp_items:
                    grp["items"].append({
                        "id": ti["id"],
                        "name": ti["name"],
                        "image": ti["img_url"],
                        "rarity": ti["rarity"],
                        "type": ti["type"],
                        "price": ti["indiv_price"],
                    })
                    items.append({
                        "id": ti["id"],
                        "name": ti["name"],
                        "img_url": ti["img_url"],
                        "rarity": ti["rarity"],
                        "price": entry_price,
                        "expires": expire_txt,
                        "type": ti["type"],
                        "section": section,
                        "group": group_name,
                        "groupId": group_id,
                        "groupPrice": entry_price,
                        "series": ti["series"],
                    })

        if items:
            print(f"🛍️ (fortnite_api) {len(items)} artículos.")
            return items, groups, shop_date_str
        else:
            print("⚠️ (fortnite_api) 0 items. Intentando fallback requests…")
    except Exception as e:
        print("⚠️ Error usando fortnite_api, paso a requests:", repr(e))

    # -------- Intento 2: requests ----------
    try:
        s = session_with_retries()
        headers = {"Authorization": FN_API_KEY} if FN_API_KEY else {}
        r = s.get(API_URL, headers=headers, timeout=30)
        print("→ fallback requests status_code:", r.status_code)
        if r.status_code != 200:
            print("Body (primeros 300):", r.text[:300])
            return [], {}, None
        data = r.json()
        shop = data.get("data") or {}
        shop_date_str = shop.get("date")

        for key in ("featured", "specialFeatured", "specialDaily", "daily", "votes", "voteWinners"):
            sec = shop.get(key)
            if not sec:
                continue
            entries = sec.get("entries") or []
            for entry in entries:
                entry_price = entry.get("regularPrice") or entry.get("finalPrice") or entry.get("price")
                expire_txt = entry.get("offerExpires") or entry.get("expiresAt") or "Próxima rotación"
                section = human_section(key)
                bundle = entry.get("bundle") or {}
                group_name = bundle.get("name") or entry.get("category") or entry.get("devName") or None
                offer_id = entry.get("offerId") or entry.get("id")

                cosmetics = entry.get("items") or []
                ids_for_key = []
                tmp_items = []
                for itm in cosmetics:
                    itm_id = itm.get("id") or itm.get("templateId") or itm.get("name")
                    if itm_id: ids_for_key.append(str(itm_id))
                    url = clean_url((itm.get("images") or {}).get("icon"))
                    if not url:
                        continue

                    name = itm.get("name") or "Sin nombre"
                    rty  = normalize_rarity(itm.get("rarity"))
                    ser  = from_series(itm.get("series"))
                    tobj = itm.get("type") or {}
                    api_type = map_api_type(tobj.get("value") or tobj.get("name"))
                    typ = api_type or infer_type_by_name(name)

                    indiv_price = itm.get("finalPrice") or itm.get("regularPrice") or itm.get("price")

                    tmp_items.append({
                        "id": itm_id or safe_id(name + url),
                        "name": name,
                        "img_url": url,
                        "rarity": rty,
                        "type": typ,
                        "series": ser,
                        "indiv_price": indiv_price,
                    })

                group_id = offer_id or safe_id("|".join(sorted(ids_for_key)) + f"|{entry_price}|{expire_txt}")
                grp = groups.get(group_id)
                if not grp:
                    groups[group_id] = grp = {
                        "id": group_id,
                        "name": group_name,
                        "price": entry_price,
                        "expires": expire_txt,
                        "items": [],
                    }

                for ti in tmp_items:
                    grp["items"].append({
                        "id": ti["id"],
                        "name": ti["name"],
                        "image": ti["img_url"],
                        "rarity": ti["rarity"],
                        "type": ti["type"],
                        "price": ti["indiv_price"],  # puede ser None
                    })
                    items.append({
                        "id": ti["id"],
                        "name": ti["name"],
                        "img_url": ti["img_url"],
                        "rarity": ti["rarity"],
                        "price": ti["indiv_price"] if ti["indiv_price"] is not None else entry_price,
                        "expires": expire_txt,
                        "type": ti["type"],
                        "section": section,
                        "group": group_name,
                        "groupId": group_id,
                        "groupPrice": entry_price,
                        "series": ti["series"],
                    })

        print(f"🛍️ (requests) {len(items)} artículos.")
        return items, groups, shop_date_str
    except requests.exceptions.Timeout:
        print("❌ Timeout (30s) en fallback requests.")
        return [], {}, None
    except Exception as e:
        print("❌ Error en fallback requests:", repr(e))
        return [], {}, None

# ------------------ Entorno ------------------

FN_API_KEY = os.getenv("FN_API_KEY")

TW_API_KEY = os.getenv("TW_API_KEY")
TW_API_SECRET = os.getenv("TW_API_SECRET")
TW_ACCESS_TOKEN = os.getenv("TW_ACCESS_TOKEN")
TW_ACCESS_SECRET = os.getenv("TW_ACCESS_SECRET")

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN")
FB_PAGE_URL = os.getenv("FB_PAGE_URL")
FB_MAX_IMAGES = int(os.getenv("FB_MAX_IMAGES", "40"))

WEB_OUT = Path(os.getenv("WEB_OUT", "fortnite"))

# ------------------ Descarga y JSON ------------------

print("Descargando tienda de Fortnite (con fallback y agrupación por lote)…")
if not FN_API_KEY:
    print("⚠️ Aviso: FN_API_KEY no está definido; el endpoint puede fallar.")

items, groups, shop_date_str = fetch_shop_items(FN_API_KEY)
print(f"🛍️ {len(items)} artículos planos. Lotes: {len(groups)}")

WEB_OUT.mkdir(parents=True, exist_ok=True)

# Export plano (compat con tu front actual)
export_items = [{
    "id": it.get("id"),
    "name": it["name"],
    "image": it["img_url"],
    "rarity": it["rarity"],
    "price": it["price"],
    "expires": it["expires"],
    "type": it.get("type") or infer_type_by_name(it["name"]),
    "section": it.get("section"),
    "group": it.get("group"),
    "groupId": it.get("groupId"),
    "groupPrice": it.get("groupPrice"),
    "series": it.get("series"),
} for it in items]

# Export por lotes
export_groups = []
for g in groups.values():
    export_groups.append({
        "id": g["id"],
        "name": g["name"],
        "price": g["price"],
        "expires": g["expires"],
        "items": [{
            "id": ti["id"],
            "name": ti["name"],
            "image": ti["image"],
            "rarity": ti["rarity"],
            "type": ti["type"],
            "price": ti["price"],  # individual; puede ser None
        } for ti in g["items"]],
    })

payload = {
    "updatedAt": datetime.now(timezone.utc).isoformat(),
    "sourceDate": shop_date_str,
    "count": len(export_items),
    "ok": bool(export_items),
    "items": export_items,    # vista individual
    "groups": export_groups,  # ✅ vista por lote
}

out_path = WEB_OUT / "shop.json"
out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print("🗂  shop.json escrito en", out_path.resolve())

# ------------------ Facebook (opcional) ------------------

if items and FB_PAGE_ID and FB_PAGE_TOKEN:
    try:
        fecha_larga_fb = datetime.now(timezone.utc).strftime("%d %b %Y")
        caption_fb = fb_build_message_for_shop(fecha_larga_fb, items, max_headlines=6)
        print("📘 Publicando en Facebook Page (multi-imagen)…")
        post_multi_image_facebook(
            page_id=FB_PAGE_ID,
            page_token=FB_PAGE_TOKEN,
            items=items,
            base_message=caption_fb,
            per_image_caption=True,
            max_images=FB_MAX_IMAGES,
        )
    except Exception as e:
        print("❌ Error al postear en Facebook:", repr(e))
elif not items:
    print("ℹ️ Saltando Facebook: 0 artículos.")
else:
    print("⚠️ Saltando Facebook: faltan FB_PAGE_ID o FB_PAGE_TOKEN.")

# ------------------ Twitter (opcional) ------------------

if items and all([TW_API_KEY, TW_API_SECRET, TW_ACCESS_TOKEN, TW_ACCESS_SECRET]):
    try:
        print("📣 Publicando hilo en Twitter (solo texto)…")
        destacados = [it for it in items if "1500" in str(it.get("price"))]
        if len(destacados) < 5:
            for it in items:
                if it not in destacados:
                    destacados.append(it)
                if len(destacados) >= 5:
                    break
        items_destacados = destacados[:5]

        hoy = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        hr  = datetime.now(timezone.utc).strftime("%H:%M")
        header = (
            f"🛍 Tienda de Fortnite ({hoy}) 🎮 {hr}\n"
            "Ya salió la tienda de hoy con TODOS los objetos y rareza.\n"
        )
        footer = "Catálogo completo + precios diarios en mi Facebook 📲"
        if FB_PAGE_URL:
            footer += f"\n{FB_PAGE_URL}"
        tweets = chunk_lines_into_tweets(
            header, [make_line(i) for i in items_destacados], footer
        )

        client = tweepy.Client(
            consumer_key=TW_API_KEY,
            consumer_secret=TW_API_SECRET,
            access_token=TW_ACCESS_TOKEN,
            access_token_secret=TW_ACCESS_SECRET,
        )
        last = None
        for i, txt in enumerate(tweets):
            r = client.create_tweet(text=txt, in_reply_to_tweet_id=last) if last else client.create_tweet(text=txt)
            last = r.data["id"]
    except tweepy.Forbidden as e:
        print("❌ Forbidden Twitter:", e)
        try: print("Detalle:", e.response.text)
        except Exception: pass
    except tweepy.TweepyException as e:
        print("❌ Error Tweepy:", e)
else:
    if not items:
        print("ℹ️ Saltando Twitter: 0 artículos.")
    else:
        print("⚠️ Saltando Twitter: faltan credenciales.")

print("🏁 Fin del script.")
