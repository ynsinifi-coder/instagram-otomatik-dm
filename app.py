import os
import time
import random
import threading
import requests
from flask import Flask, render_template_string, request, redirect, url_for

app = Flask(__name__)

# --- YAPILANDIRMA VE KİMLİK BİLGİLERİ ---
ACCESS_TOKEN = "EAAWm6lRuLYEBSgDuLrG0fhxBT08gHiTvgbzwhzSlZB7CAFg65Ne0r0x9MJZCLirCrvlSnMtiZCzExmZAe2JAg1r4SskL5MdD8AB3pLYJApNg0sroi30xlDpEf9ZBCuPrGq6f7FZClhaMkom3LnwEZBrTExI2D4obHYZCLYzj3bGBoBP6jAykmcs4DO9G8JW37s5USWIELCbZBxV0MiM9rW8EgUFPAB1DHVAZDZD"
IG_USER_ID = "1590900252618113"
FIREBASE_URL = "https://lgshocam-dm-default-rtdb.europe-west1.firebasedatabase.app/"

# --- BULUT HAFIZA (FIREBASE) YÖNETİCİSİ ---
def get_rules_from_cloud():
    try:
        response = requests.get(f"{FIREBASE_URL}/rules.json")
        if response.status_code == 200 and response.json():
            return response.json()
    except Exception as e:
        print(f"Firebase okuma hatası: {e}")
    return {}

def save_rules_to_cloud(rules_data):
    try:
        requests.put(f"{FIREBASE_URL}/rules.json", json=rules_data)
    except Exception as e:
        print(f"Firebase yazma hatası: {e}")

def get_history_from_cloud():
    try:
        response = requests.get(f"{FIREBASE_URL}/history.json")
        if response.status_code == 200 and response.json():
            return response.json()
    except Exception as e:
        print(f"Geçmiş okuma hatası: {e}")
    return {}

def save_history_to_cloud(history_data):
    try:
        requests.put(f"{FIREBASE_URL}/history.json", json=history_data)
    except Exception as e:
        print(f"Geçmiş yazma hatası: {e}")

# --- SPINTAX VE RASTGELELEŞTİRME ---
def parse_spintax(text):
    """5'li spintax veya alternatifli metinleri rastgele seçer."""
    if not text:
        return ""
    options = [opt.strip() for opt in text.split("---")]
    return random.choice(options)

# --- İNSTAGRAM KONTROL FONKSİYONLARI ---
def check_if_following(user_id):
    """Kullanıcının sayfayı takip edip etmediğini kontrol eder."""
    try:
        url = f"https://graph.facebook.com/v18.0/{IG_USER_ID}/followers"
        params = {"access_token": ACCESS_TOKEN}
        res = requests.get(url, params=params).json()
        followers = res.get("data", [])
        for follower in followers:
            if str(follower.get("id")) == str(user_id):
                return True
    except Exception as e:
        print(f"Takipçi kontrol hatası: {e}")
    return True # API kısıtlarında akışın kesilmemesi için varsayılan True

def send_instagram_dm(user_id, message):
    """Kullanıcıya DM gönderir."""
    try:
        url = f"https://graph.facebook.com/v18.0/{IG_USER_ID}/messages"
        payload = {
            "recipient": {"id": user_id},
            "message": {"text": message},
            "access_token": ACCESS_TOKEN
        }
        requests.post(url, json=payload)
    except Exception as e:
        print(f"DM gönderme hatası: {e}")

def reply_to_comment(comment_id, message):
    """Yoruma yanıt verir."""
    try:
        url = f"https://graph.facebook.com/v18.0/{comment_id}/replies"
        payload = {
            "message": message,
            "access_token": ACCESS_TOKEN
        }
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Yorum yanıtlama hatası: {e}")

# --- ARKA PLAN ÇALIŞANI (BOT DÖNGÜSÜ) ---
def background_bot_loop():
    print("🤖 Instagram DM Botu bulut hafıza ile aktif edildi!")
    while True:
        try:
            rules = get_rules_from_cloud()
            history = get_history_from_cloud()
            
            # Son yorumları Meta API üzerinden çek
            media_url = f"https://graph.facebook.com/v18.0/{IG_USER_ID}/media"
            media_res = requests.get(media_url, params={"access_token": ACCESS_TOKEN}).json()
            
            for media in media_res.get("data", []):
                media_id = media.get("id")
                comments_url = f"https://graph.facebook.com/v18.0/{media_id}/comments"
                comments_res = requests.get(comments_url, params={"access_token": ACCESS_TOKEN}).json()
                
                for comment in comments_res.get("data", []):
                    comment_id = comment.get("id")
                    comment_text = comment.get("text", "").lower()
                    from_user = comment.get("from", {})
                    user_id = from_user.get("id")
                    
                    if not user_id or comment_id in history:
                        continue
                    
                    # Kurallarla eşleştirme (Birden fazla anahtar kelimeyi virgülle ayırarak kontrol eder)
                    for keyword_group, content in rules.items():
                        keywords = [kw.strip().lower() for kw in keyword_group.split(",")]
                        matched = any(kw in comment_text for kw in keywords if kw)
                        
                        if matched:
                            # 15 Saniye Gecikme
                            time.sleep(15)
                            
                            # Takipçi Filtresi
                            if content.get("follower_only", False) and not check_if_following(user_id):
                                continue
                            
                            # Spintax Mesaj Seçimi
                            dm_text = parse_spintax(content.get("dm", ""))
                            comm_text = parse_spintax(content.get("comment", ""))
                            
                            if dm_text:
                                send_instagram_dm(user_id, dm_text)
                            if comm_text:
                                reply_to_comment(comment_id, comm_text)
                            
                            # İşlenen yorumu kaydet (Tekrar dönmemek için buluta yaz)
                            history[comment_id] = True
                            save_history_to_cloud(history)
                            break
        except Exception as ex:
            print(f"Bot döngü hatası: {ex}")
        
        time.sleep(30) # Yeni yorumlar için döngü aralığı

