"""
毎朝7時(JST)に実行する日次レポート生成・LINE送信スクリプト。

処理の流れ:
1. 前日分のデータをNotionの3データソース(コンディション/ワークアウト/食事)から取得
2. Claude API(Opus 4.8)で「昨日の総合評価コメント」を生成(健康意識を上げる前向きな評価+助言)
3. matplotlibで視覚的なレポートをJPEGとして描画
4. JPEGをリポジトリの reports/ に保存(GitHub Actionsがコミットしraw URLで公開)
5. LINE Messaging APIで自分にテキスト+画像をプッシュ送信

GitHub Actions側で daily_sync(Oura/Garmin同期)の後に実行する想定。
"""

import os
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def yesterday_jst():
    """日本時間での「昨日」。Actions実行環境はUTCのため明示的にJSTで計算する。"""
    return datetime.now(JST).date() - timedelta(days=1)

import matplotlib

matplotlib.use("Agg")  # GUIなしバックエンド(サーバー実行用)
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams
from dotenv import load_dotenv
import anthropic

from notion_writer import (
    get_notion_client,
    query_by_date,
    get_number,
    get_select,
    get_title,
    get_rich_text,
)

load_dotenv()

# sendサブコマンドはNotionにアクセスしないため、ここでは必須にしない
# (generate実行時にNoneのままならNotionクエリで自然に失敗する)
DAILY_CONDITION_DS_ID = os.environ.get("NOTION_DAILY_CONDITION_DS_ID")
WORKOUT_DS_ID = os.environ.get("NOTION_WORKOUT_DS_ID")
MEAL_DS_ID = os.environ.get("NOTION_MEAL_DS_ID")

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")

# 配色(落ち着いた、読みやすいトーン)
COL_BG = "#F4F1EA"
COL_CARD = "#FFFFFF"
COL_TEXT = "#33312E"
COL_ACCENT = "#C36F4B"
COL_SUB = "#8A857D"
COL_GOOD = "#5C8A5C"
COL_WARN = "#C9A227"


