import os
import json
import re
import requests
import random
import string
import secrets
import uuid
import time
import threading

from flask import Flask, request, render_template_string, redirect, jsonify

app = Flask(__name__)

# =========================================================
# AYARLAR
# =========================================================

ACCESS_TOKEN = "EAAWm6lRuLYEBSgDuLrG0fhxBT08gHiTvgbzwhzSlZB7CAFg65Ne0r0x9MJZCLirCrvlSnMtiZCzExmZAe2JAg1r4SskL5MdD8AB3pLYJApNg0sroi30xlDpEf9ZBCuPrGq6f7FZClhaMkom3LnwEZBrTExI2D4obHYZCLYzj3bGBoBP6jAykmcs4DO9G8JW37s5USWIELCbZBxV0MiM9rW8EgUFPAB1DHVAZDZD"
IG_USER_ID = "1590900252618113"

VERIFY_TOKEN = os.getenv(
    "VERIFY_TOKEN",
    "instagram_webhook_2026"
)

API_VERSION = "v26.0"

CONFIG_FILE = "instagram_config.json"
PROCESSED_FILE = "processed_comments.json"


# =========================================================
# CONFIG OKUMA / KAYDETME (ÇOKLU KURAL İÇİN GÜNCELLENDİ)
# =========================================================

