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
SITE_URL = os.getenv("SITE_URL", "https://username.github.io/free-stock-site")
AMAZON_TAG = os.getenv("AMAZON_TAG", "nekonoki-22")

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


def main():
    existing_products = load_products()
    product_map = {p["id"]: p for p in existing_products}

    updated_count = 0

    for feed_url in FEEDS:
        print(f"[Fetch] {feed_url}")
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            item = parse_feed_entry(entry, feed_url)
            if not item:
                continue

            pid = item["id"]
            if pid in product_map:
                if item["timestamp"] > product_map[pid].get("timestamp", 0):
                    product_map[pid] = item
                    updated_count += 1
                    print(f"  🔄 [UPDATE/RESTOCK] {item['title']} ({item['pub_date_short']})")
            else:
                product_map[pid] = item
                updated_count += 1
                print(f"  ✨ [NEW] {item['title']} ({item['pub_date_short']})")

    all_products = list(product_map.values())
    all_products.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    all_products = all_products[:3000]

    save_products(all_products)
    build_site(all_products)


if __name__ == "__main__":
    main()