def setup_japanese_font():
    """環境にある日本語フォントを探して設定する(Windows/Linux両対応)。"""
    candidates = [
        "Yu Gothic", "Meiryo", "MS Gothic",          # Windows
        "Noto Sans CJK JP", "IPAexGothic", "TakaoPGothic",  # Linux
        "Hiragino Sans",                              # macOS
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            rcParams["font.family"] = name
            return name
    return None  # 見つからなければデフォルト(文字化けの可能性あり)


# ---------- データ取得 ----------

def fetch_condition(client, date_str):
    pages = query_by_date(client, DAILY_CONDITION_DS_ID, date_str)
    if not pages:
        return None
    p = pages[0]
    return {
        "睡眠スコア": get_number(p, "睡眠スコア"),
        "レディネス": get_number(p, "レディネス"),
        "HRV": get_number(p, "HRV"),
        "安静時心拍": get_number(p, "安静時心拍"),
        "睡眠時間": get_number(p, "睡眠時間"),
    }


def fetch_workouts(client, date_str):
    pages = query_by_date(client, WORKOUT_DS_ID, date_str)
    workouts = []
    for p in pages:
        workouts.append(
            {
                "名前": get_title(p),
                "種類": get_select(p, "種類"),
                "距離km": get_number(p, "距離km"),
                "ペース": get_rich_text(p, "ペース"),
                "種目": get_rich_text(p, "種目"),
                "セット数": get_number(p, "セット数"),
                "所要時間分": get_number(p, "所要時間分"),
                "消費カロリー": get_number(p, "消費カロリー"),
                "平均心拍": get_number(p, "平均心拍"),
            }
        )
    return workouts


def fetch_meals(client, date_str):
    pages = query_by_date(client, MEAL_DS_ID, date_str)
    meals = []
    for p in pages:
        meals.append(
            {
                "区分": get_select(p, "食事区分"),
                "内容": get_rich_text(p, "内容"),
                "カロリー": get_number(p, "カロリー"),
                "タンパク質g": get_number(p, "タンパク質g"),
                "脂質g": get_number(p, "脂質g"),
                "糖質g": get_number(p, "糖質g"),
            }
        )
    return meals


def summarize_meals(meals):
    total = {"カロリー": 0, "タンパク質g": 0, "脂質g": 0, "糖質g": 0}
    for m in meals:
        for k in total:
            if m.get(k):
                total[k] += m[k]
    return total


# ---------- 評価コメント生成 ----------

def rule_based_evaluation(condition, workouts, meal_total):
    """ANTHROPIC_API_KEY 未設定時のフォールバック(ゼロコスト運用)。しきい値でスコアとコメントを作る。"""
    c = condition or {}
    parts = []
    score_inputs = []

    sleep = c.get("睡眠スコア")
    readiness = c.get("レディネス")
    if sleep is not None:
        score_inputs.append(sleep)
        parts.append("よく眠れています。" if sleep >= 75 else "睡眠がやや不足気味です。")
    if readiness is not None:
        score_inputs.append(readiness)

    if workouts:
        score_inputs.append(80)
        parts.append("運動を実施できました。継続が力になります。")
    else:
        parts.append("休養日でした。回復も大切なトレーニングです。")

    protein = meal_total.get("タンパク質g", 0)
    kcal = meal_total.get("カロリー", 0)
    if kcal:
        if protein >= 60:
            parts.append("タンパク質も十分です。")
        else:
            parts.append("タンパク質をもう少し増やせると理想的です。")

    score = round(sum(score_inputs) / len(score_inputs)) if score_inputs else 50

    if score >= 75:
        headline, advice = "好調をキープ", "この調子で今日も体を動かしましょう"
    elif score >= 50:
        headline, advice = "まずまずの一日", "今夜は早めの就寝を意識しましょう"
    else:
        headline, advice = "回復を優先しよう", "今日は無理せず睡眠を最優先に"

    return {
        "score": score,
        "headline": headline,
        "comment": "".join(parts)[:120],
        "advice": advice,
    }


def generate_evaluation(date_str, condition, workouts, meal_total, meals):
    # APIキー未設定ならルールベースで評価(無料枠運用のままにする選択肢)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 未設定: ルールベース評価を使用します。")
        return rule_based_evaluation(condition, workouts, meal_total)

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境から読む

    data = {
        "日付": date_str,
        "コンディション(Oura)": condition,
        "ワークアウト(Garmin)": workouts,
        "食事合計": meal_total,
        "食事内訳": meals,
    }

    system = (
        "あなたはユーザーの健康習慣を支えるパーソナルコーチです。"
        "前日の運動・回復・食事データをもとに、その日を総合評価し、"
        "ユーザーの健康意識を高める前向きで具体的なフィードバックを日本語で返してください。"
        "厳しすぎず、できている点をまず認め、改善点は1〜2個に絞って実行しやすく示します。"
        "データが欠けている項目には触れすぎないこと。"
    )

    user = (
        "以下は昨日の健康データです(JSON)。\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
        "次のJSON形式だけを出力してください(前置き・コードフェンス不要):\n"
        "{\n"
        '  "score": 0-100の整数(昨日の総合スコア),\n'
        '  "headline": "一言サマリー(20文字以内)",\n'
        '  "comment": "総合評価コメント(120文字程度、ねぎらい+気づき)",\n'
        '  "advice": "今日の具体的アクション1つ(40文字以内)"\n'
        "}"
    )

    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        # API障害でレポート自体が止まらないようルールベースに切り替える
        print(f"Claude API 呼び出しに失敗({e.__class__.__name__}): ルールベース評価にフォールバックします。")
        return rule_based_evaluation(condition, workouts, meal_total)

    text = next((b.text for b in resp.content if b.type == "text"), "").strip()

    # コードフェンスが付いた場合に備えて除去
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "score": condition.get("レディネス") if condition else 50,
            "headline": "今日も一歩ずつ",
            "comment": text[:120] or "データを記録できています。継続が一番の力です。",
            "advice": "水分をこまめに取りましょう",
        }


# ---------- レポート描画(JPEG) ----------

def score_color(score):
    if score is None:
        return COL_SUB
    if score >= 75:
        return COL_GOOD
    if score >= 50:
        return COL_WARN
    return COL_ACCENT


