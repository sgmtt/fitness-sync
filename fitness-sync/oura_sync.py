"""
Oura -> Notion(デイリーコンディションDB) 同期スクリプト。

毎日cronで実行される想定。リフレッシュトークンでアクセストークンを取得し、
前日分の daily_sleep / daily_readiness / sleep を取得してNotionにupsertする。

冪等性: notion_writer.upsert_page が「日付」一致ページを探して
更新 or 新規作成するため、同日に複数回実行しても重複登録されない。

リフレッシュトークンのローテーション:
Ouraはリフレッシュ時に新しいrefresh_tokenを返すことがある。
ローテーションされた場合は新しい値を標準出力に警告として出すので、
.env / GitHub Secrets の OURA_REFRESH_TOKEN を更新すること。
"""

import os
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from notion_writer import get_notion_client, upsert_page, number_prop, rich_text_prop

load_dotenv()

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"

CLIENT_ID = os.environ.get("OURA_CLIENT_ID")
CLIENT_SECRET = os.environ.get("OURA_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("OURA_REFRESH_TOKEN")
DAILY_CONDITION_DS_ID = os.environ["NOTION_DAILY_CONDITION_DS_ID"]


def refresh_access_token():
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        sys.exit(
            "OURA_CLIENT_ID / OURA_CLIENT_SECRET / OURA_REFRESH_TOKEN が未設定です。\n"
            "先に oura_auth.py で初回認証を行ってください。"
        )

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    tokens = resp.json()

    new_refresh_token = tokens.get("refresh_token")
    if new_refresh_token and new_refresh_token != REFRESH_TOKEN:
        print(
            "\n[警告] リフレッシュトークンがローテーションされました。"
            "\n.env / GitHub Secrets の OURA_REFRESH_TOKEN を以下の値に更新してください:"
            f"\n{new_refresh_token}\n"
        )
        # GitHub Actions側でこのファイルの有無を見て Secrets を自動更新する
        with open("new_oura_refresh_token.txt", "w", encoding="utf-8") as f:
            f.write(new_refresh_token)

    return tokens["access_token"]


def fetch_daily(endpoint, access_token, target_date):
    """指定日1日分のデータを取得する(start_date == end_date で範囲指定)。"""
    resp = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"start_date": target_date.isoformat(), "end_date": target_date.isoformat()},
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    return items[0] if items else None


def build_condition_properties(sleep_score, readiness_score, hrv, resting_hr, sleep_hours):
    properties = {}
    if sleep_score is not None:
        properties["睡眠スコア"] = number_prop(sleep_score)
    if readiness_score is not None:
        properties["レディネス"] = number_prop(readiness_score)
    if hrv is not None:
        properties["HRV"] = number_prop(hrv)
    if resting_hr is not None:
        properties["安静時心拍"] = number_prop(resting_hr)
    if sleep_hours is not None:
        properties["睡眠時間"] = number_prop(round(sleep_hours, 1))
    return properties


def main():
    # Ouraのデータは前日の睡眠を当日に反映する形になるため、前日分を対象にする
    target_date = date.today() - timedelta(days=1)
    date_str = target_date.isoformat()

    access_token = refresh_access_token()

    daily_sleep = fetch_daily("daily_sleep", access_token, target_date)
    daily_readiness = fetch_daily("daily_readiness", access_token, target_date)
    sleep_session = fetch_daily("sleep", access_token, target_date)

    sleep_score = daily_sleep["score"] if daily_sleep else None
    readiness_score = daily_readiness["score"] if daily_readiness else None

    hrv = None
    resting_hr = None
    sleep_hours = None
    if sleep_session:
        avg_hrv = sleep_session.get("average_hrv")
        avg_hr = sleep_session.get("average_heart_rate")
        total_sleep_seconds = sleep_session.get("total_sleep_duration")
        hrv = round(avg_hrv) if avg_hrv is not None else None
        resting_hr = round(avg_hr) if avg_hr is not None else None
        sleep_hours = total_sleep_seconds / 3600 if total_sleep_seconds is not None else None

    if not any([daily_sleep, daily_readiness, sleep_session]):
        print(f"{date_str}: Ouraデータが見つかりませんでした。スキップします。")
        return

    properties = build_condition_properties(sleep_score, readiness_score, hrv, resting_hr, sleep_hours)

    client = get_notion_client()
    page_id, action = upsert_page(
        client=client,
        data_source_id=DAILY_CONDITION_DS_ID,
        date_str=date_str,
        title=f"{date_str} コンディション",
        properties=properties,
    )

    print(f"{date_str}: Notionページを{action}しました (page_id={page_id})")
    print(f"  睡眠スコア={sleep_score} レディネス={readiness_score} HRV={hrv} "
          f"安静時心拍={resting_hr} 睡眠時間={sleep_hours}")


if __name__ == "__main__":
    main()
