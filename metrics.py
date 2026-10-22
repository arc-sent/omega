"""Метрика VK-групп: снимки числа подписчиков и отчёт о росте (/metrics, только админ).

Отчёт отдаётся PDF-файлом (build_pdf): сводка и общий график, таблица всех групп,
мини-графики по каждой группе. Рисуется matplotlib (PdfPages) — без доп. зависимостей.

Снимок — число подписчиков каждой группы всех пользователей на момент вызова
(groups.getById, fields=members_count). Снимки пишутся в db.group_metrics:
раз в сутки джобом (METRICS_HOUR:METRICS_MINUTE МСК) и при каждом /metrics.
Прирост за N дней = текущее − последний снимок не позже «сейчас − N дней»; если
такого ещё нет (отслеживание началось позже), берётся первый снимок группы и
прирост помечается «*» — «с начала отслеживания».
"""

import logging
import time
from datetime import datetime, timedelta
from io import BytesIO

import pytz

import db
from vk import VKError, fetch_groups_members

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

DAY = 86400
DELTA_PERIODS = (1, 7, 30)  # за сколько дней показывать прирост
CHART_PERIODS = (7, 30, 90)


# ─── Сбор снимка ──────────────────────────────────────────────────────────────

def _group_candidates() -> tuple[dict[int, list[int]], dict[int, str]]:
    """{vk_group_id: [account_id, ...]} и {vk_group_id: имя из бота}.

    Одна VK-группа может быть у нескольких пользователей — запрашиваем её один раз,
    а их аккаунты служат запасными токенами, если первый не сработает.
    """
    cands: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for g in db.get_all_groups_for_metrics():
        gid = abs(int(g["vk_group_id"]))
        names.setdefault(gid, g["name"])
        lst = cands.setdefault(gid, [])
        if g["account_id"] is not None and g["account_id"] not in lst:
            lst.append(g["account_id"])
    return cands, names


def collect_snapshot() -> dict:
    """Снять число подписчиков всех групп и записать в базу (блокирующий вызов).

    Группы раскладываются по токенам их владельцев; если запрос с токеном упал,
    группы пробуются со следующим аккаунтом из кандидатов. Возвращает
    {"saved": N, "total": M, "failed": [vk_group_id, ...]}.
    """
    cands, names = _group_candidates()
    tokens: dict[int, str | None] = {}
    got: dict[int, tuple[int, str]] = {}
    pending = {gid: list(accs) for gid, accs in cands.items()}
    while pending:
        by_account: dict[int, list[int]] = {}
        for gid, accs in list(pending.items()):
            if not accs:
                continue
            by_account.setdefault(accs.pop(0), []).append(gid)
        if not by_account:
            break  # у оставшихся групп кончились аккаунты
        for aid, gids in by_account.items():
            if aid not in tokens:
                tokens[aid] = db.get_vk_token_by_account(aid)
            token = tokens[aid]
            if not token:
                continue
            try:
                res = fetch_groups_members(token, gids)
            except VKError as exc:
                logger.warning("Метрика: аккаунт %s, %s групп — %s", aid, len(gids), exc)
                continue
            for gid in gids:
                count, name = res.get(gid, (None, ""))
                if count is not None:
                    got[gid] = (count, name or names.get(gid, ""))
                    pending.pop(gid, None)
        pending = {gid: accs for gid, accs in pending.items() if accs}

    db.add_group_metrics([(gid, cnt, name) for gid, (cnt, name) in got.items()])
    failed = sorted(set(cands) - set(got))
    logger.info("Метрика: снято %s из %s групп, не удалось: %s", len(got), len(cands), failed)
    return {"saved": len(got), "total": len(cands), "failed": failed}


def last_snapshot_age(now: int | None = None) -> int | None:
    """Сколько секунд назад был последний снимок любой группы (None — ни одного)."""
    now = int(now if now is not None else time.time())
    with db._connect() as conn:
        row = conn.execute("SELECT MAX(captured_at) AS t FROM group_metrics").fetchone()
    return now - row["t"] if row and row["t"] else None


# ─── Отчёт ────────────────────────────────────────────────────────────────────

def _delta(gid: int, current: int, now: int, days: int) -> tuple[int | None, bool]:
    """(прирост, приблизительно?) за days дней. None — сравнить не с чем."""
    base = db.get_latest_metric_before(gid, now - days * DAY)
    if base is not None:
        return current - base["members_count"], False
    first = db.get_first_metric(gid)
    if first is None or now - first["captured_at"] < 3600:
        return None, False  # отслеживание только началось
    return current - first["members_count"], True


def _fmt_delta(value: int | None, approx: bool) -> str:
    if value is None:
        return "—"
    return f"{value:+d}" + ("*" if approx else "")


