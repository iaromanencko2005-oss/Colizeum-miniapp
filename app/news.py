"""
Новости клуба с реакциями гостей.

Админ публикует новость в боте: фото с подписью (или текстом)
  /news Заголовок | Текст новости
Гости видят её на главной приложения и отвечают одной кнопкой: «Круто, жду» или
«Не интересно» (передумать можно). Админ видит итоги в /news_list.
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import ClubNews, NewsReaction, Client

MSK_OFFSET_H = 3
TITLE_MAX, BODY_MAX = 90, 1500


class NewsError(Exception):
    pass


def parse_news(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise NewsError("Формат: /news Заголовок | Текст новости")
    title, _, body = raw.partition("|")
    title, body = re.sub(r"\s+", " ", title).strip(), body.strip()
    if len(title) < 3:
        raise NewsError("Нужен заголовок — хотя бы 3 символа")
    if len(title) > TITLE_MAX:
        raise NewsError(f"Заголовок длинноват: до {TITLE_MAX} символов (сейчас {len(title)})")
    if len(body) > BODY_MAX:
        raise NewsError(f"Текст до {BODY_MAX} символов (сейчас {len(body)})")
    return {"title": title, "body": body}


def counts(db, ids: list[int]) -> dict:
    """{news_id: (рады, не_интересно)}"""
    out = {i: [0, 0] for i in ids}
    if not ids:
        return out
    for nid, val, n in (db.query(NewsReaction.news_id, NewsReaction.value, func.count(NewsReaction.client_id))
                        .filter(NewsReaction.news_id.in_(ids)).group_by(NewsReaction.news_id, NewsReaction.value).all()):
        out[nid][0 if val > 0 else 1] += int(n)
    return out


def _date(dt: datetime) -> str:
    return f"{dt + timedelta(hours=MSK_OFFSET_H):%d.%m}"


def news_list(db, me: Client | None, limit: int = 20) -> list[dict]:
    items = (db.query(ClubNews).filter(ClubNews.status == "active")
             .order_by(ClubNews.created_at.desc()).limit(limit).all())
    ids = [n.id for n in items]
    cnt = counts(db, ids)
    mine = {}
    if me is not None and ids:
        mine = dict(db.query(NewsReaction.news_id, NewsReaction.value)
                    .filter(NewsReaction.client_id == me.id, NewsReaction.news_id.in_(ids)).all())
    return [{"id": n.id, "title": n.title, "body": n.body or "", "date": _date(n.created_at),
             "img": f"/api/news/img/{n.id}?v={n.image_ver}" if n.image else None,
             "likes": cnt[n.id][0], "dislikes": cnt[n.id][1], "mine": int(mine.get(n.id, 0))} for n in items]


def react(db, client: Client, news_id: int, value: int) -> dict:
    n = db.get(ClubNews, news_id)
    if n is None or n.status != "active":
        raise NewsError("Эта новость уже снята")
    value = 1 if value > 0 else -1
    r = db.get(NewsReaction, (news_id, client.id))
    if r is None:
        db.add(NewsReaction(news_id=news_id, client_id=client.id, value=value))
    else:
        r.value, r.created_at = value, datetime.utcnow()
    db.commit()
    likes, dislikes = counts(db, [news_id])[news_id]
    return {"id": news_id, "likes": likes, "dislikes": dislikes, "mine": value}


def summary_line(likes: int, dislikes: int) -> str:
    total = likes + dislikes
    if not total:
        return "Реакций пока нет"
    return f"Рады: {likes} ({round(likes * 100 / total)}%) · Не интересно: {dislikes} ({round(dislikes * 100 / total)}%)"