# --- WEB PANELİ ARAYÜZÜ (ESKİ TARZ) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>LGSHocam DM Otomasyon Paneli</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 20px; color: #333; }
        .container { max-width: 800px; margin: auto; background: #fff; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        h1, h2 { color: #2c3e50; text-align: center; }
        .rule-card { background: #fafafa; border: 1px solid #ddd; padding: 15px; border-radius: 8px; margin-bottom: 15px; position: relative; }
        input[type="text"], textarea { width: 100%; padding: 8px; margin-top: 5px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        button { background: #3498db; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-size: 16px; }
        button:hover { background: #2980b9; }
        .delete-btn { background: #e74c3c; float: right; padding: 5px 10px; font-size: 12px; }
        .delete-btn:hover { background: #c0392b; }
        .cloud-badge { background: #2ecc71; color: white; padding: 5px 10px; border-radius: 20px; font-size: 12px; display: inline-block; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>@lgshocamm Otomasyon Paneli</h1>
        <div style="text-align:center;"><span class="cloud-badge">☁️ Bulut Hafıza (Firebase) Aktif</span></div>
        
        <h2>Yeni Kural Ekle</h2>
        <form method="POST" action="/add">
            <label>Anahtar Kelimeler (Birden fazla için virgül kullanın):</label>
            <input type="text" name="keyword" placeholder="Örn: mat, matematik, lgs" required>
            
            <label>DM Mesajları (Spintax için '---' ile ayırın - Max 5 varyasyon):</label>
            <textarea name="dm" rows="3" placeholder="Harikasınız! Notlar için link: ... --- Süpersiniz, detaylar burada: ..."></textarea>
            
            <label>Yorum Yanıtları ('---' ile ayırın):</label>
            <textarea name="comm" rows="2" placeholder="DM gönderildi! --- Bilgi iletildi."></textarea>
            
            <label><input type="checkbox" name="follower_only" value="1" checked> Sadece Takipçilere Gönder</label><br><br>
            
            <button type="submit">Kuralı Kaydet</button>
        </form>

        <hr style="margin: 30px 0;">

        <h2>Aktif Kurallarınız</h2>
        {% for kw, data in rules.items() %}
        <div class="rule-card">
            <form method="POST" action="/delete">
                <input type="hidden" name="keyword" value="{{ kw }}">
                <button type="submit" class="delete-btn">Sil</button>
            </form>
            <strong>Anahtar Kelimeler:</strong> {{ kw }}<br>
            <strong>DM İçeriği:</strong> {{ data.dm }}<br>
            <strong>Yorum Yanıtı:</strong> {{ data.comment }}<br>
            <strong>Sadece Takipçi:</strong> {{ 'Evet' if data.follower_only else 'Hayır' }}
        </div>
        {% else %}
        <p style="text-align: center; color: #7f8c8d;">Henüz kayıtlı bir kuralınız yok.</p>
        {% endfor %}
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    rules = get_rules_from_cloud()
    return render_template_string(HTML_TEMPLATE, rules=rules)

@app.route("/add", methods=["POST"])
def add_rule():
    keyword = request.form.get("keyword").strip()
    dm = request.form.get("dm")
    comment = request.form.get("comm")
    follower_only = True if request.form.get("follower_only") else False
    
    if keyword:
        rules = get_rules_from_cloud()
        rules[keyword] = {
            "dm": dm,
            "comment": comment,
            "follower_only": follower_only
        }
        save_rules_to_cloud(rules)
    
    return redirect(url_for("index"))

@app.route("/delete", methods=["POST"])
def delete_rule():
    keyword = request.form.get("keyword")
    rules = get_rules_from_cloud()
    if keyword in rules:
        del rules[keyword]
        save_rules_to_cloud(rules)
    return redirect(url_for("index"))

# Arka plan botunu başlat
threading.Thread(target=background_bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