def render_report(date_str, condition, workouts, meal_total, evaluation, out_path):
    fig = plt.figure(figsize=(7.2, 11.0), dpi=150)
    fig.patch.set_facecolor(COL_BG)

    # タイトル
    fig.text(0.5, 0.965, "デイリー・ヘルスレポート", ha="center",
             fontsize=20, color=COL_TEXT, weight="bold")
    fig.text(0.5, 0.94, f"{date_str} の振り返り", ha="center",
             fontsize=12, color=COL_SUB)

    score = evaluation.get("score")

    # 総合スコアの円
    ax_score = fig.add_axes([0.30, 0.76, 0.40, 0.15])
    ax_score.set_xlim(0, 1)
    ax_score.set_ylim(0, 1)
    ax_score.axis("off")
    circ = plt.Circle((0.5, 0.5), 0.45, color=score_color(score), alpha=0.15)
    ax_score.add_patch(circ)
    ax_score.text(0.5, 0.55, f"{score if score is not None else '--'}",
                  ha="center", va="center", fontsize=40,
                  color=score_color(score), weight="bold")
    ax_score.text(0.5, 0.18, "総合スコア", ha="center", va="center",
                  fontsize=11, color=COL_SUB)

    # ヘッドライン
    fig.text(0.5, 0.735, evaluation.get("headline", ""), ha="center",
             fontsize=16, color=COL_ACCENT, weight="bold")

    # コンディション指標
    def card(y, h):
        ax = fig.add_axes([0.07, y, 0.86, h])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, color=COL_CARD,
                                   transform=ax.transAxes, zorder=0))
        return ax

    ax_cond = card(0.595, 0.115)
    ax_cond.text(0.03, 0.82, "コンディション (Oura)", fontsize=12,
                 color=COL_TEXT, weight="bold")
    c = condition or {}
    metrics = [
        ("睡眠スコア", c.get("睡眠スコア"), ""),
        ("レディネス", c.get("レディネス"), ""),
        ("HRV", c.get("HRV"), "ms"),
        ("安静時心拍", c.get("安静時心拍"), "bpm"),
        ("睡眠時間", c.get("睡眠時間"), "h"),
    ]
    for i, (label, val, unit) in enumerate(metrics):
        x = 0.03 + i * 0.195
        disp = f"{val}{unit}" if val is not None else "--"
        ax_cond.text(x + 0.085, 0.50, disp, ha="center", fontsize=15,
                     color=COL_TEXT, weight="bold")
        ax_cond.text(x + 0.085, 0.20, label, ha="center", fontsize=8.5,
                     color=COL_SUB)

    # ワークアウト
    ax_w = card(0.435, 0.135)
    ax_w.text(0.03, 0.86, "ワークアウト (Garmin)", fontsize=12,
              color=COL_TEXT, weight="bold")
    if workouts:
        lines = []
        for w in workouts:
            parts = [w.get("種類") or w.get("名前") or "運動"]
            if w.get("距離km"):
                parts.append(f"{w['距離km']}km")
            if w.get("ペース"):
                parts.append(w["ペース"])
            if w.get("セット数"):
                parts.append(f"{int(w['セット数'])}セット")
            if w.get("所要時間分"):
                parts.append(f"{int(w['所要時間分'])}分")
            if w.get("消費カロリー"):
                parts.append(f"{int(w['消費カロリー'])}kcal")
            lines.append("・" + "  ".join(parts))
        ax_w.text(0.03, 0.58, "\n".join(lines[:4]), fontsize=10.5,
                  color=COL_TEXT, va="top", linespacing=1.6)
    else:
        ax_w.text(0.03, 0.45, "記録された運動はありません(休養日)", fontsize=10.5,
                  color=COL_SUB, va="center")

    # 食事(PFC)
    ax_m = card(0.255, 0.155)
    ax_m.text(0.03, 0.88, "食事 (合計)", fontsize=12, color=COL_TEXT, weight="bold")
    kcal = meal_total.get("カロリー", 0)
    ax_m.text(0.03, 0.62, f"{int(kcal)} kcal", fontsize=20,
              color=COL_ACCENT, weight="bold", va="center")
    # PFCバー
    p = meal_total.get("タンパク質g", 0)
    f_ = meal_total.get("脂質g", 0)
    carb = meal_total.get("糖質g", 0)
    pfc = [("P タンパク質", p, COL_GOOD), ("F 脂質", f_, COL_WARN),
           ("C 糖質", carb, COL_ACCENT)]
    maxg = max([p, f_, carb, 1])
    for i, (label, g, col) in enumerate(pfc):
        y = 0.40 - i * 0.13
        ax_m.text(0.03, y, label, fontsize=9.5, color=COL_SUB, va="center")
        ax_m.add_patch(plt.Rectangle((0.30, y - 0.035), 0.55 * (g / maxg), 0.07,
                                     color=col, alpha=0.8))
        ax_m.text(0.87, y, f"{int(g)}g", fontsize=9.5, color=COL_TEXT, va="center")

    # 評価コメント + アドバイス
    ax_c = card(0.045, 0.19)
    ax_c.text(0.03, 0.90, "コーチからのひとこと", fontsize=12,
              color=COL_TEXT, weight="bold")
    comment = evaluation.get("comment", "")
    wrapped = _wrap(comment, 26)
    ax_c.text(0.03, 0.70, wrapped, fontsize=10.5, color=COL_TEXT,
              va="top", linespacing=1.7)
    ax_c.add_patch(plt.Rectangle((0.0, 0.0), 1, 0.26, color=COL_ACCENT, alpha=0.12))
    ax_c.text(0.03, 0.13, "今日のアクション", fontsize=9, color=COL_ACCENT,
              weight="bold", va="center")
    ax_c.text(0.30, 0.13, evaluation.get("advice", ""), fontsize=11,
              color=COL_TEXT, weight="bold", va="center")

    fig.savefig(out_path, format="jpeg", facecolor=COL_BG,
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)