def report_data(now: int | None = None) -> dict:
    """Данные отчёта: строки по группам (крупные сверху), итоги, группы без данных."""
    now = int(now if now is not None else time.time())
    cands, names = _group_candidates()
    rows, missing = [], []
    for gid in cands:
        latest = db.get_latest_metric_before(gid, now)
        if latest is None:
            missing.append(gid)
            continue
        cur = latest["members_count"]
        rows.append({
            "gid": gid,
            "name": names.get(gid) or str(gid),
            "members": cur,
            "deltas": {d: _delta(gid, cur, now, d) for d in DELTA_PERIODS},
            "posts": db.count_published_to_vk_group(gid, now - 7 * DAY, now + 1),
        })
    rows.sort(key=lambda r: r["members"], reverse=True)
    totals = {}
    for d in DELTA_PERIODS:
        vals = [r["deltas"][d][0] for r in rows if r["deltas"][d][0] is not None]
        totals[d] = (sum(vals) if vals else None, any(r["deltas"][d][1] for r in rows))
    return {
        "now": now, "names": names, "groups": len(cands), "rows": rows, "missing": missing,
        "total": sum(r["members"] for r in rows), "totals": totals,
        "has_approx": any(r["deltas"][d][1] for r in rows for d in DELTA_PERIODS),
    }


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def summary_lines(data: dict) -> list[str]:
    """Шапка отчёта: дата, число групп, подписчики, прирост (без эмодзи — идёт и в PDF)."""
    totals = " · ".join(f"{d} дн: {_fmt_delta(*data['totals'][d])}" for d in DELTA_PERIODS)
    return [
        f"Метрика групп · {datetime.fromtimestamp(data['now'], MOSCOW_TZ):%d.%m.%Y %H:%M} МСК",
        f"Групп: {data['groups']} · подписчиков всего: {_fmt_int(data['total'])}",
        f"Прирост · {totals}",
    ]


def build_report(now: int | None = None) -> list[str]:
    """Текст отчёта по всем группам (список строк; самые большие группы сверху)."""
    data = report_data(now)
    if not data["groups"]:
        return ["Пока ни у одного пользователя нет групп."]
    names = data["names"]
    head = summary_lines(data)
    lines = ["📊 " + head[0], *head[1:], ""]
    for i, r in enumerate(data["rows"], 1):
        d = " / ".join(_fmt_delta(*r["deltas"][p]) for p in DELTA_PERIODS)
        lines.append(f"{i}. {r['name']} (id {r['gid']})")
        lines.append(f"    👥 {_fmt_int(r['members'])} · Δ 1/7/30 дн: {d} · постов за 7 дн: {r['posts']}")
    if data["missing"]:
        lines.append("")
        lines.append("⚠️ Нет данных (VK не отдал число подписчиков или нет токена): "
                     + ", ".join(f"{names.get(g) or g} (id {g})" for g in data["missing"]))
    if data["has_approx"]:
        lines.append("")
        lines.append("* — с начала отслеживания (данных за весь период ещё нет).")
    return lines


def split_messages(lines: list[str], limit: int = 4000) -> list[str]:
    """Разбить строки на сообщения не длиннее лимита Telegram."""
    out, buf = [], ""
    for line in lines:
        if buf and len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


# ─── График ───────────────────────────────────────────────────────────────────

