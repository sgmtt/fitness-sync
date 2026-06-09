"""
Oura OAuth2 初回認証スクリプト(手元PCで一度だけ実行する)。

ローカルに http://localhost:8080/callback を待ち受けるサーバーを立て、
ブラウザでOuraの認可画面を開き、認可コード -> アクセストークン+リフレッシュトークン
の交換まで行う。取得したリフレッシュトークンは .env に手動で転記して、
以降は oura_sync.py から使う(自動更新もそちらで行う)。

実行前に環境変数 OURA_CLIENT_ID / OURA_CLIENT_SECRET を設定しておくこと
(.env に書いて python-dotenv で読む、または直接 export してもよい)。
"""

import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "http://localhost:8080/callback"
AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SCOPES = "email personal daily heartrate workout tag session spo2 stress heart_health ring_configuration"

CLIENT_ID = os.environ.get("OURA_CLIENT_ID")
CLIENT_SECRET = os.environ.get("OURA_CLIENT_SECRET")

_auth_code = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        if code:
            _auth_code["code"] = code
            self.wfile.write("<h1>認証成功。このタブは閉じてOKです。</h1>".encode("utf-8"))
        else:
            self.wfile.write("<h1>認証コードの取得に失敗しました。</h1>".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # アクセスログを抑制


def get_authorization_code():
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    print("ブラウザでOuraの認可画面を開きます。表示されない場合は以下のURLを開いてください:")
    print(url)
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    print("認可コードの受信を待機中... (ブラウザで許可してください)")
    while "code" not in _auth_code:
        server.handle_request()
    server.server_close()
    return _auth_code["code"]


def exchange_code_for_tokens(code):
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    return resp.json()


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit(
            "OURA_CLIENT_ID / OURA_CLIENT_SECRET が未設定です。\n"
            ".env (または環境変数) に設定してから再実行してください。"
        )

    code = get_authorization_code()
    tokens = exchange_code_for_tokens(code)

    print("\n=== トークン取得成功 ===")
    print(f"access_token : {tokens['access_token']}")
    print(f"refresh_token: {tokens['refresh_token']}")
    print(f"expires_in   : {tokens['expires_in']} 秒")
    print(
        "\n上記の refresh_token を .env の OURA_REFRESH_TOKEN に転記してください。"
        "\n(GitHub Actionsで使う場合は Secrets の OURA_REFRESH_TOKEN にも設定すること)"
    )


if __name__ == "__main__":
    main()