def _wrap(text, width):
    out, line = [], ""
    for ch in text:
        line += ch
        if len(line) >= width and ch in "。、 ":
            out.append(line)
            line = ""
    if line:
        out.append(line)
    return "\n".join(out)


# ---------- メイン ----------

def build_line_text(date_str, evaluation):
    return (
        f"おはようございます☀\n"
        f"【{date_str} の振り返り】\n"
        f"総合スコア {evaluation.get('score', '--')} / {evaluation.get('headline', '')}\n\n"
        f"{evaluation.get('comment', '')}\n\n"
        f"▶ 今日のアクション: {evaluation.get('advice', '')}"
    )


def generate(date_str):
    """データ取得→評価→JPEG描画。LINE送信用テキストをファイルに書き出す。"""
    setup_japanese_font()
    os.makedirs(REPORTS_DIR, exist_ok=True)

    notion = get_notion_client()
    condition = fetch_condition(notion, date_str)
    workouts = fetch_workouts(notion, date_str)
    meals = fetch_meals(notion, date_str)
    meal_total = summarize_meals(meals)

    evaluation = generate_evaluation(date_str, condition, workouts, meal_total, meals)

    out_path = os.path.join(REPORTS_DIR, f"{date_str}.jpg")
    render_report(date_str, condition, workouts, meal_total, evaluation, out_path)
    print(f"レポートを生成しました: {out_path}")

    # send ステップが拾えるよう、LINE本文を一時ファイルに保存
    with open(os.path.join(REPORTS_DIR, "_message.txt"), "w", encoding="utf-8") as f:
        f.write(build_line_text(date_str, evaluation))


def send(date_str):
    """生成済みレポートを公開URL付きでLINEに送る(コミット/push後に実行する想定)。"""
    msg_path = os.path.join(REPORTS_DIR, "_message.txt")
    if os.path.exists(msg_path):
        with open(msg_path, encoding="utf-8") as f:
            text = f.read()
    else:
        text = f"【{date_str}】レポートを確認してください。"

    base = os.environ.get("REPORT_PUBLIC_BASE_URL", "").rstrip("/")
    image_url = f"{base}/{date_str}.jpg" if base else None

    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_USER_ID"):
        from line_notify import push_text_and_image
        push_text_and_image(text, image_url)
        print("LINEに送信しました。" + ("(画像あり)" if image_url else "(画像URL未設定のためテキストのみ)"))
    else:
        print("LINEの認証情報が未設定のため送信をスキップしました。")
        # Windowsコンソール(cp932)で表示できない文字があっても落ちないようにする
        safe = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"
        )
        print(safe)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    date_str = yesterday_jst().isoformat()

    if mode == "generate":
        generate(date_str)
    elif mode == "send":
        send(date_str)
    else:
        # ローカル確認用: 生成して(公開URLなしで)送信まで通す
        generate(date_str)
        send(date_str)


if __name__ == "__main__":
    main()