def daily_series(days: int, now: int | None = None,
                 group_ids: list[int] | None = None) -> list[tuple[datetime, int, int]]:
    """(день, подписчиков на конец дня, прирост за день) за последние days дней.

    На каждый день берётся последний снимок группы не позже конца дня (дни без
    снимка заполняются предыдущим значением). Сумма включает только группы, у
    которых снимок уже есть, поэтому добавление группы даёт скачок суммы. Прирост
    за день считается «как есть к как есть» — только по группам, у которых есть
    значения и на этот, и на предыдущий день, — так новые группы его не искажают.
    """
    now = int(now if now is not None else time.time())
    if group_ids is None:
        group_ids = list(_group_candidates()[0])
    history = {gid: db.get_group_metrics(gid) for gid in group_ids}
    today = datetime.fromtimestamp(now, MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    prev: dict[int, int] = {}
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        end_ts = min(int((day + timedelta(days=1)).timestamp()), now + 1)
        cur: dict[int, int] = {}
        for gid, snaps in history.items():
            val = None
            for s in snaps:
                if s["captured_at"] < end_ts:
                    val = s["members_count"]
                else:
                    break
            if val is not None:
                cur[gid] = val
        if cur:
            growth = sum(v - prev[g] for g, v in cur.items() if g in prev) if result else 0
            result.append((day, sum(cur.values()), growth))
        prev = cur
    return result


def daily_totals(days: int, now: int | None = None,
                 group_ids: list[int] | None = None) -> list[tuple[datetime, int]]:
    """Суммарное число подписчиков на конец каждого дня (см. daily_series)."""
    return [(d, total) for d, total, _ in daily_series(days, now, group_ids)]


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _draw_growth(ax1, ax2, points: list[tuple[datetime, int, int]], title: str) -> None:
    """Подписчики (линия, ax1) и прирост по дням (столбцы, ax2); points — daily_series."""
    labels = [d.strftime("%d.%m") for d, _, _ in points]
    values = [v for _, v, _ in points]
    growth = [g for _, _, g in points]
    x = range(len(labels))
    ax1.plot(x, values, color="#5b9bd5", marker="o", markersize=3, linewidth=2)
    ax1.set_title(title, fontsize=12, fontweight="bold")
    ax1.set_ylabel("Подписчиков")
    ax1.grid(linestyle="--", alpha=0.5)
    ax1.ticklabel_format(axis="y", style="plain", useOffset=False)

    ax2.bar(x, growth, color=["#70ad47" if g >= 0 else "#e15759" for g in growth], width=0.6)
    ax2.axhline(0, color="#888", linewidth=0.8)
    ax2.set_ylabel("Прирост за день")
    ax2.grid(axis="y", linestyle="--", alpha=0.5)
    step = max(1, len(labels) // 15)
    ax2.set_xticks(list(x)[::step])
    ax2.set_xticklabels(labels[::step], rotation=45 if len(labels) > 10 else 0,
                        ha="right" if len(labels) > 10 else "center", fontsize=9)
    for ax in (ax1, ax2):
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)


def build_chart(days: int, now: int | None = None, group_id: int | None = None) -> bytes | None:
    """PNG: подписчики (линия) и прирост по дням (столбцы). None — мало данных."""
    points = daily_series(days, now, [group_id] if group_id else None)
    if len(points) < 2:
        return None
    if group_id:
        _, names = _group_candidates()
        title = f"{names.get(group_id) or group_id}: подписчики за {days} дн"
    else:
        title = f"Все группы: подписчики за {days} дн"

    plt = _plt()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    _draw_growth(ax1, ax2, points, title)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ─── PDF-отчёт ────────────────────────────────────────────────────────────────

A4_LANDSCAPE = (11.69, 8.27)
TABLE_ROWS_PER_PAGE = 22
GROUP_CHARTS_PER_PAGE = 9  # сетка 3×3


def _delta_color(value: int | None) -> str:
    if value is None or value == 0:
        return "#333333"
    return "#2e7d32" if value > 0 else "#c62828"


def _pdf_title_page(plt, data: dict, chart_days: int):
    fig = plt.figure(figsize=A4_LANDSCAPE)
    head = summary_lines(data)
    fig.text(0.05, 0.94, head[0], fontsize=18, fontweight="bold")
    fig.text(0.05, 0.895, head[1], fontsize=12)
    fig.text(0.05, 0.86, head[2], fontsize=12)
    points = daily_series(chart_days, data["now"])
    if len(points) >= 2:
        ax1 = fig.add_axes([0.08, 0.40, 0.88, 0.40])
        ax2 = fig.add_axes([0.08, 0.15, 0.88, 0.20], sharex=ax1)
        _draw_growth(ax1, ax2, points, f"Все группы: подписчики за {chart_days} дн")
        plt.setp(ax1.get_xticklabels(), visible=False)
        fig.text(0.08, 0.085, "Прирост за день считается только по группам, отслеживаемым оба дня; "
                              "скачок линии в день добавления новой группы — не рост подписчиков.",
                 fontsize=8, color="#666666")
    else:
        fig.text(0.05, 0.75, "График роста появится, когда накопятся снимки хотя бы за 2 дня "
                             "(снимок делается каждый день и при каждом /metrics).",
                 fontsize=11, color="#666666")
    notes = []
    if data["missing"]:
        notes.append("Нет данных (VK не отдал число подписчиков или нет токена): "
                     + ", ".join(f"{data['names'].get(g) or g} (id {g})" for g in data["missing"]))
    if data["has_approx"]:
        notes.append("* — прирост с начала отслеживания (данных за весь период ещё нет).")
    for i, note in enumerate(notes):
        fig.text(0.05, 0.05 - i * 0.025, note[:220], fontsize=8, color="#666666")
    return fig


def _pdf_table_pages(plt, data: dict):
    cols = ["#", "Группа", "ID", "Подписчики"] + [f"Δ {d} дн" for d in DELTA_PERIODS] + ["Постов 7 дн"]
    widths = [0.04, 0.36, 0.11, 0.11] + [0.09] * len(DELTA_PERIODS) + [0.11]
    rows = data["rows"]
    pages = max(1, -(-len(rows) // TABLE_ROWS_PER_PAGE))
    for page in range(pages):
        chunk = rows[page * TABLE_ROWS_PER_PAGE:(page + 1) * TABLE_ROWS_PER_PAGE]
        fig = plt.figure(figsize=A4_LANDSCAPE)
        fig.text(0.05, 0.94, f"Группы ({page + 1}/{pages})", fontsize=14, fontweight="bold")
        ax = fig.add_axes([0.05, 0.05, 0.9, 0.85])
        ax.axis("off")
        if not chunk:
            ax.text(0, 1, "Нет данных ни по одной группе.", va="top", fontsize=11)
            yield fig
            continue
        cells, colors = [], []
        for i, r in enumerate(chunk, page * TABLE_ROWS_PER_PAGE + 1):
            name = r["name"] if len(r["name"]) <= 45 else r["name"][:44] + "…"
            cells.append([str(i), name, str(r["gid"]), _fmt_int(r["members"])]
                         + [_fmt_delta(*r["deltas"][d]) for d in DELTA_PERIODS] + [str(r["posts"])])
            colors.append([_delta_color(r["deltas"][d][0]) for d in DELTA_PERIODS])
        table = ax.table(cellText=cells, colLabels=cols, colWidths=widths, loc="upper center",
                         cellLoc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.45)
        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("#dddddd")
            if row == 0:
                cell.set_facecolor("#5b9bd5")
                cell.set_text_props(color="white", fontweight="bold")
                continue
            if row % 2 == 0:
                cell.set_facecolor("#f5f8fc")
            if col == 1:
                cell.set_text_props(ha="left")
                cell._loc = "left"
            if 4 <= col < 4 + len(DELTA_PERIODS):
                cell.set_text_props(color=colors[row - 1][col - 4])
        yield fig


def _pdf_group_pages(plt, data: dict, chart_days: int):
    rows = data["rows"]
    for start in range(0, len(rows), GROUP_CHARTS_PER_PAGE):
        chunk = rows[start:start + GROUP_CHARTS_PER_PAGE]
        fig, axes = plt.subplots(3, 3, figsize=A4_LANDSCAPE)
        fig.suptitle(f"Динамика по группам за {chart_days} дн", fontsize=14, fontweight="bold")
        for ax, r in zip(axes.flat, chunk):
            name = r["name"] if len(r["name"]) <= 32 else r["name"][:31] + "…"
            ax.set_title(f"{name}\n{_fmt_int(r['members'])} · Δ7: {_fmt_delta(*r['deltas'][7])}",
                         fontsize=9)
            points = daily_totals(chart_days, data["now"], [r["gid"]])
            if len(points) < 2:
                ax.text(0.5, 0.5, "мало данных", ha="center", va="center",
                        transform=ax.transAxes, color="#888888")
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            values = [v for _, v in points]
            x = range(len(values))
            color = "#2e7d32" if values[-1] >= values[0] else "#c62828"
            ax.plot(x, values, color=color, linewidth=1.8)
            ax.fill_between(x, values, min(values), color=color, alpha=0.08)
            step = max(1, len(points) // 4)
            ax.set_xticks(list(x)[::step])
            ax.set_xticklabels([d.strftime("%d.%m") for d, _ in points][::step], fontsize=7)
            ax.tick_params(axis="y", labelsize=7)
            ax.ticklabel_format(axis="y", style="plain", useOffset=False)
            ax.grid(linestyle="--", alpha=0.4)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        for ax in list(axes.flat)[len(chunk):]:
            ax.axis("off")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        yield fig


def build_pdf(now: int | None = None, chart_days: int = 30) -> bytes | None:
    """PDF-отчёт: сводка + общий график, таблица всех групп, мини-графики по группам.

    None — если групп нет совсем.
    """
    data = report_data(now)
    if not data["groups"]:
        return None
    plt = _plt()
    from matplotlib.backends.backend_pdf import PdfPages

    buf = BytesIO()
    with PdfPages(buf, metadata={"Title": "Метрика групп VK", "Author": "baideromega"}) as pdf:
        for fig in (_pdf_title_page(plt, data, chart_days),
                    *_pdf_table_pages(plt, data),
                    *_pdf_group_pages(plt, data, chart_days)):
            pdf.savefig(fig)
            plt.close(fig)
    buf.seek(0)
    return buf.read()


def pdf_filename(now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    return f"metrics_{datetime.fromtimestamp(now, MOSCOW_TZ):%Y-%m-%d_%H-%M}.pdf"
