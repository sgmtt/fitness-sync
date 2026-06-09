"""
Garmin 初回認証スクリプト(手元PCで一度だけ実行する)。

GitHub ActionsなどのデータセンターIPからメール+パスワードでログインすると
Garminに429/MFA要求でブロックされるため、手元PCで一度ログインして
OAuthトークン(約1年有効)を取得し、以降はトークンでログインする。

実行すると .env の GARMIN_TOKENS を自動で追記/更新する。
GitHub Secrets への登録は別途行うこと(値は print しない)。
"""

import os
import sys

from garminconnect import Garmin
from dotenv import load_dotenv

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(ENV_PATH)


def update_env_var(name, value):
    """ENV_PATH の name=... 行を更新(なければ末尾に追記)する。"""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not (email and password):
        sys.exit("GARMIN_EMAIL / GARMIN_PASSWORD が .env に未設定です。")

    print("Garmin Connect にログインしています...")
    print("(新しい端末からのログインのため、登録メールアドレスに認証コードが届きます)")

    def prompt_mfa():
        return input("メールに届いた認証コード(6桁)を入力してEnter: ").strip()

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login()

    tokens = client.client.dumps()
    if not tokens or len(tokens) < 512:
        sys.exit("トークンの取得に失敗しました(形式が想定外)。")

    update_env_var("GARMIN_TOKENS", tokens)
    print(f"ログイン成功。GARMIN_TOKENS を {ENV_PATH} に保存しました(約{len(tokens)}文字)。")
    print("このトークンは約1年有効。期限切れ時はこのスクリプトを再実行してください。")


if __name__ == "__main__":
    main()
