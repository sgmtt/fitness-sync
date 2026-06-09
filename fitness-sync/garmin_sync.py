"""
Garmin Connect -> Notion(ワークアウトDB) 同期スクリプト。

毎日cronで実行される想定。Garmin Connectにログインして前日分のアクティビティを取得し、
activityType でランニング/筋トレ/その他を判別してワークアウトDBにupsertする。

冪等性: 1日に複数アクティビティ(例: ランニング+筋トレ)が存在しうるため、
notion_writer.upsert_page を match_by_title=True で呼び、
「日付」+「名前(アクティビティ名)」が一致するページを既存とみなして更新する。

認証: 2FA未設定のためメール+パスワードでそのままログインできる。
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from garminconnect import Garmin
from dotenv import load_dotenv

from notion_writer import (
    get_notion_client,
    upsert_page,
    number_prop,
    rich_text_prop,
    select_prop,
)

load_dotenv()

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
WORKOUT_DS_ID = os.environ["NOTION_WORKOUT_DS_ID"]

RUNNING_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running"}
STRENGTH_TYPES = {"strength_training"}


def login():
    # 保存済みOAuthトークンを優先(GitHub Actions等のデータセンターIPからの
    # パスワードログインはGarminに429/MFA要求でブロックされるため)。
    # トークンは garmin_auth.py を手元PCで実行して取得する(約1年有効)。
    tokens = os.environ.get("GARMIN_TOKENS")
    if tokens:
        client = Garmin()
        client.login(tokenstore=tokens)
        return client

    if not (GARMIN_EMAIL and GARMIN_PASSWORD):
        sys.exit(
            "GARMIN_TOKENS も GARMIN_EMAIL/GARMIN_PASSWORD も未設定です。\n"
            "garmin_auth.py を手元PCで実行してトークンを取得してください。"
        )

    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    return client


def classify(activity_type_key):
    if activity_type_key in RUNNING_TYPES:
        return "ランニング"
    if activity_type_key in STRENGTH_TYPES:
        return "筋トレ"
    return "その他"


def format_pace(duration_seconds, distance_meters):
    """分/km のペース文字列を作る(距離0または不明な場合はNone)。"""
    if not duration_seconds or not distance_meters:
        return None
    distance_km = distance_meters / 1000
    if distance_km <= 0:
        return None
    pace_min_per_km = (duration_seconds / 60) / distance_km
    minutes = int(pace_min_per_km)
    seconds = round((pace_min_per_km - minutes) * 60)
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}'{seconds:02d}\"/km"


def fetch_exercise_summary(client, activity_id):
    """筋トレの種目名一覧とセット数を可能な範囲で取得する(取得失敗時は空)。"""
    try:
        sets_data = client.get_activity_exercise_sets(activity_id)
    except Exception:
        return [], 0

    exercise_sets = sets_data.get("exerciseSets") or []
    working_sets = [s for s in exercise_sets if s.get("setType") == "ACTIVE"]

    names = []
    for s in working_sets:
        for exercise in s.get("exercises") or []:
            name = exercise.get("name") or exercise.get("category")
            if name and name not in names:
                names.append(name)

    return names, len(working_sets)


def build_properties(client, activity, kind):
    duration = activity.get("duration")
    distance = activity.get("distance")
    calories = activity.get("calories")
    avg_hr = activity.get("averageHR")
    max_hr = activity.get("maxHR")

    properties = {"種類": select_prop(kind)}

    if duration is not None:
        properties["所要時間分"] = number_prop(round(duration / 60))
    if calories is not None:
        properties["消費カロリー"] = number_prop(round(calories))
    if avg_hr is not None:
        properties["平均心拍"] = number_prop(round(avg_hr))
    if max_hr is not None:
        properties["最大心拍"] = number_prop(round(max_hr))

    if kind == "ランニング":
        if distance is not None:
            properties["距離km"] = number_prop(round(distance / 1000, 2))
        pace = format_pace(duration, distance)
        if pace is not None:
            properties["ペース"] = rich_text_prop(pace)

    if kind == "筋トレ":
        names, set_count = fetch_exercise_summary(client, activity["activityId"])
        if set_count:
            properties["セット数"] = number_prop(set_count)
        if names:
            properties["種目"] = rich_text_prop(", ".join(names))

    return properties


def main():
    # Actions実行環境はUTCのため、日付は日本時間基準で計算する
    target_date = datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    date_str = target_date.isoformat()

    client = login()
    activities = client.get_activities_by_date(date_str, date_str)

    if not activities:
        print(f"{date_str}: Garminアクティビティが見つかりませんでした。スキップします。")
        return

    notion_client = get_notion_client()

    for activity in activities:
        type_key = (activity.get("activityType") or {}).get("typeKey", "")
        kind = classify(type_key)
        title = activity.get("activityName") or f"{date_str} {kind}"

        properties = build_properties(client, activity, kind)

        page_id, action = upsert_page(
            client=notion_client,
            data_source_id=WORKOUT_DS_ID,
            date_str=date_str,
            title=title,
            properties=properties,
            match_by_title=True,
        )

        print(f"{date_str}: 「{title}」({kind})を{action}しました (page_id={page_id})")


if __name__ == "__main__":
    main()
