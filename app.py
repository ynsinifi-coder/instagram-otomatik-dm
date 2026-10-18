"""Reels yorum otomasyonu. Kurulum için KURULUM.md dosyasını okuyun."""
import argparse
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import firebase_admin
from firebase_admin import credentials
import json
from google.auth.transport.requests import AuthorizedSession
from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE') == '1', MAX_CONTENT_LENGTH=65536)
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN', '')
IG_USER_ID = os.getenv('IG_USER_ID', '')
FIREBASE_URL = os.getenv('FIREBASE_URL', '').rstrip('/')
FIREBASE_SERVICE_ACCOUNT = os.getenv(
    'FIREBASE_SERVICE_ACCOUNT',
    '/etc/secrets/firebase-service-account.json'
)
API_VERSION = os.getenv('META_API_VERSION', '')
LOGIN_TYPE = os.getenv('META_LOGIN_TYPE', 'facebook')
GRAPH_HOST = 'https://graph.instagram.com' if LOGIN_TYPE == 'instagram' else 'https://graph.facebook.com'
PANEL_USER = os.getenv('PANEL_USER', 'admin')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD', '')
POLL_SECONDS = max(30, int(os.getenv('POLL_SECONDS', '120')))
POLL_ENABLED = os.getenv('POLL_ENABLED', '0') == '1'
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN', '')
META_APP_SECRET = os.getenv('META_APP_SECRET', '')
SEND_DELAY = max(0, int(os.getenv('SEND_DELAY_SECONDS', '5')))
STOP = threading.Event()
EVENTS = deque(maxlen=50)
EVENT_LOCK = threading.Lock()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
 if not os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON") and not os.path.isfile(FIREBASE_SERVICE_ACCOUNT):

if firebase_json:
    firebase_credential = credentials.Certificate(json.loads(firebase_json))
else:
    firebase_credential = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT)
    ) from None

class ServiceError(Exception):
    pass


def event(message):
    logging.info(message)
    with EVENT_LOCK:
        EVENTS.appendleft(time.strftime('%H:%M:%S') + ' — ' + message)


def validate_config():
    missing = [name for name, value in [('ACCESS_TOKEN', ACCESS_TOKEN), ('IG_USER_ID', IG_USER_ID),
               ('FIREBASE_URL', FIREBASE_URL), ('META_API_VERSION', API_VERSION),
               ('PANEL_PASSWORD', PANEL_PASSWORD), ('SECRET_KEY', os.getenv('SECRET_KEY'))] if not value]
    if missing:
        raise SystemExit('Eksik ortam değişkenleri: ' + ', '.join(missing))
    if LOGIN_TYPE not in ('facebook', 'instagram') or not re.fullmatch(r'v\d+\.\d+', API_VERSION):
        raise SystemExit('META_LOGIN_TYPE veya META_API_VERSION geçersiz.')
    if urlparse(FIREBASE_URL).scheme != 'https':
        raise SystemExit('FIREBASE_URL HTTPS olmalı.')


def graph(method, path, *, params=None, payload=None):
    """Gönderimlerde otomatik tekrar yok; hata çıktısında token/yanıt gövdesi yok."""
    uncertain = ' Gönderim sonucu belirsiz olabilir; otomatik tekrar yapılmaz.' if method != 'GET' else ''
