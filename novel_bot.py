import os, re, json, asyncio
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN", "YAHAN_APNA_BOT_TOKEN_DALO")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def json_ld(soup):
    items = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for d in stack:
            if isinstance(d, dict):
                items.append(d)
                items.extend(x for x in d.get("@graph", []) if isinstance(x, dict))
    return items


def fetch_details(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    ld = json_ld(soup)

    # Name
    title = meta(soup, "og:title", "twitter:title") or (soup.h1.get_text(strip=True) if soup.h1 else None)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = re.split(r"\s[-|–]\s", title or "Unknown")[0].strip()
    title = re.sub(r"^\s*read\s+", "", title, flags=re.I)
    title = re.sub(r"\s*\b(RAW\s+English\s+Translation|English\s+Translation|RAW|Read\s+Online|Online)\b.*$",
                   "", title, flags=re.I).strip()

    # Cover
    cover = meta(soup, "og:image", "twitter:image")

    # Rating
    rating = None
    for d in ld:
        ar = d.get("aggregateRating")
        if isinstance(ar, dict) and ar.get("ratingValue"):
            best = ar.get("bestRating", 5)
            rating = f"{ar['ratingValue']}/{best}"
            break
    if not rating:
        el = soup.find(attrs={"itemprop": "ratingValue"})
        if el:
            rating = f"{el.get_text(strip=True) or el.get('content')}/5"
    if not rating:
        m = re.search(r"(\d(?:\.\d+)?)\s*/\s*(5|10)\b", text)
        if m:
            rating = f"{m.group(1)}/{m.group(2)}"

    # Tags / genres
    tags = []
    for d in ld:
        g = d.get("genre")
        if g:
            tags += g if isinstance(g, list) else [g]
    if not tags:
        for a in soup.find_all("a", href=True):
            if re.search(r"/(genre|genres|tag|tags|category|categories)/", a["href"], re.I):
                t = a.get_text(strip=True)
                if t and len(t) < 30 and t not in tags:
                    tags.append(t)
    if not tags:
        for a in soup.select('[class*="genre"] a, [class*="tag"] a, [class*="categor"] a'):
            t = a.get_text(strip=True)
            if t and len(t) < 30 and t not in tags:
                tags.append(t)
    if not tags:
        kw = meta(soup, "keywords")
        if kw:
            tags = [k.strip() for k in kw.split(",")][:10]
    tags = tags[:12]

    # Chapters
    chapters = None
    m = (re.search(r"(\d[\d,]*)\s*(?:chapters|chs)\b", text, re.I)
         or re.search(r"chapters?\s*[:\-]?\s*(\d[\d,]*)", text, re.I))
    if m:
        chapters = m.group(1).replace(",", "")
    else:
        links = {a["href"] for a in soup.find_all("a", href=True)
                 if re.search(r"chapter", a["href"], re.I)}
        if links:
            chapters = str(len(links))

    # Status
    m = re.search(r"\b(ongoing|completed|complete|hiatus|finished)\b", text, re.I)
    status = m.group(1).capitalize() if m else None

    desc = meta(soup, "og:description", "description") or ""

    return {"title": title, "cover": cover, "rating": rating,
            "tags": tags, "chapters": chapters, "status": status, "desc": desc[:600]}


GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")


def ai_extra(d):
    """Naya naam + (zarurat ho to) tags banata hai. Return: (title, tags, error)"""
    if not GEMINI_KEY:
        return None, [], "GEMINI_API_KEY set nahi hai (GitHub Secrets check karo)"
    prompt = (
        "You are a web-novel editor. For the novel below, create ONE new, catchy, original "
        "English title (max 10 words) that is clearly different from the original title and "
        "does not reuse its wording. Also give 6 to 9 short genre/tag words.\n"
        'Reply ONLY with JSON: {"title": "...", "tags": ["...", "..."]}\n\n'
        f"Original title: {d['title']}\n"
        f"Known tags: {', '.join(d['tags'])}\n"
        f"Synopsis: {d['desc']}"
    )
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": 300,
                                       "responseMimeType": "application/json",
                                       "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        return str(data.get("title", "")).strip() or None, list(data.get("tags", [])), None
    except Exception as e:
        return None, [], f"Gemini error: {e}"


def build_caption(d):
    lines = [f"📖 Novel Name: {d.get('new_title') or d['title']}"]
    if d["tags"]:
        lines.append(f"\n🏷️ Tags: {', '.join(d['tags'])}.")
    if d["chapters"]:
        lines.append(f"\n📚 Total Chapters: {d['chapters']}")
    if d["rating"]:
        lines.append(f"\n⭐ Rating: {d['rating']}")
    if d["status"]:
        lines.append(f"\n📌 Status: {d['status']}")
    return "\n".join(lines)[:1024]


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Novel ka link bhejo, main uski details nikal dunga 📖")


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"https?://\S+", update.message.text or "")
    if not m:
        await update.message.reply_text("Please ek valid novel link bhejo.")
        return
    wait = await update.message.reply_text("⏳ Details nikal raha hoon...")
    try:
        d = await asyncio.to_thread(fetch_details, m.group(0))
        new_title, ai_tags, err = await asyncio.to_thread(ai_extra, d)
        d["new_title"] = new_title
        if not d["tags"]:
            d["tags"] = ai_tags
        caption = build_caption(d)
        if d["cover"]:
            try:
                await update.message.reply_photo(d["cover"], caption=caption)
            except Exception:
                await update.message.reply_text(caption)
        else:
            await update.message.reply_text(caption)
        if err:
            await update.message.reply_text(f"⚠️ Naya naam nahi ban paya: {err}")
    except Exception as e:
        await update.message.reply_text(f"❌ Details nahi mil payi: {e}")
    finally:
        await wait.delete()


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()


if __name__ == "__main__":
    main()
