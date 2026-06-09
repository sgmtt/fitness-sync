"""Notion書き込み用の共通ヘルパー。日付をキーにしたupsertで冪等性を担保する。"""

import os

from notion_client import Client
from dotenv import load_dotenv

load_dotenv()


def get_notion_client():
    token = os.environ["NOTION_TOKEN"]
    return Client(auth=token)


def find_page_by_date(client, data_source_id, date_str, title=None):
    """
    指定データソース内で「日付」が date_str と一致するページを探す。
    title を渡した場合は「名前」も一致するものに絞る(1日に複数件あるDB向け)。
    見つからなければNone。

    Notion API 2025-09-03 仕様: databases.query ではなく data_sources.query を使う。
    """
    conditions = [{"property": "日付", "date": {"equals": date_str}}]
    if title is not None:
        conditions.append({"property": "名前", "title": {"equals": title}})

    filter_obj = conditions[0] if len(conditions) == 1 else {"and": conditions}

    result = client.data_sources.query(
        data_source_id=data_source_id,
        filter=filter_obj,
        page_size=1,
    )
    results = result.get("results", [])
    return results[0] if results else None


def upsert_page(client, data_source_id, date_str, title, properties, match_by_title=False):
    """
    既存ページがあれば更新、なければ新規作成する(二重登録防止)。

    match_by_title=True の場合、「日付」に加えて「名前」も一致するページを既存とみなす。
    1日に複数件入りうるDB(例: ワークアウト)で使う。

    properties: 「日付」「名前」以外のプロパティ群を辞書で渡す
                (例: {"睡眠スコア": {"number": 80}, ...})

    Notion API 2025-09-03 仕様: ページ作成時の parent は data_source_id を指定する。
    """
    full_properties = {
        "名前": {"title": [{"text": {"content": title}}]},
        "日付": {"date": {"start": date_str}},
        **properties,
    }

    existing = find_page_by_date(
        client, data_source_id, date_str, title=title if match_by_title else None
    )
    if existing:
        client.pages.update(page_id=existing["id"], properties=full_properties)
        return existing["id"], "updated"

    created = client.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id},
        properties=full_properties,
    )
    return created["id"], "created"


def query_by_date(client, data_source_id, date_str):
    """指定データソースの「日付」== date_str に一致するページを全件返す(レポート集計用)。"""
    result = client.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "日付", "date": {"equals": date_str}},
    )
    return result.get("results", [])


def get_number(page, prop_name):
    prop = page["properties"].get(prop_name) or {}
    return prop.get("number")


def get_select(page, prop_name):
    prop = page["properties"].get(prop_name) or {}
    sel = prop.get("select")
    return sel.get("name") if sel else None


def get_title(page, prop_name="名前"):
    prop = page["properties"].get(prop_name) or {}
    arr = prop.get("title") or []
    return "".join(t.get("plain_text", "") for t in arr)


def get_rich_text(page, prop_name):
    prop = page["properties"].get(prop_name) or {}
    arr = prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in arr)


def number_prop(value):
    return {"number": value}


def rich_text_prop(text):
    return {"rich_text": [{"text": {"content": text}}]}


def select_prop(name):
    return {"select": {"name": name}}