def load_config():
    default_config = {
        "enabled": True,
        "rules": []
    }

    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            
            # Eski tekil sistemden yeni çoklu kural sistemine geçiş (Migration)
            if "rules" not in saved:
                if saved.get("reel_id"):
                    default_config["rules"].append({
                        "id": str(uuid.uuid4()),
                        "reel_url": saved.get("reel_url", ""),
                        "reel_id": saved.get("reel_id", ""),
                        "keyword": saved.get("keyword", "bilgi"),
                        "message": saved.get("message", ""),
                        "reply_1": saved.get("reply_1", ""),
                        "reply_2": saved.get("reply_2", ""),
                        "reply_3": saved.get("reply_3", "")
                    })
                default_config["enabled"] = saved.get("enabled", True)
            else:
                default_config = saved

    except Exception as e:
        print("CONFIG OKUMA HATASI:", e)

    return default_config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(
            config,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================================================
# İŞLENMİŞ YORUMLAR
# =========================================================

def load_processed_comments():
    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
    except Exception as e:
        print("İŞLENMİŞ YORUMLAR OKUMA HATASI:", e)

    return set()


def save_processed_comments(comments):
    try:
        with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(
                list(comments),
                f,
                ensure_ascii=False
            )
    except Exception as e:
        print("İŞLENMİŞ YORUMLAR KAYDETME HATASI:", e)


# =========================================================
# REEL SHORTCODE BULMA
# =========================================================

def extract_shortcode(url):
    if not url:
        return None
    url = url.strip()
    match = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|p|reels)/([^/?#]+)", url)
    if match:
        return match.group(1)
    return None


# =========================================================
# INSTAGRAM MEDYALARINI ARA
# =========================================================

def find_reel_by_url(target_url):
    if not ACCESS_TOKEN: return None, "INSTAGRAM_ACCESS_TOKEN eksik."
    if not IG_USER_ID: return None, "IG_USER_ID eksik."

    target_shortcode = extract_shortcode(target_url)
    if not target_shortcode: return None, "Geçerli bir Instagram Reel linki girilmedi."

    url = f"https://graph.instagram.com/{API_VERSION}/{IG_USER_ID}/media"
    params = {"fields": "id,media_type,permalink,caption,shortcode", "limit": 100, "access_token": ACCESS_TOKEN}

    page_count = 0
    media_count = 0

    while url and page_count < 100:
        page_count += 1
        try:
            response = requests.get(url, params=params, timeout=20)
        except Exception as e:
            return None, f"Instagram API bağlantı hatası: {e}"

        if response.status_code != 200: return None, response.text

        data = response.json()
        for media in data.get("data", []):
            media_count += 1
            shortcode = media.get("shortcode")
            permalink = media.get("permalink", "")

            if shortcode == target_shortcode or target_shortcode in permalink:
                return {
                    "media_id": media.get("id"),
                    "permalink": permalink,
                    "shortcode": shortcode,
                }, None

        next_url = data.get("paging", {}).get("next")
        if next_url:
            url = next_url
            params = {}
        else:
            url = None

    return None, f"Reel bulunamadı. Kontrol edilen: {media_count}"


# =========================================================
# ANA SAYFA
# =========================================================

@app.route("/", methods=["GET"])
def home():
    config = load_config()
    rule_count = len(config.get("rules", []))
    
    return f"""
    <h2>LGS Hocam Otomatik DM Sistemi aktif ✅</h2>
    <p>Şu anda <b>{rule_count}</b> farklı video için kurallar devrede.</p>
    <p><a href="/panel">Yönetim panelini aç</a></p>
    """


# =========================================================
# YÖNETİM PANELİ (ÇOKLU KURAL & SPINTAX)
# =========================================================

@app.route("/panel", methods=["GET", "POST"])
def panel():
    config = load_config()
    message = ""
    message_type = ""

    if request.method == "POST":
        reel_url = request.form.get("reel_url", "").strip()
        keyword = request.form.get("keyword", "").strip()
        dm_1 = request.form.get("dm_1", "").strip()

        if not reel_url or not keyword or not dm_1:
            message = "❌ Reel linki, kelime ve en azından 1. DM Mesajı alanı zorunludur."
            message_type = "error"
        else:
            reel, error = find_reel_by_url(reel_url)
            if error:
                message = f"❌ Reel bulunamadı.<br>{error}"
                message_type = "error"
            else:
                new_rule = {
                    "id": str(uuid.uuid4()),
                    "reel_url": reel_url,
                    "reel_id": reel["media_id"],
                    "keyword": keyword.lower(),
                    "dm_1": dm_1,
                    "dm_2": request.form.get("dm_2", "").strip(),
                    "dm_3": request.form.get("dm_3", "").strip(),
                    "dm_4": request.form.get("dm_4", "").strip(),
                    "dm_5": request.form.get("dm_5", "").strip(),
                    "reply_1": request.form.get("reply_1", "").strip(),
                    "reply_2": request.form.get("reply_2", "").strip(),
                    "reply_3": request.form.get("reply_3", "").strip(),
                    "reply_4": request.form.get("reply_4", "").strip(),
                    "reply_5": request.form.get("reply_5", "").strip()
                }
                
                config["rules"].append(new_rule)
                save_config(config)

                message = f"✅ Yeni rotasyonlu otomasyon kuralı başarıyla eklendi! (Kelime: {keyword})"
                message_type = "success"

    return render_template_string(
        PANEL_HTML,
        config=config,
        message=message,
        message_type=message_type
    )

# =========================================================
# KURAL SİLME
# =========================================================

@app.route("/delete_rule/<rule_id>", methods=["POST"])
def delete_rule(rule_id):
    config = load_config()
    config["rules"] = [r for r in config["rules"] if r.get("id") != rule_id]
    save_config(config)
    return redirect("/panel")


# =========================================================
# OTOMASYONU BAŞLAT / DURDUR
# =========================================================

@app.route("/toggle", methods=["POST"])
def toggle_automation():
    config = load_config()
    config["enabled"] = not bool(config.get("enabled", True))
    save_config(config)
    return redirect("/panel")


# =========================================================
# WEBHOOK DOĞRULAMA
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


# =========================================================
# WEBHOOK (15 Saniye Gecikmeli & Spintax Rotasyon)
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    config = load_config()

    if not config.get("enabled", True):
        return "EVENT_RECEIVED", 200

    rules = config.get("rules", [])
    if not rules:
        return "EVENT_RECEIVED", 200

    processed_comments = load_processed_comments()

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                
                if change.get("field") != "comments":
                    continue

                value = change.get("value", {})
                comment_id = value.get("id")
                comment_text = value.get("text", "").lower()
                media_id = value.get("media", {}).get("id") or value.get("media_id")

                if not media_id or not comment_id:
                    continue

                # Eğer daha önce işlendiyse atla
                if comment_id in processed_comments:
                    continue

                # Eşleşen kuralı ara
                matched_rule = None
                for rule in rules:
                    if str(rule.get("reel_id")) == str(media_id):
                        if rule.get("keyword") in comment_text:
                            matched_rule = rule
                            break 
                
                if not matched_rule:
                    continue 

                print(f"✅ UYGUN YORUM BULUNDU! Kural Kelimesi: {matched_rule['keyword']}")
                
                # Tekrar çalışmaması için anında işlenmiş yorumlara kaydediyoruz
                processed_comments.add(comment_id)
                save_processed_comments(processed_comments)

                # --- 15 SANİYE GECİKME VE SPINTAX İŞLEMİ ---
                def delayed_job(c_id, rule_data):
                    print(f"⏳ [KUYRUK] Yorum algılandı. 15 saniye bekleniyor... (Yorum ID: {c_id})")
                    time.sleep(15)
                    
                    # DM Rotasyonu Seçimi
                    dm_replies = []
                    if rule_data.get("dm_1"): dm_replies.append(rule_data["dm_1"])
                    if rule_data.get("dm_2"): dm_replies.append(rule_data["dm_2"])
                    if rule_data.get("dm_3"): dm_replies.append(rule_data["dm_3"])
                    if rule_data.get("dm_4"): dm_replies.append(rule_data["dm_4"])
                    if rule_data.get("dm_5"): dm_replies.append(rule_data["dm_5"])
                    
                    if not dm_replies and rule_data.get("message"):
                        dm_replies.append(rule_data["message"])

                    chosen_dm = random.choice(dm_replies) if dm_replies else "Mesaj bulunamadı"

                    success = send_private_reply(c_id, chosen_dm)
                    
                    if success:
                        replies = []
                        if rule_data.get("reply_1"): replies.append(rule_data["reply_1"])
                        if rule_data.get("reply_2"): replies.append(rule_data["reply_2"])
                        if rule_data.get("reply_3"): replies.append(rule_data["reply_3"])
                        if rule_data.get("reply_4"): replies.append(rule_data["reply_4"])
                        if rule_data.get("reply_5"): replies.append(rule_data["reply_5"])

                        if replies:
                            chosen_reply = random.choice(replies)
                            reply_to_comment(c_id, chosen_reply)
                            
                    print(f"✅ [BAŞARILI] İşlem tamamlandı. Yorum ID: {c_id}")

                thread = threading.Thread(target=delayed_job, args=(comment_id, matched_rule))
                thread.daemon = True
                thread.start()

    except Exception as e:
        print("WEBHOOK HATASI:", e)

    return "EVENT_RECEIVED", 200


# =========================================================
# YORUMA HERKESE AÇIK CEVAP YAZ
# =========================================================

def reply_to_comment(comment_id, reply_text):
    if not ACCESS_TOKEN: return False
    url = f"https://graph.instagram.com/{API_VERSION}/{comment_id}/replies"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.post(url, headers=headers, json={"message": reply_text}, timeout=20)
        return response.status_code in (200, 201)
    except:
        return False


# =========================================================
# DM GÖNDER
# =========================================================

def send_private_reply(comment_id, message_text):
    if not ACCESS_TOKEN or not IG_USER_ID: return False
    url = f"https://graph.instagram.com/{API_VERSION}/{IG_USER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"recipient": {"comment_id": comment_id}, "message": {"text": message_text}}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        return response.status_code in (200, 201)
    except:
        return False


# =========================================================
# PANEL HTML ARAYÜZÜ
# =========================================================

PANEL_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LGS Hocam - Gelişmiş Otomasyon Paneli</title>
    <style>
        :root { --primary: #4F46E5; --primary-hover: #4338CA; --bg-color: #F3F4F6; --card-bg: #FFFFFF; --text-main: #1F2937; --text-muted: #6B7280; --border-color: #E5E7EB; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--bg-color); color: var(--text-main); margin: 0; padding: 40px 20px; display: flex; justify-content: center; }
        .container { max-width: 900px; width: 100%; }
        .header { text-align: center; margin-bottom: 30px; }
        .header h1 { color: var(--primary); font-size: 28px; margin: 0; }
        .card { background: var(--card-bg); border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); padding: 30px; margin-bottom: 25px; }
        
        .status-box { display: flex; align-items: center; justify-content: space-between; padding: 20px; border-radius: 12px; background: #F8FAFC; border: 1px solid var(--border-color); margin-bottom: 30px; }
        .dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; margin-right: 5px; }
        .dot.active { background-color: #10B981; box-shadow: 0 0 10px rgba(16,185,129,0.4); }
        .dot.inactive { background-color: #EF4444; }
        
        .rule-list { display: grid; gap: 15px; margin-bottom: 30px; }
        .rule-item { background: #F9FAFB; border: 1px solid #E5E7EB; padding: 15px 20px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; }
        .rule-details h4 { margin: 0 0 5px 0; color: var(--text-main); }
        .rule-details p { margin: 0; font-size: 13px; color: var(--text-muted); }
        .rule-details .badge { background: #E0E7FF; color: #4338CA; padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; margin-left: 5px; }
        
        .form-group { margin-bottom: 20px; }
        label { display: block; font-weight: 600; margin-bottom: 8px; }
        .helper-text { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; display: block; }
        input[type="text"], textarea { width: 100%; padding: 14px; border: 1px solid var(--border-color); border-radius: 10px; box-sizing: border-box; font-family: inherit; }
        textarea { resize: vertical; }
        
        .reply-box { background: #F9FAFB; border: 1px dashed #D1D5DB; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
        .reply-box textarea { min-height: 60px; margin-bottom: 10px; }
        
        .btn { padding: 14px 20px; border: none; border-radius: 10px; font-weight: bold; cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; justify-content: center; text-decoration: none; }
        .btn-primary { background: var(--primary); color: white; width: 100%; font-size: 16px; }
        .btn-danger { background: #FEE2E2; color: #DC2626; padding: 8px 12px; font-size: 13px; }
        .btn-danger:hover { background: #FECACA; }
        .btn-toggle-on { background: #10B981; color: white; }
        .btn-toggle-off { background: #EF4444; color: white; }
        
        .alert { padding: 16px; border-radius: 10px; margin-bottom: 20px; font-weight: 500; }
        .alert-success { background: #D1FAE5; color: #065F46; border: 1px solid #A7F3D0; }
        .alert-error { background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA; }
    </style>
</head>
<body>

<div class="container">
    <div class="header">
        <h1>🎓 LGS Hocam</h1>
        <p>Gelişmiş Çoklu DM & Yorum Otomasyonu (Spintax Korumalı)</p>
    </div>

    {% if message %}
    <div class="alert alert-{{ message_type }}">{{ message | safe }}</div>
    {% endif %}

    <div class="card status-box">
        <div>
            <div style="font-weight: bold; font-size: 18px;">
                {% if config.get("enabled", True) %}
                    <span class="dot active"></span> Sistem Aktif (15 Sn. Korumalı)
                {% else %}
                    <span class="dot inactive"></span> Sistem Durduruldu
                {% endif %}
            </div>
            <p style="margin: 5px 0 0 0; color: var(--text-muted); font-size: 14px;">
                Aktif Kural Sayısı: <b>{{ config.get("rules", [])|length }}</b>
            </p>
        </div>
        <form method="POST" action="/toggle" style="margin: 0;">
            {% if config.get("enabled", True) %}
                <button type="submit" class="btn btn-toggle-off">⏸️ Tüm Sistemi Durdur</button>
            {% else %}
                <button type="submit" class="btn btn-toggle-on">▶️ Sistemi Başlat</button>
            {% endif %}
        </form>
    </div>

    <div class="card">
        <h2 style="margin-top: 0; font-size: 18px; border-bottom: 2px solid #F3F4F6; padding-bottom: 10px;">📋 Aktif Otomasyon Kuralları</h2>
        
        {% if not config.get("rules") %}
            <p style="color: var(--text-muted); text-align: center; padding: 20px;">Henüz hiç kural eklemediniz. Aşağıdan yeni bir Reels videosu ekleyebilirsiniz.</p>
        {% else %}
            <div class="rule-list">
                {% for rule in config.get("rules", []) %}
                <div class="rule-item">
                    <div class="rule-details">
                        <h4>Şart: <span class="badge">"{{ rule.keyword }}"</span> kelimesini yazanlar</h4>
                        <p><b>Video:</b> <a href="{{ rule.reel_url }}" target="_blank" style="color: var(--primary);">{{ rule.reel_url[:45] }}...</a></p>
                        <p><b>Örnek Mesaj:</b> 
                        {% if rule.dm_1 %}{{ rule.dm_1[:60] }}...
                        {% elif rule.message %}{{ rule.message[:60] }}...
                        {% else %}Mesaj Belirsiz{% endif %}
                        </p>
                    </div>
                    <form action="/delete_rule/{{ rule.id }}" method="POST" style="margin: 0;" onsubmit="return confirm('Bu kuralı silmek istediğinize emin misiniz?');">
                        <button type="submit" class="btn btn-danger">🗑️ Sil</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        {% endif %}
    </div>

    <div class="card">
        <h2 style="margin-top: 0; font-size: 18px; border-bottom: 2px solid #F3F4F6; padding-bottom: 10px;">➕ Yeni Kural Ekle</h2>
        <form method="POST">
            <div class="form-group">
                <label>📌 Instagram Reel Linki</label>
                <input type="text" name="reel_url" placeholder="Yeni videonun linkini yapıştırın..." required>
            </div>

            <div class="form-group">
                <label>🔑 Anahtar Kelime</label>
                <span class="helper-text">Örn: 9. sınıf için "kamp", 7. sınıf için "not" yazabilirsiniz.</span>
                <input type="text" name="keyword" placeholder="bilgi" required>
            </div>

            <div class="reply-box">
                <h3 style="margin-top: 0; font-size: 15px;">📩 Rastgele Gidecek DM Mesajları (En az 1 tane zorunlu)</h3>
                <span class="helper-text">Instagram botlarından kaçmak için mesajlarınızı çeşitlendirin.</span>
                <textarea name="dm_1" placeholder="1. DM Alternatifi (Zorunlu) - Örn: Merhaba, işte kamp linki! 🚀" required></textarea>
                <textarea name="dm_2" placeholder="2. DM Alternatifi (Opsiyonel)"></textarea>
                <textarea name="dm_3" placeholder="3. DM Alternatifi (Opsiyonel)"></textarea>
                <textarea name="dm_4" placeholder="4. DM Alternatifi (Opsiyonel)"></textarea>
                <textarea name="dm_5" placeholder="5. DM Alternatifi (Opsiyonel)"></textarea>
            </div>

            <div class="reply-box">
                <h3 style="margin-top: 0; font-size: 15px;">💬 Yoruma Verilecek Rastgele Cevaplar (Opsiyonel)</h3>
                <input type="text" name="reply_1" placeholder="1. Cevap (Örn: İlgili linki DM attım 🚀)" style="margin-bottom: 10px;">
                <input type="text" name="reply_2" placeholder="2. Cevap..." style="margin-bottom: 10px;">
                <input type="text" name="reply_3" placeholder="3. Cevap..." style="margin-bottom: 10px;">
                <input type="text" name="reply_4" placeholder="4. Cevap..." style="margin-bottom: 10px;">
                <input type="text" name="reply_5" placeholder="5. Cevap...">
            </div>

            <button type="submit" class="btn btn-primary">🚀 Kuralı Kaydet ve Aktifleştir</button>
        </form>
    </div>
</div>

</body>
</html>
"""

@app.get("/privacy-policy")
def privacy_policy(): return "Gizlilik Politikası", 200
@app.post("/deauthorize")
def deauthorize(): return "OK", 200
@app.post("/data-deletion")
def data_deletion(): return jsonify({"url": "...", "confirmation_code": "..."}), 200
@app.get("/data-deletion-status/<code>")
def data_deletion_status(code): return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
