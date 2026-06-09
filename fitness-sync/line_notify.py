"""LINE Messaging API でテキスト+画像をプッシュ送信する。"""

import os

import requests

PUSH_URL = "https://api.line.me/v2/bot/message/push"


def push_text_and_image(text, image_url):
    """
    自分(LINE_USER_ID)にテキストと画像を1通で送る。

    LINEの画像メッセージは公開HTTPSのURLが必要(バイナリ直接添付は不可)。
    image_url / preview画像URL ともに同じURLを使う。
    """
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    user_id = os.environ["LINE_USER_ID"]

    messages = [{"type": "text", "text": text}]
    if image_url:
        messages.append(
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        )

    resp = requests.post(
        PUSH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": messages},
    )
    resp.raise_for_status()
    return resp
