import os
import sys
import json
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import feedparser
from jinja2 import Environment, FileSystemLoader

sys.stdout.reconfigure(encoding='utf-8')

# ================== 設定 ==================
SITE_NAME = "在庫・入荷速報チェッカー"
SITE_URL = os.getenv("SITE_URL", "https://anosrs.github.io/free-stock-site")
AMAZON_TAG = os.getenv("AMAZON_TAG", "nekonoki-22")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1531328595799183491/PFvqwM1s23KBhz8aEZuXLVPwW6fdL8NvnPbYW7utQhpqpGkdqcnEJ3nHOwOy2n_XbglP").strip()

# 監視フィード一覧
FEEDS = [
    "https://tokkacat.com/feed/",
    "https://nexxjp.com/feed/",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "products.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
# リポジトリ直下に出力（GitHub Pages が直接読み込めるように修正）
DIST_DIR = BASE_DIR
PRODUCT_DIST_DIR = os.path.join(DIST_DIR, "product")

# .env ファイルがあれば自動で読み込む
env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
                if k.strip() == "DISCORD_WEBHOOK_URL":
                    DISCORD_WEBHOOK_URL = v.strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# JST タイムゾーン
JST = timezone(timedelta(hours=9))


def clean_title(title: str) -> str:
    return re.sub(r"^\[在庫感知\]\s*", "", title or "").strip()


def extract_asin_from_text_or_url(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"/(?:dp|gp/product|exec/obidos/ASIN)/([A-Z0-9]{10})", text)
    if m:
        return m.group(1)
    m2 = re.search(r"\b(B0[A-Z0-9]{8}|[0-9]{9}[0-9X])\b", text)
    if m2:
        return m2.group(1)
    return None


def fetch_nexxjp_details(url: str) -> dict:
    """nexxjp.com のような個別ページから JSON-LD または ページ内リンクから ASIN / 価格を抜く"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=7)
        if r.status_code != 200:
            return {}
        soup = BeautifulSoup(r.text, "html.parser")
        
        # JSON-LD 探索
        json_ld = soup.find("script", type="application/ld+json")
        if json_ld and json_ld.string:
            try:
                data = json.loads(json_ld.string)
                if isinstance(data, list):
                    data = data[0]
                asin = data.get("sku") or extract_asin_from_text_or_url(data.get("description", ""))
                price = data.get("offers", {}).get("price")
                return {
                    "asin": asin,
                    "price": f"¥{price:,}" if isinstance(price, (int, float)) else str(price) if price else None,
                    "title": data.get("name"),
                    "image_url": data.get("image", [None])[0] if isinstance(data.get("image"), list) else data.get("image")
                }
            except Exception:
                pass

        # ページ内の Amazon リンクから ASIN 探索
        for a in soup.find_all("a", href=True):
            href = a["href"]
            asin = extract_asin_from_text_or_url(href)
            if asin:
                return {"asin": asin}
    except Exception as e:
        print(f"[Warning] fetch_details error ({url}): {e}")
    return {}


def parse_feed_entry(entry, feed_url: str) -> dict | None:
    title = clean_title(entry.get("title", ""))
    link = entry.get("link", "").strip()
    
    # 記事IDの生成 (リンクから数字IDを取得、なければハッシュ)
    m_id = re.search(r"/(\d+)/?$", link)
    if m_id:
        product_id = m_id.group(1)
    else:
        product_id = str(abs(hash(link)))[:8]

    # 本文取得
    content_html = ""
    if "content" in entry and entry["content"]:
        content_html = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        content_html = entry.get("summary", "")
    else:
        content_html = entry.get("description", "")

    soup = BeautifulSoup(content_html, "html.parser")
    text_block = title + " " + soup.get_text(" ", strip=True)

    asin = extract_asin_from_text_or_url(text_block)
    amazon_url = None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(d in href for d in ["amazon.co.jp", "amazon.jp", "amzn.to", "amzn.asia"]):
            amazon_url = href
            if not asin:
                asin = extract_asin_from_text_or_url(href)
            break

    image_url = None
    img = soup.find("img", src=True)
    if img:
        image_url = img["src"]

    price = None
    m_price = re.search(r"価格[:：]?\s*([¥￥]?[0-9,]+|価格情報なし)", text_block)
    if m_price:
        price = m_price.group(1)

    # もし nexxjp.com などの場合で ASIN が取れていない場合、個別ページをスクレイピング
    if "nexxjp.com" in feed_url or not asin:
        details = fetch_nexxjp_details(link)
        if details.get("asin"):
            asin = details["asin"]
        if details.get("price") and not price:
            price = details["price"]
        if details.get("image_url") and not image_url:
            image_url = details["image_url"]

    # ASIN が確定している場合の画像補完
    if not image_url and asin:
        image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ"

    # Amazon アフィリエイトURLの生成
    if asin:
        final_amazon_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZON_TAG}"
        cart_url = f"https://www.amazon.co.jp/gp/aws/cart/add.html?ASIN.1={asin}&tag={AMAZON_TAG}&Quantity.1=1"
    elif amazon_url:
        final_amazon_url = amazon_url + ("&" if "?" in amazon_url else "?") + f"tag={AMAZON_TAG}"
        cart_url = None
    else:
        final_amazon_url = link
        cart_url = None

    # 日時フォーマット (フィード内の実際の投稿日時を使用)
    if entry.get("published_parsed"):
        dt = datetime.fromtimestamp(time.mktime(entry["published_parsed"]), tz=timezone.utc).astimezone(JST)
    elif entry.get("updated_parsed"):
        dt = datetime.fromtimestamp(time.mktime(entry["updated_parsed"]), tz=timezone.utc).astimezone(JST)
    else:
        dt = datetime.now(JST)

    pub_date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    pub_date_short = dt.strftime("%m/%d %H:%M")
    pub_date_rfc = dt.strftime("%a, %d %b %Y %H:%M:%S +0900")
    timestamp = int(dt.timestamp())

    # 数値形式の価格
    price_numeric = 0
    if price:
        digits = re.sub(r"[^\d]", "", price)
        if digits:
            price_numeric = int(digits)

    return {
        "id": product_id,
        "title": title,
        "asin": asin,
        "price": price,
        "price_numeric": price_numeric,
        "image_url": image_url,
        "amazon_url": final_amazon_url,
        "cart_url": cart_url,
        "item_url": link,
        "pub_date": pub_date_str,
        "pub_date_short": pub_date_short,
        "pub_date_rfc": pub_date_rfc,
        "timestamp": timestamp
    }


def load_products() -> list:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_products(products: list):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def build_site(products: list):
    """Jinja2 テンプレートを使って HTML / RSS / Sitemap を生成"""
    os.makedirs(DIST_DIR, exist_ok=True)
    os.makedirs(PRODUCT_DIST_DIR, exist_ok=True)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    now = datetime.now(JST)
    current_year = now.year
    updated_at = now.strftime("%Y-%m-%d %H:%M")
    build_date = now.strftime("%a, %d %b %Y %H:%M:%S +0900")

    # 1. トップページ index.html
    tpl_index = env.get_template("index.html")
    html_index = tpl_index.render(
        site_name=SITE_NAME,
        site_url=SITE_URL,
        products=products[:60],  # 最新60件表示
        updated_at=updated_at,
        current_year=current_year
    )
    with open(os.path.join(DIST_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_index)

    # 2. 個別商品ページ product/{id}.html
    tpl_product = env.get_template("product.html")
    for p in products:
        html_product = tpl_product.render(
            site_name=SITE_NAME,
            site_url=SITE_URL,
            product=p,
            current_year=current_year
        )
        with open(os.path.join(PRODUCT_DIST_DIR, f"{p['id']}.html"), "w", encoding="utf-8") as f:
            f.write(html_product)

    # 3. RSSフィード feed.xml
    tpl_feed = env.get_template("feed.xml")
    xml_feed = tpl_feed.render(
        site_name=SITE_NAME,
        site_url=SITE_URL,
        products=products[:30],
        build_date=build_date
    )
    with open(os.path.join(DIST_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(xml_feed)

    # 4. Sitemap sitemap.xml
    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f'  <url><loc>{SITE_URL}/</loc><priority>1.0</priority></url>'
    ]
    for p in products:
        sitemap_lines.append(f'  <url><loc>{SITE_URL}/product/{p["id"]}.html</loc><priority>0.8</priority></url>')
    sitemap_lines.append('</urlset>')
    with open(os.path.join(DIST_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sitemap_lines))

    print(f"🎉 サイトビルド完了！ (総記事数: {len(products)} 件)")


def fetch_nexxjp_homepage_products() -> list:
    """nexxjp.com のトップページからリアルタイム『最新の在庫感知』アイテムを直接取得"""
    print("[Fetch Homepage] https://nexxjp.com/")
    results = []
    try:
        r = requests.get("https://nexxjp.com/", headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        
        product_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/product/(\d+)", href)
            if m:
                full_url = href if href.startswith("http") else f"https://nexxjp.com{href}"
                if full_url not in product_links:
                    product_links.append(full_url)

        now = datetime.now(JST)

        # 上位20件を順番に取得（並び順をそのまま保持）
        for idx, url in enumerate(product_links[:20]):
            details = fetch_nexxjp_details(url)
            m_id = re.search(r"/(\d+)/?$", url)
            product_id = m_id.group(1) if m_id else str(abs(hash(url)))[:8]
            
            asin = details.get("asin")
            title = details.get("title") or f"商品 ({product_id})"
            price = details.get("price")
            image_url = details.get("image_url")
            
            if not image_url and asin:
                image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ"

            if asin:
                final_amazon_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZON_TAG}"
                cart_url = f"https://www.amazon.co.jp/gp/aws/cart/add.html?ASIN.1={asin}&tag={AMAZON_TAG}&Quantity.1=1"
            else:
                final_amazon_url = url
                cart_url = None

            # 巡回して最新在庫を感知した時刻をそのまま商品の検知時間とする
            dt = now - timedelta(seconds=idx)
            
            price_numeric = 0
            if price:
                digits = re.sub(r"[^\d]", "", price)
                if digits:
                    price_numeric = int(digits)

            item = {
                "id": product_id,
                "title": title,
                "asin": asin,
                "price": price,
                "price_numeric": price_numeric,
                "image_url": image_url,
                "amazon_url": final_amazon_url,
                "cart_url": cart_url,
                "item_url": url,
                "pub_date": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "pub_date_short": dt.strftime("%m/%d %H:%M"),
                "pub_date_rfc": dt.strftime("%a, %d %b %Y %H:%M:%S +0900"),
                "timestamp": int(dt.timestamp())
            }
            results.append(item)
            print(f"  🔥 [NEXX TOP] {title} (ASIN: {asin}) ({item['pub_date_short']})")
            
    except Exception as e:
        print(f"[Warning] fetch_nexxjp_homepage_products error: {e}")
    return results


def send_discord_notification(item: dict):
    """新着商品検出時に Discord Webhook へ通知を送る"""
    if not DISCORD_WEBHOOK_URL:
        return

    title = item.get("title", "新着入荷商品")
    price = item.get("price") or "価格確認中"
    amazon_url = item.get("amazon_url", "")
    cart_url = item.get("cart_url", "")
    site_product_url = f"https://anosrs.github.io/free-stock-site/product/{item['id']}.html"
    image_url = item.get("image_url", "")
    pub_time = item.get("pub_date_short", "")

    description = f"**価格**: `{price}`\n\n"
    if amazon_url:
        description += f"🛒 **[Amazon商品ページを開く]({amazon_url})**\n"
    if cart_url:
        description += f"⚡ **[1クリック カートに追加]({cart_url})**\n"
    description += f"🌐 **[速報サイトで確認]({site_product_url})**"

    embed = {
        "title": f"🚨【入荷速報】{title}",
        "url": amazon_url or site_product_url,
        "description": description,
        "color": 15158332,  # 赤/オレンジ
        "footer": {"text": f"在庫・入荷速報チェッカー • {pub_time}"}
    }

    if image_url:
        embed["thumbnail"] = {"url": image_url}

    payload = {
        "username": "在庫・入荷速報BOT",
        "embeds": [embed]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code in [200, 204]:
            print(f"  🔔 [Discord通知完了] {title}")
        else:
            print(f"  ⚠️ [Discord通知失敗] Status Code: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ [Discord通知エラー] {e}")


def main():
    existing_products = load_products()
    product_map = {}
    for p in existing_products:
        key = p.get("asin") or p["id"]
        product_map[key] = p

    new_notified_keys = set()

    # 1. NEXX JP トップページから最新在庫感知を取得 (優先度高)
    homepage_items = fetch_nexxjp_homepage_products()
    for item in homepage_items:
        key = item.get("asin") or item["id"]
        if key in product_map:
            # 既存商品は元の通知日時・投稿日時をそのまま保護固定
            orig_item = product_map[key]
            merged = dict(orig_item)
            merged.update(item)
            merged["pub_date"] = orig_item.get("pub_date", item["pub_date"])
            merged["pub_date_short"] = orig_item.get("pub_date_short", item["pub_date_short"])
            merged["pub_date_rfc"] = orig_item.get("pub_date_rfc", item["pub_date_rfc"])
            merged["timestamp"] = orig_item.get("timestamp", item["timestamp"])
            product_map[key] = merged
        else:
            product_map[key] = item
            if key not in new_notified_keys:
                new_notified_keys.add(key)
                send_discord_notification(item)

    # 2. 各種 RSS フィードから取得
    for feed_url in FEEDS:
        print(f"[Fetch] {feed_url}")
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            item = parse_feed_entry(entry, feed_url)
            if not item:
                continue

            key = item.get("asin") or item["id"]

            if key in product_map:
                orig_item = product_map[key]
                merged = dict(orig_item)
                merged.update(item)
                merged["pub_date"] = orig_item.get("pub_date", item["pub_date"])
                merged["pub_date_short"] = orig_item.get("pub_date_short", item["pub_date_short"])
                merged["pub_date_rfc"] = orig_item.get("pub_date_rfc", item["pub_date_rfc"])
                merged["timestamp"] = orig_item.get("timestamp", item["timestamp"])
                product_map[key] = merged
            else:
                product_map[key] = item
                if key not in new_notified_keys:
                    new_notified_keys.add(key)
                    send_discord_notification(item)

    all_products = list(product_map.values())
    all_products.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    all_products = all_products[:3000]

    save_products(all_products)
    build_site(all_products)


if __name__ == "__main__":
    main()
