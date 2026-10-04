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
    if not text:
        return ""
    options = [opt.strip() for opt in text.split("---")]
    return random.choice(options)

# --- İNSTAGRAM KONTROL FONKSİYONLARI ---
def check_if_following(user_id):
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
    return True

def send_instagram_dm(user_id, message):
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
    print("🤖 Instagram DM Botu aktif edildi!")
    while True:
        try:
            rules = get_rules_from_cloud()
            history = get_history_from_cloud()
            
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
                    
                    for keyword_group, content in rules.items():
                        # Virgülle ayrılmış anahtar kelimeleri kontrol et
                        keywords = [kw.strip().lower() for kw in keyword_group.split(",")]
                        matched = any(kw in comment_text for kw in keywords if kw)
                        
                        if matched:
                            time.sleep(15) # 15 saniye bekleme
                            
                            if content.get("follower_only", False) and not check_if_following(user_id):
                                continue
                            
                            dm_text = parse_spintax(content.get("dm", ""))
                            comm_text = parse_spintax(content.get("comment", ""))
                            
                            if dm_text:
                                send_instagram_dm(user_id, dm_text)
                            if comm_text:
                                reply_to_comment(comment_id, comm_text)
                            
                            history[comment_id] = True
                            save_history_to_cloud(history)
                            break
        except Exception as ex:
            print(f"Bot döngü hatası: {ex}")
        
        time.sleep(30)

# --- ESKİ TARZ WEB PANELİ ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Instagram Otomasyon Paneli</title>
    <style>
        body { font-family: Segoe UI, Tahoma, Geneva, Verdana, sans-serif; background: #eef2f3; margin: 0; padding: 20px; }
        .container { max-width: 750px; margin: auto; background: #ffffff; padding: 25px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h2 { color: #333; border-bottom: 2px solid #eee; padding-bottom: 10px; }
        label { font-weight: bold; color: #555; display: block; margin-top: 10px; }
        input[type="text"], textarea { width: 100%; padding: 10px; margin-top: 5px; margin-bottom: 15px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; }
        button { background: #4CAF50; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 15px; }
        button:hover { background: #45a049; }
        .rule-box { background: #f9f9f9; border-left: 4px solid #4CAF50; padding: 12px; margin-bottom: 15px; border-radius: 4px; position: relative; }
        .delete-btn { background: #ff4d4d; float: right; padding: 5px 10px; font-size: 12px; }
        .delete-btn:hover { background: #cc0000; }
        .cloud-info { background: #e8f5e9; color: #2e7d32; padding: 8px; border-radius: 5px; font-size: 13px; margin-bottom: 15px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h2>@lgshocamm Otomasyon Paneli</h2>
        <div class="cloud-info">☁️ Bulut Hafıza Aktif (Kurallarınız silinmez)</div>
        
        <form method="POST" action="/add">
            <label>Anahtar Kelimeler (Birden fazla için virgül kullanın):</label>
            <input type="text" name="keyword" placeholder="Örn: mat, matematik, lgs" required>
            
            <label>DM Mesajları ('---' ile 5'li rotasyon yapabilirsiniz):</label>
            <textarea name="dm" rows="3" placeholder="Merhaba link burada --- Selam detaylar bu mesajda"></textarea>
            
            <label>Yorum Yanıtları ('---' ile ayırın):</label>
            <textarea name="comm" rows="2" placeholder="DM gönderildi! --- Bilgi iletildi."></textarea>
            
            <label><input type="checkbox" name="follower_only" value="1" checked> Sadece Takipçilere Gönder</label><br><br>
            
            <button type="submit">Kuralı Ekle</button>
        </form>

        <h2 style="margin-top: 40px;">Kayıtlı Kurallar</h2>
        {% for kw, data in rules.items() %}
        <div class="rule-box">
            <form method="POST" action="/delete">
                <input type="hidden" name="keyword" value="{{ kw }}">
                <button type="submit" class="delete-btn">Sil</button>
            </form>
            <strong>Anahtar Kelimeler:</strong> {{ kw }}<br>
            <strong>DM:</strong> {{ data.dm }}<br>
            <strong>Yorum Yanıtı:</strong> {{ data.comment }}<br>
            <strong>Sadece Takipçi:</strong> {{ 'Evet' if data.follower_only else 'Hayır' }}
        </div>
        {% else %}
        <p style="color: #777; text-align: center;">Henüz kural eklenmemiş.</p>
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

threading.Thread(target=background_bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
