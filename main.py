import asyncio
import calendar
import html
import json
import logging
import random
import re
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import database as db
import rituals

TZ = ZoneInfo(config.TIMEZONE)


def _today():
    return datetime.now(TZ).date()

router = Router()


class SQLiteStorage(BaseStorage):
    """Хранилище состояний FSM прямо в webinars.db — в отличие от штатного
    MemoryStorage (у которого в документации честно написано "не для
    продакшена, все данные теряются при перезапуске"), переживает любой
    перезапуск бота. Обнаружено 2026-09-06 на реальном случае: человек
    начинал что-то посреди диалога (писал намерение, оформлял оплату,
    редактировал текст в панели), в этот момент бот перезапускался ради
    несвязанной правки - и следующее сообщение человека просто не попадало
    ни в один обработчик и терялось молча, без единого сообщения кому-либо."""

    @staticmethod
    def _key(key: StorageKey) -> str:
        parts = [str(key.bot_id), str(key.chat_id), str(key.user_id)]
        if key.thread_id is not None:
            parts.append(f"t{key.thread_id}")
        if key.business_connection_id is not None:
            parts.append(f"b{key.business_connection_id}")
        if key.destiny != "default":
            parts.append(f"d{key.destiny}")
        return ":".join(parts)

    async def set_state(self, key: StorageKey, state=None) -> None:
        db.fsm_set_state(self._key(key), state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey):
        return db.fsm_get_state(self._key(key))

    async def set_data(self, key: StorageKey, data) -> None:
        db.fsm_set_data(self._key(key), dict(data))

    async def get_data(self, key: StorageKey) -> dict:
        return db.fsm_get_data(self._key(key))

    async def close(self) -> None:
        pass


# общее хранилище FSM-состояний — на уровне модуля, а не только внутри main(),
# потому что иногда нужно установить состояние КОНКРЕТНОМУ человеку не из его
# собственного апдейта (например, пригласить его на ритуал-намерение сразу
# после того, как АДМИН подтвердил его оплату — см. _invite_intention_ritual)
fsm_storage = SQLiteStorage()


async def _block_guard(handler, event, data):
    user = event.from_user
    if user and db.is_user_blocked(user.id) and not db.is_admin(user.id):
        return  # молча игнорируем — заблокированный человек не получит никакого ответа
    return await handler(event, data)


async def _unknown_user_guard(handler, event, data):
    """Если человека нет в базе (никогда не было, или был удалён) и он
    прислал что-то, кроме /start — например, нажал старую кнопку меню,
    оставшуюся в его Telegram с прошлого раза — бот сам подсказывает
    нажать /start, вместо того чтобы промолчать или повести себя странно."""
    user = event.from_user
    text = event.text or ""
    if user and not text.startswith("/start") and not db.get_user(user.id):
        await event.answer(
            "Чтобы начать (или начать заново), напишите /start 🙏",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    return await handler(event, data)


router.message.outer_middleware(_block_guard)
router.message.outer_middleware(_unknown_user_guard)
router.callback_query.outer_middleware(_block_guard)

BTN_WEBINARS = "📅 Вебинары, Практики, Расстановки"
BTN_SANCTUM = "⚜️ Сакральный канал VEDA SANCTUM"
BTN_MEDITATION = "🧘🏽‍♀️ VEDA HEALING FLOW"
BTN_PROFILE = "✨ Мой профиль в VEDAME SPACE"
BTN_ABOUT = "💠 Философия Alena Veda"
BTN_FEED = "📖 Архив публикаций пространства"
BTN_INFO = "❓ Инфо/Кодекс пространства"
BTN_ADMIN = "⚙️ Админ-панель"

SANCTUM_FULL_NAME = "VEDA SANCTUM | CODEofGOD"

FIELD_LABELS = {
    "title": "название",
    "description": "описание",
    "date_text": "дата и время",
    "price": "цена",
    "invite_link": "ссылка",
    "intro_text": "текст-приглашение (сообщение 1, до кнопки «Войти в глубину»)",
    "laws_text": "текст с законами (сообщение 2, до кнопки «Инициировать шаг»; можно вставить {price} - подставится цена этого человека)",
}

CODE_TO_FIELD = {"t": "title", "d": "description", "dt": "date_text", "p": "price", "l": "invite_link"}

# тип записи в разделе "Вебинары, Практики, Расстановки" — используется в
# автоматических напоминаниях, чтобы подставить правильное слово вместо {тип}
EVENT_TYPE_WORDS = {"webinar": "вебинар", "practice": "практика", "constellation": "расстановка"}
EVENT_TYPE_LABELS = {"webinar": "Вебинар", "practice": "Практика", "constellation": "Расстановка"}

_RU_MONTHS_GENITIVE = {
    "01": "января", "02": "февраля", "03": "марта", "04": "апреля",
    "05": "мая", "06": "июня", "07": "июля", "08": "августа",
    "09": "сентября", "10": "октября", "11": "ноября", "12": "декабря",
}


def _format_event_dt(dt: datetime) -> str:
    """"15 сентября, 18:00 по Киеву" — единый формат отображения даты/времени
    вебинара, всегда вычисляется из настоящей даты (event_dt), а не вводится
    отдельным свободным текстом — чтобы напоминания и карточка не могли разъехаться."""
    month = _RU_MONTHS_GENITIVE.get(f"{dt.month:02d}", str(dt.month))
    return f"{dt.day} {month}, {dt.strftime('%H:%M')} по Киеву"


def _event_type_kb(callback_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{callback_prefix}{code}")]
        for code, label in EVENT_TYPE_LABELS.items()
    ])

SEGMENT_LABELS = {
    "all": "👥 Все подписчики бота",
    "sanctum_active": "⚜️ VEDA SANCTUM - активные",
    "sanctum_expired": "🕰 VEDA SANCTUM - истёкшие",
    "sanctum_removed": "🚫 VEDA SANCTUM - удалённые",
    "sanctum_never": "🌱 Никогда не были в VEDA SANCTUM",
    "webinar_attendees": "📅 Были на вебинарах",
    "never_purchased": "💤 Ничего не покупали",
    "unfinished_payment": "⏳ Не завершили оплату",
}


def _segment_user_ids(segment: str):
    today_iso = _today().isoformat()
    if segment == "sanctum_active":
        return db.get_sanctum_active_user_ids(today_iso)
    elif segment == "sanctum_expired":
        return db.get_sanctum_expired_user_ids(today_iso)
    elif segment == "sanctum_removed":
        return db.get_sanctum_removed_user_ids()
    elif segment == "sanctum_never":
        return db.get_sanctum_never_user_ids()
    elif segment == "webinar_attendees":
        return db.get_webinar_attendee_user_ids()
    elif segment == "never_purchased":
        return db.get_never_purchased_user_ids()
    elif segment == "unfinished_payment":
        return db.get_unfinished_payment_user_ids()
    return db.get_all_user_ids()


def _payment_block(purpose_key: str) -> str:
    # payment_requisites и payment_purpose_* — доверенные HTML-поля (см. HTML_TRUSTED_FIELDS):
    # сохраняются через message.html_text, поэтому здесь НЕ экранируем — иначе жирный
    # шрифт и ссылки, вставленные прямо в Telegram, показались бы как сырой текст.
    card = db.get_setting("payment_requisites")
    purpose = db.get_setting(purpose_key)
    return f"💳 Реквизиты:\n{card}\n\n📝 Назначение платежа:\n{purpose}"


def _payment_ready(purpose_key: str) -> bool:
    return (
        db.get_setting("payment_requisites") != db.PLACEHOLDER_PAYMENT_REQUISITES
        and db.get_setting(purpose_key) != db.PLACEHOLDER_PAYMENT_PURPOSE
    )


NOT_READY_MESSAGE = "Извините, регистрация сюда пока недоступна. Загляните чуть позже 🙏"


def _end_of_month(d):
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day)


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _ru_days(n: int) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        word = "дней"
    else:
        last = n_abs % 10
        if last == 1:
            word = "день"
        elif 2 <= last <= 4:
            word = "дня"
        else:
            word = "дней"
    return f"{n} {word}"


def _ru_human_word(n: int) -> str:
    """Правильное окончание «человек» в оборотах вида «для N человек» —
    здесь предлог «для» ставит ВСЁ числительное в родительный падеж, а это
    другой паттерн, чем у _ru_days («два дня»/«пять дней» — именительный
    контекст, там 2-4 требуют родительного падежа ЕДИНСТВЕННОГО числа).
    В родительном падеже, управляемом предлогом, только «один» склоняется
    как прилагательное и держит единственное число («одного человека»,
    «двадцати одного человека») — «двух/трёх/четырёх/пяти... человек»
    требуют родительного падежа МНОЖЕСТВЕННОГО числа («для двух рублей»,
    не «для двух рубля»). Поймано и исправлено 2026-09-06 — первая версия
    ошибочно скопировала паттерн _ru_days, не учтя разницу падежа."""
    n_abs = abs(n) % 100
    last = n_abs % 10
    if last == 1 and n_abs != 11:
        return "человека"
    return "человек"


async def _require_text(message: Message):
    """Возвращает текст сообщения, или сама отвечает "нужен текст" и
    возвращает None, если пришло фото/стикер/что угодно нетекстовое —
    защищает поля в базе от записи пустого/None значения."""
    if not message.text:
        await message.answer("Пришлите, пожалуйста, обычным текстом 🙏")
        return None
    return message.text


async def _require_html_text(message: Message):
    """Как _require_text, но сохраняет форматирование (жирный/курсив/подчёркнутый/
    ссылки), которое вы выделяете прямо в Telegram при вводе — для полей,
    которые потом показываются людям с сохранением этого форматирования."""
    if not message.text:
        await message.answer("Пришлите, пожалуйста, обычным текстом 🙏")
        return None
    return message.html_text


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(text: str) -> str:
    """Убирает HTML-теги форматирования (<b>, <a href=...> и т.п.) — нужно
    для мест, где Telegram физически не умеет показывать форматирование
    (например, текст на кнопке), чтобы там не отображались сырые теги."""
    return _HTML_TAG_RE.sub("", text or "")


def _protect_for(user_id: int) -> bool:
    """Защиту от копирования включаем только обычным людям — админам (вам)
    тексты всегда остаются копируемыми, чтобы можно было спокойно работать
    с ботом и проверять его, не теряя доступ к своим же текстам."""
    return not db.is_admin(user_id)


def _int_setting(key: str, default: int) -> int:
    try:
        n = int(db.get_setting(key) or default)
        return n if n > 0 else default
    except ValueError:
        return default


def _lapse_anchor(membership):
    """Дата, от которой считается отсутствие человека в Sanctum: у убранного
    вручную - день, когда её убрали (removed_at), у просто не продлившего -
    день окончания доступа (valid_until). None, если считать не от чего."""
    if not membership:
        return None
    valid_until = _parse_date(membership["valid_until"]) if membership["valid_until"] else None
    if membership["status"] == "removed":
        removed_at = _parse_date(membership["removed_at"]) if membership["removed_at"] else None
        return removed_at or valid_until
    return valid_until


def _is_lapsed(membership) -> bool:
    """Человек сейчас НЕ в Sanctum: убран вручную, либо доступ уже закончился."""
    anchor = _lapse_anchor(membership)
    if anchor is None:
        return False
    if membership["status"] == "removed":
        return True
    return anchor < _today()


def _price_lock_deadline(membership):
    """Последний день, когда за ушедшим ещё сохраняется его прежняя цена
    (price_lock_days после дня отсутствия), либо None, если человек сейчас в
    Sanctum. Единая дата на весь случай ухода - и для текстов, и для цены."""
    if not _is_lapsed(membership):
        return None
    return _lapse_anchor(membership) + timedelta(days=_int_setting("price_lock_days", 30))


def _price_lock_expired(membership) -> bool:
    deadline = _price_lock_deadline(membership)
    return deadline is not None and _today() > deadline


def _price_for_user(user_id: int) -> str:
    """Цена ДЛЯ ЭТОГО человека: пока он в Sanctum, или недавно ушёл (в пределах
    price_lock_days) - его личная закреплённая цена; если ушёл дольше или
    подписки никогда не было - текущая базовая цена (как для новых)."""
    membership = db.get_sanctum_membership(user_id)
    if membership and membership["price"] and not _price_lock_expired(membership):
        return membership["price"]
    return db.get_sanctum()["price"]


def _fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y")


def _lapse_text(template: str, user_row, expired_date, deadline, price: str) -> str:
    """Подставляет в текст ухода/возврата имя ({имя}), дату окончания ({дата}),
    последний день сохранения цены ({дата_до}) и цену ({цена})."""
    text = _personalize(template or "", user_row)
    return (
        text.replace("{дата}", _fmt_date(expired_date))
        .replace("{дата_до}", _fmt_date(deadline))
        .replace("{цена}", price)
    )


def _return_kb(with_promise: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Желаю войти вновь", callback_data="sanctum_apply")]]
    if with_promise:
        rows.append([InlineKeyboardButton(text="⏰ Оплачу позже - назначить дату", callback_data="sanctum_promise")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _promise_active(membership) -> bool:
    """Человек сам отметил в боте, что оплатит позже, и назначенная дата ещё
    не прошла - пока так, не беспокоим его возвратными письмами."""
    if not membership or not membership["promise_date"]:
        return False
    promise = _parse_date(membership["promise_date"])
    return bool(promise and promise >= _today())


def _resolve_price(user_id: int, explicit_price):
    if explicit_price:
        return explicit_price
    return _price_for_user(user_id)


def _extend_sanctum_membership(user_id: int, price=None):
    """Оплата покрывает до конца месяца: если текущий период ещё активен —
    до конца СЛЕДУЮЩЕГО месяца (продление), если истёк или подписки не было —
    до конца ТЕКУЩЕГО месяца (человек просто "догнал" оплату сейчас).
    Цена: если передана явно (например, из заявки) — берём её, иначе —
    сохраняем ту, что уже закреплена за человеком, а если её никогда не было —
    текущую базовую."""
    today = _today()
    membership = db.get_sanctum_membership(user_id)
    current_valid_until = _parse_date(membership["valid_until"]) if membership else None

    if current_valid_until and current_valid_until >= today:
        base = current_valid_until + timedelta(days=1)
    else:
        base = today

    new_valid_until = _end_of_month(base)
    resolved_price = _resolve_price(user_id, price)
    db.upsert_sanctum_membership(user_id, new_valid_until.isoformat(), resolved_price)
    return new_valid_until, resolved_price


# ---------- Путь Восхождения + Орден Люминаров ----------

ASCENSION_LEVEL_NAMES = {
    1: "Первое Касание",
    2: "Искра",
    3: "Исследователь Глубины",
}

# (порог по числу реально вошедших-оплативших приглашённых, ранг) — проверяются
# от старшего к младшему, первый подошедший порог и есть текущий ранг
LUMINAR_THRESHOLDS = [(30, 3), (10, 2), (5, 1)]

LUMINAR_LABELS = {
    1: "Орден: Люминар I",
    2: "Орден: Люминар II",
    3: "Орден: Люминар III",
}

LUMINAR_RANK_NAMES = {1: "Люминар I", 2: "Люминар II", 3: "Люминар III"}

# её формулировки даров, 2026-08-31 (II пересмотрен с 30% на 50%) — только
# I выдаётся автоматически (см. _credit_luminar_referral), II и III она дарит
# лично, бот только описывает дар и сообщает ей о новом ранге
LUMINAR_GIFT_DESCRIPTIONS = {
    1: "месяц в VEDA SANCTUM в дар",
    2: "скидка 30% на личную сессию-терапию или распаковку личности",
    3: "2 часа личной глубинной сессии в дар - распаковка личности или энерготерапия, на выбор",
}

# сколько дней просрочки допускаем, прежде чем откатывать ступень на одну вниз —
# короткое опоздание с оплатой (пара дней) не должно "ронять" человека
ASCENSION_OVERDUE_GRACE_DAYS = 30


def _luminar_rank(count: int) -> int:
    for threshold, rank in LUMINAR_THRESHOLDS:
        if (count or 0) >= threshold:
            return rank
    return 0


def compute_ascension_level(user_id: int) -> int:
    """Текущая ступень "Пути Восхождения" (1/2/3 пока что) — считается из
    накопленных дней в VEDA SANCTUM (accumulated_days, растёт с каждым
    продлением, не обнуляется при перерыве) плюс ускорители 3-й ступени
    (ключ Люминар I или покупка VEDA HEALING FLOW), минус откат при
    затянувшейся просрочке (см. ASCENSION_OVERDUE_GRACE_DAYS)."""
    membership = db.get_sanctum_membership(user_id)
    if not membership:
        return 1

    months = (membership["accumulated_days"] or 0) / 30

    level = 2 if months >= 2 else 1
    if level == 2:
        user_row = db.get_user(user_id)
        has_luminar_1 = user_row and _luminar_rank(user_row["luminar_count"]) >= 1
        bought_meditation = user_row and bool(user_row["bought_meditation_bot"])
        # оба условия нужны вместе: 8 месяцев (2+6) в поле И (ключ Люминар I
        # ИЛИ покупка VEDA HEALING FLOW) — её явное решение 2026-08-31, а не
        # "любое из трёх" (более раннее моё предположение, было неверным)
        if months >= 8 and (has_luminar_1 or bought_meditation):
            level = 3

    valid_until = _parse_date(membership["valid_until"]) if membership["valid_until"] else None
    out_of_standing = membership["status"] == "removed" or (
        valid_until and (_today() - valid_until).days > ASCENSION_OVERDUE_GRACE_DAYS
    )
    if out_of_standing and level > 1:
        level -= 1

    return level


def _referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def _personal_link_kb(label: str):
    """Готовая клавиатура с одной кнопкой-ссылкой на личный чат создательницы -
    используется везде, где текст приглашает написать ей лично (Искра,
    напоминание о намерении, оплата, поздравления Люминаров). Возвращает
    None, если ссылка ещё не задана в панели - тогда сообщение уходит без
    кнопки, а не с кнопкой в никуда."""
    link = db.get_setting("admin_personal_chat_link")
    if not link:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=link)]])


async def _send_with_optional_photo(
    bot: Bot, user_id: int, text: str, photo: str, reply_markup=None, protect_content=False
):
    """Отправляет текст-поздравление; если для этого сообщения задано фото -
    старается прислать одним сообщением (фото с подписью), как она просила.
    У Telegram подпись к фото ограничена 1024 символами - если текст длиннее
    (сейчас так у «Первое Касание», ~1670 символов) или Telegram отклонит по
    другой причине, присылаем фото и полный текст отдельно, но подряд - чтобы
    ни фото, ни хотя бы слово из текста не потерялись. Используется и для
    поздравлений со ступенями/Люминарами, и для рассылок с фото (см.
    adm_broadcast_execute) - там же самый риск: длинная подпись к фото."""
    if photo:
        try:
            await bot.send_photo(
                user_id, photo, caption=text or None, reply_markup=reply_markup, protect_content=protect_content
            )
            return
        except TelegramBadRequest:
            await bot.send_photo(user_id, photo, protect_content=protect_content)
    await bot.send_message(user_id, text, reply_markup=reply_markup, protect_content=protect_content)


class IntentionStates(StatesGroup):
    waiting_text = State()


# Тексты ступеней/намерения/Люминаров теперь целиком в settings (админ-панель
# → ⚜️ VEDA SANCTUM → ✉️ Тексты Пути Восхождения и Люминаров) — она попросила
# возможность оформлять их сама (жирный текст и т.п.), поэтому здесь только
# ключи настроек, а сами тексты читаются через db.get_setting() в момент
# использования (см. show_profile, _handle_ascension_transition,
# _invite_intention_ritual, check_intention_reminders, _credit_luminar_referral).
ASCENSION_TEXT_KEYS = {
    1: "ascension_level1_text",
    2: "ascension_level2_text",
    3: "ascension_level3_text",
}


async def _invite_intention_ritual(bot: Bot, user_id: int):
    """Приглашение написать намерение при переходе на ступень «Искра» —
    устанавливает состояние ожидания ответа НАПРЯМУЮ, в обход обычного
    механизма (это не собственный апдейт человека — сюда попадают из
    reg_confirm/adm_grant_access_price, из чужого чата), поэтому берём
    FSMContext через общее модульное хранилище fsm_storage, а не через
    аргумент функции."""
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=fsm_storage, key=key)
    await state.set_state(IntentionStates.waiting_text)
    try:
        await bot.send_message(user_id, db.get_setting("ascension_intention_invite_text"))
    except Exception:
        logging.exception("Не удалось отправить приглашение к намерению пользователю %s", user_id)


@router.callback_query(F.data == "start_intention")
async def start_intention_cb(callback: CallbackQuery):
    await _invite_intention_ritual(callback.bot, callback.from_user.id)
    await callback.answer()


def _intention_cta_kb(level: int):
    """Кнопка «Написать намерение» - только на тексте ступени «Искра»
    (level 2), и при самом переходе, и при повторном чтении полного текста
    из профиля («📖 Читать послание ступени») - оба места используют один и
    тот же текст, значит и кнопка должна быть на обоих одинаково."""
    if level != 2:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Написать намерение", callback_data="start_intention")]])


async def _handle_ascension_transition(bot: Bot, user_id: int, old_level: int, new_level: int):
    """Если человек реально перешёл на новую ступень (не откат, не тот же
    уровень) — присылает видеокружок/голосовое (если заданы для этой ступени),
    а следом текст-поздравление - именно в таком порядке, по её явному решению
    2026-09-17: личный видео/голосовой штрих должен идти ПЕРЕД текстом, а не
    после. На 2-й ступени («Искра») под текстом есть кнопка «Написать намерение» -
    сам ритуал (_invite_intention_ritual) запускается ТОЛЬКО по нажатию этой
    кнопки (см. start_intention_cb), не автоматически, иначе приглашение и
    включение ожидания текста дублируются."""
    if new_level <= old_level:
        return
    text_key = ASCENSION_TEXT_KEYS.get(new_level)
    if text_key:
        await _send_ascension_extra_media(bot, user_id, new_level)
        text = db.get_setting(text_key)
        user_row = db.get_user(user_id)
        photo = db.get_setting(f"ascension_level{new_level}_photo")
        try:
            await _send_with_optional_photo(
                bot, user_id, _personalize(text, user_row), photo, reply_markup=_intention_cta_kb(new_level)
            )
        except Exception:
            logging.exception("Не удалось отправить поздравление со ступенью пользователю %s", user_id)


async def _send_ascension_extra_media(bot: Bot, user_id: int, level: int):
    """Необязательный личный штрих на переходе ступени - видеокружок и/или
    голосовое, отдельным сообщением ПЕРЕД текстом-поздравлением (у обоих в
    Telegram не бывает подписи, поэтому не совмещаются с текстом в одно
    сообщение, как фото; порядок - её явное решение 2026-09-17). Ничего не
    делает, если для этой ступени ничего не загружено - см. adm_ascension_media."""
    # у видео и голосового - отдельные попытки: если, например, кружочек не
    # прошёл (нередкий реальный случай - у человека в настройках Telegram
    # включён запрет на голосовые/видеосообщения не от контактов, ошибка
    # VOICE_MESSAGES_FORBIDDEN, обнаружено 2026-09-18), голосовое всё равно
    # должно попытаться дойти само по себе, а не молча пропуститься вместе с ним
    video_note_id = db.get_setting(f"ascension_level{level}_video_note")
    if video_note_id:
        try:
            await bot.send_video_note(user_id, video_note_id)
        except Exception:
            logging.exception("Не удалось отправить видеокружок ступени пользователю %s", user_id)

    voice_id = db.get_setting(f"ascension_level{level}_voice")
    if voice_id:
        try:
            await bot.send_voice(user_id, voice_id)
        except Exception:
            logging.exception("Не удалось отправить голосовое ступени пользователю %s", user_id)


async def _credit_luminar_referral(bot: Bot, referred_user_id: int):
    """Засчитывает реально вошедшего-оплатившего человека тому, кто его
    пригласил (если есть) — вызывать ТОЛЬКО при первой в жизни оплате VEDA
    SANCTUM этим человеком (см. reg_confirm). Если это переводит пригласившего
    на новый ранг Ордена Люминаров — сообщает и ему, и всем админам; на ранге I
    (5 приглашённых) дополнительно дарит пригласившему месяц в VEDA SANCTUM."""
    referred_user = db.get_user(referred_user_id)
    if not referred_user or not referred_user["referred_by"]:
        return
    referrer_id = referred_user["referred_by"]
    referrer = db.get_user(referrer_id)
    if not referrer:
        return

    old_rank = _luminar_rank(referrer["luminar_count"])
    new_count = db.increment_luminar_count(referrer_id)
    new_rank = _luminar_rank(new_count)
    if new_rank <= old_rank:
        # обычный, не пороговый реферал — мгновенная тёплая обратная связь,
        # не дожидаясь порога 5/10/30 (иначе первые несколько приглашённых
        # человеком остаются вообще незамеченными для него самого)
        ping_variants = [db.get_setting(f"luminar_referral_ping_text_{i}") for i in (1, 2, 3)]
        ping_variants = [t for t in ping_variants if t]
        if ping_variants:
            try:
                await bot.send_message(referrer_id, random.choice(ping_variants))
            except Exception:
                logging.exception("Не удалось отправить мгновенное уведомление о реферале пользователю %s", referrer_id)
        return

    gift_desc = LUMINAR_GIFT_DESCRIPTIONS[new_rank]
    gift_note = ""
    template = db.get_setting(f"luminar_{new_rank}_text") or ""
    user_message = (
        template.replace("{имя}", (referrer["preferred_name"] or referrer["first_name"] or "друг"))
        .replace("{число}", str(new_count))
        .replace("{дар}", gift_desc)
    )
    if new_rank == 1:
        old_referrer_level = compute_ascension_level(referrer_id)
        valid_until, _ = _extend_sanctum_membership(referrer_id)
        new_referrer_level = compute_ascension_level(referrer_id)
        gift_note = f"\n🎁 Автоматически подарен месяц в VEDA SANCTUM (до {valid_until.strftime('%d.%m.%Y')})."
        await _handle_ascension_transition(bot, referrer_id, old_referrer_level, new_referrer_level)

    try:
        await _send_with_optional_photo(
            bot,
            referrer_id,
            user_message,
            db.get_setting(f"luminar_{new_rank}_photo"),
            reply_markup=_personal_link_kb("💌 Написать Алёне лично"),
        )
    except Exception:
        logging.exception("Не удалось уведомить о новом ранге Люминара пользователя %s", referrer_id)

    referrer_name = _user_display_name(referrer)
    today_str = _today().strftime("%d.%m.%Y")
    if new_rank != 1:
        gift_note = f"\n🎁 Дар (выдаётся Вами лично): {gift_desc}."
    for admin_id in db.get_all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"✨ {html.escape(referrer_name)} (ID {referrer_id}) получил(а) {LUMINAR_RANK_NAMES[new_rank]} "
                f"{today_str} - всего приглашённых-оплативших: {new_count}.{gift_note}",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа %s о новом ранге Люминара", admin_id)


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SANCTUM)],
        [KeyboardButton(text=BTN_MEDITATION)],
        [KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_WEBINARS)],
        [KeyboardButton(text=BTN_ABOUT)],
        [KeyboardButton(text=BTN_FEED)],
        [KeyboardButton(text=BTN_INFO)],
    ]
    if db.is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _divider(label: str) -> list:
    return [InlineKeyboardButton(text=f"━━━ {label} ━━━", callback_data="noop")]


# Единственный источник правды и для разделов админ-панели, и для списка прав,
# которые можно выдать администратору-помощнику (владельцу — Вам — все эти
# проверки не нужны, у владельца доступ ко всему всегда, см. db.has_permission).
ADMIN_PERMISSION_SECTIONS = [
    ("📊 Обзор", [
        ("adm_analytics", "📊 Аналитика"),
    ]),
    ("🗓 Вебинары, практики, расстановки", [
        ("adm_webinars", "🗓 Вебинары"),
        ("adm_webinar_reminder_texts", "✉️ Тексты напоминаний о вебинарах"),
    ]),
    ("⚜️ VEDA SANCTUM", [
        ("adm_sanctum", "⚜️ Настройки VEDA SANCTUM"),
        ("adm_grant_access", "🔑 Выдать/продлить доступ VEDA SANCTUM"),
        ("adm_sanctum_list", "📋 Подписчики VEDA SANCTUM (убрать - прямо там)"),
        ("adm_reminder_texts", "✉️ Тексты напоминаний VEDA SANCTUM"),
        ("adm_rituals", "🌙 Календарь ритуалов"),
    ]),
    ("🪜 Путь Восхождения и Люминаров", [
        ("adm_ascension_texts", "🪜 Тексты Пути Восхождения и Люминаров"),
        ("adm_ascension_photos", "🖼 Фото Пути Восхождения и Люминаров"),
        ("adm_ascension_media", "🎥 Видео/голос на переходе ступени"),
        ("adm_intentions_list", "🕯 Намерения участников"),
        ("adm_profile_texts", "✨ Тексты «Мой профиль»"),
        ("adm_personal_link", "💌 Ссылка на личный чат с Alena Veda"),
    ]),
    ("🧘 VEDA HEALING FLOW", [
        ("adm_meditation_text", "✏️ Текст VEDA HEALING FLOW"),
        ("adm_photo_meditation", "🖼 Фото VEDA HEALING FLOW"),
        ("adm_meditation_coming_soon", "🌙 Текст «скоро откроется» (пока нет ссылки)"),
        ("adm_meditation_link", "🔗 Ссылка на VEDA HEALING FLOW"),
    ]),
    ("💠 Общие тексты бота", [
        ("adm_welcome_text", "✏️ Текст приветствия (/start)"),
        ("adm_photo_welcome", "🖼 Фото приветствия (/start)"),
        ("adm_about", "💠 Текст «Философия Alena Veda»"),
        ("adm_photo_about", "🖼 Фото «Философия Alena Veda»"),
        ("adm_faq", "❓ Текст «Частые вопросы»"),
        ("adm_faq_suggestions", "💡 Вопросы от людей для FAQ"),
        ("adm_faq_suggestion_invite", "✏️ Текст приглашения «Предложить вопрос»"),
        ("adm_faq_suggestion_confirm", "✏️ Текст подтверждения (вопрос принят)"),
        ("adm_rules", "📜 Текст «Правила пространства»"),
    ]),
    ("💳 Оплаты", [
        ("adm_payment", "💳 Реквизиты оплаты"),
        ("adm_pending", "🧾 Заявки на подтверждение"),
        ("adm_stalled", "⏳ Зависшие заявки (чек ещё не прислали)"),
    ]),
    ("📢 Контент пространства", [
        ("adm_broadcast", "📢 Сделать рассылку"),
        ("adm_feed_add", "➕ Добавить публикацию в архив напрямую"),
    ]),
    ("🧲 Автоматизация", [
        ("adm_reengage", "🧲 Автовозврат потерянных людей"),
    ]),
    ("👥 Люди", [
        ("adm_users_list", "👥 Подписчики бота (блокировка - прямо там)"),
        ("adm_admins", "👥 Администраторы"),
    ]),
]

ADMIN_PERMISSIONS = {key: label for _, buttons in ADMIN_PERMISSION_SECTIONS for key, label in buttons}

# набор прав, который предлагается по умолчанию (уже отмеченным) при добавлении
# нового администратора-помощника — её собственный подтверждённый стартовый
# набор для помощников по контенту (2026-08-31): аналитика, рассылки, архив,
# сами вебинары; она может доснять/добавить прямо в этом же экране
DEFAULT_HELPER_PERMISSIONS = {"adm_analytics", "adm_broadcast", "adm_feed_add", "adm_webinars", "adm_users_list"}


def admin_panel_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for label, buttons in ADMIN_PERMISSION_SECTIONS:
        visible = [(key, text) for key, text in buttons if db.has_permission(user_id, key)]
        if not visible:
            continue
        rows.append(_divider(label))
        for key, text in visible:
            rows.append([InlineKeyboardButton(text=text, callback_data=key)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _require_permission(callback: CallbackQuery, permission_key: str) -> bool:
    """Единая проверка доступа к разделу админ-панели — владельцу (Вам) всегда
    True, администратору-помощнику — только если раздел явно разрешён (см.
    "👥 Администраторы" → права). Сама отвечает алертом и возвращает False,
    если доступа нет."""
    if not db.has_permission(callback.from_user.id, permission_key):
        await callback.answer("У Вас нет доступа к этому разделу", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    # заголовки-разделители в админ-панели — нажатие ничего не делает,
    # просто отвечаем Telegram, чтобы кнопка не "крутилась" у неё в ожидании
    await callback.answer()


class ReceiptStates(StatesGroup):
    waiting_receipt = State()


class WebinarAddStates(StatesGroup):
    title = State()
    event_type = State()
    description = State()
    date_text = State()
    price = State()
    invite_link = State()


class EditFieldStates(StatesGroup):
    waiting_value = State()


class AdminPermStates(StatesGroup):
    picking = State()


class GrantAccessStates(StatesGroup):
    waiting_user_id = State()
    waiting_valid_until = State()
    waiting_price = State()
    waiting_accumulated_months = State()


class PromiseStates(StatesGroup):
    waiting_date = State()


class BroadcastStates(StatesGroup):
    waiting_segment = State()
    waiting_content = State()
    waiting_more_photos = State()
    waiting_button_choice = State()
    waiting_button_text = State()
    waiting_button_url = State()
    waiting_archive_choice = State()
    waiting_archive_days = State()
    waiting_allow_questions = State()
    waiting_confirm = State()


class FeedAddStates(StatesGroup):
    waiting_content = State()
    waiting_days = State()
    waiting_allow_questions = State()


class QuestionStates(StatesGroup):
    waiting_question = State()


class AnswerQuestionStates(StatesGroup):
    waiting_answer = State()
    waiting_visibility = State()


class PhotoUploadStates(StatesGroup):
    waiting_photo = State()


class NameStates(StatesGroup):
    waiting_name = State()


class ContactAdminStates(StatesGroup):
    waiting_message = State()


class FaqSuggestionStates(StatesGroup):
    waiting_text = State()


class AdminReplyStates(StatesGroup):
    waiting_reply = State()


# ---------- базовые команды ----------

async def _send_welcome(message: Message):
    text = db.get_setting("welcome_text")
    welcome_photo = db.get_setting("welcome_photo")
    if welcome_photo:
        await message.answer_photo(welcome_photo)
    await message.answer(
        text, reply_markup=main_menu_kb(message.from_user.id), protect_content=_protect_for(message.from_user.id)
    )


NAME_QUESTION_TEXT = "Как я могу к Вам обращаться? Представьтесь, пожалуйста. ✨"


async def _send_referral_welcome(message: Message, referrer):
    """Особый первый момент для тех, кто пришёл по реферальной ссылке — до
    вопроса об имени, отдельно от обычного сценария (см. обсуждение премиальных
    систем приглашений - персонализация + эксклюзивность). Имя пригласившего
    берётся так же, как везде в боте ({имя} = preferred_name или first_name),
    а не username и не ID - username добавляется РЯДОМ, в скобках, кликабельным,
    только если он у пригласившего вообще есть."""
    referrer_name = referrer["preferred_name"] or referrer["first_name"] or "друг"
    if referrer["username"]:
        mention = (
            f'{html.escape(referrer_name)} (<a href="https://t.me/{referrer["username"]}">'
            f'@{html.escape(referrer["username"])}</a>)'
        )
    else:
        mention = html.escape(referrer_name)
    text = (
        db.get_setting("referral_welcome_text")
        .replace("{пригласивший}", mention)
        .replace("{название}", html.escape(SANCTUM_FULL_NAME))
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data="referral_continue")]])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "referral_continue")
async def referral_continue_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(NameStates.waiting_name)
    await callback.message.answer(NAME_QUESTION_TEXT)
    await callback.answer()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # реферальная ссылка вида t.me/<бот>?start=ref_<id> — Telegram доставляет её
    # как "/start ref_<id>"; засчитываем пригласившего только на самом первом
    # /start (add_user сам это гарантирует через INSERT OR IGNORE)
    referred_by = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            candidate = int(parts[1][len("ref_"):])
            if candidate != message.from_user.id and db.get_user(candidate):
                referred_by = candidate
        except ValueError:
            pass
    db.add_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name, referred_by=referred_by
    )
    user_row = db.get_user(message.from_user.id)
    # знакомство считаем завершённым только когда сохранено имя — не просто
    # по факту существования записи в базе. Реальный случай (2026-09-06):
    # у человека временно не отправилось особое приветствие (Telegram на
    # мгновение посчитал бота заблокированным), запись в базе уже была
    # создана, и при повторном /start бот решил, что знакомство пройдено,
    # пропустив и приветствие, и вопрос об имени, и "Первое Касание" вообще.
    # Теперь при незавершённом знакомстве /start безопасно повторяет его
    # заново, опираясь на уже сохранённого пригласившего (user_row), а не
    # только на параметр именно этого конкретного /start.
    if not (user_row and user_row["preferred_name"]):
        referrer_id = (user_row["referred_by"] if user_row else None) or referred_by
        if referrer_id:
            referrer = db.get_user(referrer_id)
            if referrer:
                try:
                    await _send_referral_welcome(message, referrer)
                    return
                except Exception:
                    logging.exception(
                        "Не удалось отправить особое приветствие пригласившему пользователю %s",
                        message.from_user.id,
                    )
                    # не бросаем дальше — человек всё равно получит обычный
                    # вопрос об имени ниже, а не останется совсем без ответа
        await state.set_state(NameStates.waiting_name)
        await message.answer(NAME_QUESTION_TEXT)
        return
    await _send_welcome(message)


@router.my_chat_member()
async def on_my_chat_member_update(event: ChatMemberUpdated):
    """Telegram сам присылает это событие, когда человек блокирует бота или
    выходит из чата с ним (new_chat_member.status становится 'kicked'/'left'),
    и когда он снова пишет боту (status возвращается в 'member') — единственный
    способ узнать про самостоятельный уход, в отличие от блокировки, которую
    делаете вы сами через админ-панель (users.blocked, отдельное поле)."""
    status = event.new_chat_member.status
    user_id = event.from_user.id
    if status in ("kicked", "left"):
        db.set_user_self_departed(user_id, True)
        logging.info("Пользователь %s сам заблокировал бота или вышел", user_id)
    elif status == "member":
        db.set_user_self_departed(user_id, False)


@router.message(NameStates.waiting_name)
async def name_received(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    db.set_preferred_name(message.from_user.id, text.strip())
    data = await state.get_data()
    pending_webinar_id = data.get("pending_webinar_id")
    await state.clear()
    if pending_webinar_id:
        await message.answer(
            f"Благодарю, {text.strip()}! 🙏", reply_markup=main_menu_kb(message.from_user.id)
        )
        # если вебинар вдруг стал недоступен, пока человек знакомился с ботом —
        # не страшно, он уже в главном меню с полноценной навигацией
        await _send_webinar_card(message, pending_webinar_id, message.from_user.id)
        return
    await _send_welcome(message)
    # небольшая пауза перед "Первым Касанием" — чтобы оба сообщения не
    # выскакивали одним потоком сразу друг за другом
    await asyncio.sleep(2)
    # "Первое Касание" — одноразовое сообщение только настоящим новичкам,
    # сразу после того, как они представились в самый первый раз (сюда не
    # попадают ни возвращающиеся люди, ни те, кто пришёл по ссылке на
    # конкретный вебинар — см. ветку выше)
    user_row = db.get_user(message.from_user.id)
    text = db.get_setting(ASCENSION_TEXT_KEYS[1])
    photo = db.get_setting("ascension_level1_photo")
    kb_rows = []
    if user_row and user_row["referred_by"]:
        # кнопка на Sanctum здесь уместна именно для пришедших по ссылке —
        # закрывает обещание "подробнее далее" из особого приветствия
        kb_rows.append([InlineKeyboardButton(text="⚜️ Что такое VEDA SANCTUM", callback_data="open_sanctum")])
    # календарь ритуалов - показываем на самом первом экране, который видит
    # АБСОЛЮТНО каждый новый человек, чтобы не-участники узнавали о нём
    # активно, а не только натыкались случайно через "Инфо"
    kb_rows.append([_ritual_calendar_btn()])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await _send_with_optional_photo(message.bot, message.from_user.id, _personalize(text, user_row), photo, kb)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb(message.from_user.id))


@router.message(IntentionStates.waiting_text)
async def intention_received(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    db.set_sanctum_intention(message.from_user.id, text.strip())
    await state.clear()
    user_row = db.get_user(message.from_user.id)
    await message.answer(
        _personalize(db.get_setting("ascension_intention_confirmation_text"), user_row),
        reply_markup=_personal_link_kb("💌 Написать запрос Алёне"),
    )


# ---------- вебинары (пользователь) ----------

@router.message(F.text == BTN_WEBINARS)
async def show_webinars(message: Message):
    # пропускаем записи с незаполненным названием/ценой — это может быть
    # черновик или последствие сбоя при заполнении, но пользователь не должен
    # видеть в списке кнопку "None" вместо названия
    webinars = [w for w in db.get_active_webinars() if w["title"] and w["price"]]
    past_count = len(db.get_past_webinars())
    past_row = [InlineKeyboardButton(text=f"📜 Прошедшие ({past_count})", callback_data="wb_past_list")]

    if not webinars:
        text = (
            "Пока нет по графику предстоящих или открытых вебинаров.\n"
            "Наблюдайте за информацией в этом пространстве или загляните позже в раздел «Вебинары, практики, расстановки»🌿"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[past_row]) if past_count else None
        await message.answer(text, reply_markup=kb)
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{_strip_html_tags(w['title'])} - {_strip_html_tags(w['price'])}",
            callback_data=f"wb_view_{w['id']}",
        )]
        for w in webinars
    ]
    if past_count:
        rows.append(past_row)
    await message.answer("Ближайшие вебинары:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "wb_past_list")
async def wb_past_list(callback: CallbackQuery):
    past = db.get_past_webinars()
    if not past:
        await callback.answer("Прошедших пока нет.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_strip_html_tags(w["title"]), callback_data=f"wb_past_view_{w['id']}")]
        for w in past
    ])
    await callback.message.answer("📜 Прошедшие вебинары, практики, расстановки:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("wb_past_view_"))
async def wb_past_view(callback: CallbackQuery):
    webinar_id = int(callback.data[len("wb_past_view_"):])
    w = db.get_webinar(webinar_id)
    if not w or w["is_active"] or not w["title"]:
        await callback.answer("Эта запись больше недоступна.", show_alert=True)
        return
    # прошедший вид: тема и описание остаются (это и есть "витрина" тем, которые
    # уже разбирались), а цена и регистрация скрыты — участие в прошедшем
    # событии физически невозможно, показывать цену тоже не имеет смысла
    text = f"{w['title']}\n\n{w['description']}\n\n🗓 Прошёл: {w['date_text']}"
    kb_rows = []
    qa_count = db.count_public_qa("webinar", webinar_id)
    if qa_count:
        kb_rows.append([InlineKeyboardButton(
            text=f"💬 Вопросы и ответы ({qa_count})", callback_data=f"wq_public_webinar_{webinar_id}"
        )])
    kb_rows.append([InlineKeyboardButton(text="⬅️ К прошедшим", callback_data="wb_past_list")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if w["photo"]:
        await callback.message.answer_photo(w["photo"])
    await callback.message.answer(text, reply_markup=kb, protect_content=_protect_for(callback.from_user.id))
    await callback.answer()


async def _send_webinar_card(message: Message, webinar_id: int, viewer_id: int) -> bool:
    """Общая логика показа карточки вебинара — используется и из wb_view (обычный
    переход по кнопке из списка), и из name_received (когда человек попал сюда по
    прямой ссылке/пересланной карточке, ещё не будучи в базе, и только что прошёл
    через "как к Вам обращаться"). Возвращает False, если вебинар недоступен."""
    w = db.get_webinar(webinar_id)
    if not w or not w["is_active"] or not w["title"] or not w["price"]:
        return False
    # title/description/date_text/price — доверенные HTML-поля (не экранируем,
    # см. HTML_TRUSTED_FIELDS): жирный/курсив/ссылки, которые вы вставили
    # прямо в Telegram при редактировании, показываются как есть. Без
    # обёртки <b> вокруг названия намеренно — вы сами решаете, делать ли
    # его жирным, форматируя текст при вводе.
    text = (
        f"{w['title']}\n\n"
        f"{w['description']}\n\n"
        f"🗓 {w['date_text']}\n"
        f"💳 Стоимость: {w['price']}"
    )
    kb_rows = [[InlineKeyboardButton(text="Зарегистрироваться и оплатить", callback_data=f"wb_reg_{webinar_id}")]]
    if w["allow_questions"]:
        kb_rows.append([InlineKeyboardButton(text="❓ Задать вопрос", callback_data=f"wq_ask_webinar_{webinar_id}")])
        qa_count = db.count_public_qa("webinar", webinar_id)
        if qa_count:
            kb_rows.append([InlineKeyboardButton(
                text=f"💬 Вопросы и ответы ({qa_count})", callback_data=f"wq_public_webinar_{webinar_id}"
            )])
    kb_rows.append([InlineKeyboardButton(text="⬅️ К списку вебинаров", callback_data="wb_list_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if w["photo"]:
        await message.answer_photo(w["photo"])
    await message.answer(text, reply_markup=kb, protect_content=_protect_for(viewer_id))
    return True


@router.callback_query(F.data == "wb_list_back")
async def wb_list_back_cb(callback: CallbackQuery):
    await show_webinars(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("wb_view_"))
async def wb_view(callback: CallbackQuery, state: FSMContext):
    webinar_id = int(callback.data.split("_")[-1])
    # человек мог попасть сюда по пересланной карточке/приглашению, ни разу не
    # нажав /start сам — заводим его в базу и сначала спрашиваем имя, как при
    # обычном первом запуске, а карточку покажем сразу после ответа (см.
    # name_received) — иначе он упрётся в тупик на моменте отправки чека
    if not db.get_user(callback.from_user.id):
        db.add_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        await state.set_state(NameStates.waiting_name)
        await state.update_data(pending_webinar_id=webinar_id)
        await callback.message.answer("Как я могу к Вам обращаться? Представьтесь, пожалуйста. ✨")
        await callback.answer()
        return
    ok = await _send_webinar_card(callback.message, webinar_id, callback.from_user.id)
    if not ok:
        await callback.answer("Этот вебинар больше недоступен", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("wb_reg_"))
async def wb_reg(callback: CallbackQuery, state: FSMContext):
    webinar_id = int(callback.data.split("_")[-1])
    w = db.get_webinar(webinar_id)
    if not w or not w["is_active"] or not w["title"] or not w["price"]:
        await callback.answer("Этот вебинар больше недоступен", show_alert=True)
        return
    if not _payment_ready("payment_purpose_webinar"):
        await callback.answer(NOT_READY_MESSAGE, show_alert=True)
        return

    # если у человека уже есть незавершённая заявка именно на этот вебинар —
    # используем её, а не создаём дубль (иначе потом придёт два одинаковых
    # напоминания подряд про один и тот же вебинар)
    existing = db.get_pending_registration(callback.from_user.id, "webinar", webinar_id)
    if existing and existing["status"] == "awaiting_confirmation":
        await callback.message.answer(
            f"У Вас уже есть заявка на «{w['title']}» - она ожидает проверки, "
            "я подтвержу её в ближайшее время 🙏"
        )
        await callback.answer()
        return
    reg_id = existing["id"] if existing else db.create_registration(
        callback.from_user.id, "webinar", webinar_id, w["title"], w["price"]
    )
    await state.set_state(ReceiptStates.waiting_receipt)
    await state.update_data(reg_id=reg_id)
    await callback.message.answer(
        f"Отлично! Для участия в «{w['title']}» переведите {w['price']}.\n\n"
        f"{_payment_block('payment_purpose_webinar')}\n\n"
        "После оплаты пришлите сюда, в VEDAME SPACE, скриншот Вашего чека 📸",
        reply_markup=_personal_link_kb("💌 Написать Алёне лично"),
    )
    await callback.answer()


# ---------- VEDA SANCTUM (пользователь) ----------

@router.message(F.text == BTN_SANCTUM)
async def show_sanctum(message: Message, user_id: int = None):
    # user_id — необязательный, только чтобы этот же экран можно было открыть
    # и по инлайн-кнопке (см. open_sanctum), где "автор" сообщения бота — сам
    # бот, а не нажавший, поэтому id нажавшего нужно передать явно
    user_id = user_id or message.from_user.id
    s = db.get_sanctum()
    if s["intro_text"] == db.PLACEHOLDER_SANCTUM_INTRO or s["price"] == db.PLACEHOLDER_PRICE:
        await message.answer(f"✨ Информация о канале {html.escape(SANCTUM_FULL_NAME)} скоро появится здесь. Загляните позже 🙏")
        return

    # уже существующего подписчика (активного или с истёкшей подпиской) не
    # прогоняем заново через вступление-манифест и законы — это только для
    # тех, кто в VEDA SANCTUM никогда не был (или был, но удалён оттуда)
    membership = db.get_sanctum_membership(user_id)
    today = _today()
    if membership and membership["status"] != "removed" and membership["valid_until"]:
        valid_until = _parse_date(membership["valid_until"])
        price = membership["price"] or s["price"]
        if valid_until and valid_until >= today:
            text = (
                f"⚜️ {html.escape(SANCTUM_FULL_NAME)}\n\n"
                f"Ваш доступ активен до {valid_until.strftime('%d.%m.%Y')}.\n\n"
                f"Хотите продлить заранее на следующий месяц? Стоимость: {price} "
                "(закреплена за Вами, как за опытным участником Sanctum)."
            )
            button_text = "Продлить"
        else:
            date_part = f" {valid_until.strftime('%d.%m.%Y')}" if valid_until else ""
            deadline = _price_lock_deadline(membership)
            if deadline and not _price_lock_expired(membership):
                price_line = (
                    f"Стоимость подписки в месяц: {price} - Ваша прежняя цена сохраняется "
                    f"до {_fmt_date(deadline)}."
                )
            else:
                price_line = f"Стоимость подписки в месяц: {_price_for_user(user_id)}."
            text = (
                f"⚜️ {html.escape(SANCTUM_FULL_NAME)}\n\n"
                f"Ваш доступ закончился{date_part}.\n\n"
                f"Хотите возобновить?\n{price_line}"
            )
            button_text = "Возобновить"
        rows = [[InlineKeyboardButton(text=button_text, callback_data="sanctum_apply")]]
        if valid_until and valid_until >= today:
            rows.append([_ritual_calendar_btn()])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer(text, reply_markup=kb, protect_content=_protect_for(user_id))
        return

    # человек смотрит информацию о Sanctum, но ещё ни разу не начинал оформление -
    # запоминаем момент для "поведенческого" напоминания (см. check_reengagement,
    # get_sanctum_intro_viewers_due); сбрасывается, как только реально нажмёт
    # "Инициировать шаг" (см. sanctum_apply)
    db.mark_sanctum_intro_viewed(user_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Войти в глубину", callback_data="sanctum_laws")]
    ])
    if s["intro_photo"]:
        await message.answer_photo(s["intro_photo"])
    # intro_text и laws_text хранят готовую HTML-разметку (жирный текст и т.п.),
    # поэтому НЕ экранируем их, в отличие от остальных админ-текстов в боте.
    await message.answer(s["intro_text"], reply_markup=kb, protect_content=_protect_for(user_id))


@router.callback_query(F.data == "open_sanctum")
async def open_sanctum_cb(callback: CallbackQuery):
    await show_sanctum(callback.message, user_id=callback.from_user.id)
    await callback.answer()



@router.callback_query(F.data == "open_path_overview")
async def open_path_overview_cb(callback: CallbackQuery):
    user_row = db.get_user(callback.from_user.id)
    text = _personalize(db.get_setting("ascension_overview_text"), user_row)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "open_luminar_intro")
async def open_luminar_intro_cb(callback: CallbackQuery):
    await callback.message.answer(db.get_setting("luminar_intro_text"))
    await callback.answer()


@router.callback_query(F.data.startswith("open_level_msg_"))
async def open_level_message_cb(callback: CallbackQuery):
    # полное поэтичное послание ступени - то же самое, что приходит push-
    # сообщением ровно в момент перехода (см. _handle_ascension_transition);
    # здесь человек может перечитать его снова, по собственному желанию, раз
    # в профиле теперь только короткая сводка (см. show_profile)
    level = int(callback.data[len("open_level_msg_"):])
    user_row = db.get_user(callback.from_user.id)
    text = _personalize(db.get_setting(ASCENSION_TEXT_KEYS[level]), user_row)
    await callback.message.answer(text, reply_markup=_intention_cta_kb(level))
    await callback.answer()


@router.callback_query(F.data == "sanctum_laws")
async def sanctum_laws(callback: CallbackQuery):
    s = db.get_sanctum()
    if s["laws_text"] == db.PLACEHOLDER_SANCTUM_LAWS:
        await callback.answer(NOT_READY_MESSAGE, show_alert=True)
        return
    price = _price_for_user(callback.from_user.id)
    text = s["laws_text"].replace("{price}", price)
    ritual_line = db.get_setting("ritual_sanctum_line")
    if ritual_line:
        text += "\n\n" + ritual_line
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Инициировать шаг", callback_data="sanctum_apply")]
    ])
    await callback.message.answer(text, reply_markup=kb, protect_content=_protect_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "sanctum_apply")
async def sanctum_apply(callback: CallbackQuery, state: FSMContext):
    s = db.get_sanctum()
    if s["price"] == db.PLACEHOLDER_PRICE or not _payment_ready("payment_purpose_sanctum"):
        await callback.answer(NOT_READY_MESSAGE, show_alert=True)
        return
    # реально начал оформление - "поведенческое" напоминание (см. show_sanctum,
    # check_reengagement) больше не нужно
    db.clear_sanctum_intro_viewed(callback.from_user.id)
    price = _price_for_user(callback.from_user.id)

    # та же защита от дублей, что и в wb_reg — одна активная заявка на Sanctum
    # на человека, а не новая при каждом нажатии кнопки
    existing = db.get_pending_registration(callback.from_user.id, "sanctum", None)
    if existing and existing["status"] == "awaiting_confirmation":
        await callback.message.answer(
            f"У Вас уже есть заявка в {html.escape(SANCTUM_FULL_NAME)} - она ожидает проверки, "
            "я подтвержу её в ближайшее время 🙏"
        )
        await callback.answer()
        return
    reg_id = existing["id"] if existing else db.create_registration(
        callback.from_user.id, "sanctum", None, "VEDA SANCTUM", price
    )
    await state.set_state(ReceiptStates.waiting_receipt)
    await state.update_data(reg_id=reg_id)
    kb_rows = []
    personal_kb = _personal_link_kb("💌 Написать Алёне лично")
    if personal_kb:
        kb_rows.extend(personal_kb.inline_keyboard)
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sanctum_back")])
    await callback.message.answer(
        f"Для вступления в {html.escape(SANCTUM_FULL_NAME)} переведите {price}.\n\n"
        f"{_payment_block('payment_purpose_sanctum')}\n\n"
        "После оплаты пришлите сюда, в VEDAME SPACE, скриншот Вашего чека 📸\n\n"
        "Благодарю!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.callback_query(F.data == "sanctum_back")
async def sanctum_back_cb(callback: CallbackQuery):
    # единая точка "назад" с экрана реквизитов - ведёт не строго на предыдущий
    # шаг, а в show_sanctum, который сам покажет правильный экран для этого
    # конкретного человека (манифест для нового, "продлить/возобновить" для
    # уже бывшего в Sanctum) - это надёжнее, чем помнить, откуда именно он
    # попал на sanctum_apply (путей туда несколько - законы, продление из
    # профиля, продление с самого экрана Sanctum).
    await show_sanctum(callback.message, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "sanctum_promise")
async def sanctum_promise_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromiseStates.waiting_date)
    membership = db.get_sanctum_membership(callback.from_user.id)
    limit_line = ""
    latest = _promise_latest_date(membership)
    if latest:
        limit_line = (
            f"\n\nДату можно назвать не позже {_fmt_date(latest)} - до этого дня за Вами "
            "сохраняется Ваша прежняя цена."
        )
    await callback.message.answer(
        "На какую дату Вы планируете совершение оплаты?\n"
        "Пришлите в формате ДД.ММ.ГГГГ (например: 15.09.2026),\n"
        "я напомню Вам за день до неё.\n\n"
        "Это важно отметить именно здесь, в боте, а не писать мне лично: только отметка в боте "
        "показывает, что Вы возвращаетесь, - и пока дата не наступила, я не буду Вас беспокоить."
        f"{limit_line}"
    )
    await callback.answer()


def _promise_latest_date(membership):
    """Самая поздняя дата, которую можно назвать в "оплачу позже": последний
    день сохранения цены (price_lock_days после окончания доступа) - иначе
    можно было бы бесконечно "отодвигать" оплату и держать старую цену."""
    if not membership or not membership["valid_until"]:
        return None
    valid_until = _parse_date(membership["valid_until"])
    if not valid_until:
        return None
    return valid_until + timedelta(days=_int_setting("price_lock_days", 30))


@router.message(PromiseStates.waiting_date)
async def sanctum_promise_date(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    try:
        promise_date = datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Не получилось распознать дату.\nПришлите в формате ДД.ММ.ГГГГ, например: 15.09.2026.")
        return
    if promise_date <= _today():
        await message.answer("Дата должна быть в будущем.\nПришлите, пожалуйста, другую дату.")
        return
    membership = db.get_sanctum_membership(message.from_user.id)
    latest = _promise_latest_date(membership)
    if latest and promise_date > latest:
        await message.answer(
            f"Эту дату я, к сожалению, принять не могу: прежняя цена сохраняется до {_fmt_date(latest)}.\n"
            f"Пришлите, пожалуйста, дату не позже {_fmt_date(latest)}."
        )
        return
    db.set_promise_date(message.from_user.id, promise_date.isoformat())
    await state.clear()
    await message.answer(
        f"Хорошо 🙏\nЯ напомню Вам {(promise_date - timedelta(days=1)).strftime('%d.%m.%Y')}, "
        f"за день до {promise_date.strftime('%d.%m.%Y')}."
    )


# ---------- VEDA HEALING FLOW (медитативный помощник) ----------

def _meditation_button(text: str = "Перейти в VEDA HEALING FLOW"):
    """Кнопка на бот с медитациями — только если ссылка уже задана в
    админ-панели. Пока её нет, кнопку нигде не показываем вообще (ни в этом
    экране, ни в профиле, ни в напоминаниях) — лучше отсутствие кнопки, чем
    кнопка, которая ведёт в никуда."""
    link = db.get_setting("meditation_bot_link")
    if not link:
        return None
    return InlineKeyboardButton(text=text, url=link)


@router.message(F.text == BTN_MEDITATION)
async def show_meditation_bot(message: Message):
    btn = _meditation_button()
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None
    meditation_photo = db.get_setting("meditation_photo")
    if meditation_photo:
        await message.answer_photo(meditation_photo)
    text = db.get_setting("meditation_text")
    if not btn:
        # ссылка ещё не задана — без этой подсказки текст выше заканчивается
        # призывом "⤵️" в никуда, кнопки под ним нет и не будет, пока ссылка
        # не появится (см. _meditation_button)
        text += "\n\n" + db.get_setting("meditation_coming_soon_text")
    await message.answer(
        text,
        reply_markup=kb,
        protect_content=_protect_for(message.from_user.id),
    )


# ---------- обо мне ----------

@router.message(F.text == BTN_ABOUT)
async def show_about(message: Message):
    about_photo = db.get_setting("about_photo")
    if about_photo:
        await message.answer_photo(about_photo)
    text = db.get_setting("about_text")
    await message.answer(text, protect_content=_protect_for(message.from_user.id))


# ---------- инфо и правила ----------

def _info_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="open_faq")],
        [InlineKeyboardButton(text="📜 Правила пространства", callback_data="open_rules")],
        [_ritual_calendar_btn()],
    ])


@router.message(F.text == BTN_INFO)
async def show_info_menu(message: Message):
    await message.answer("Выберите, что интересует:", reply_markup=_info_menu_kb())


@router.callback_query(F.data == "info_menu_back")
async def info_menu_back_cb(callback: CallbackQuery):
    await callback.message.answer("Выберите, что интересует:", reply_markup=_info_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "open_faq")
async def open_faq_cb(callback: CallbackQuery):
    text = db.get_setting("faq_text")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💡 Предложить свой вопрос", callback_data="suggest_faq_question")],
        [InlineKeyboardButton(text="📜 Правила пространства", callback_data="open_rules")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="info_menu_back")],
    ])
    await callback.message.answer(text, reply_markup=kb, protect_content=_protect_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "suggest_faq_question")
async def suggest_faq_question_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FaqSuggestionStates.waiting_text)
    await callback.message.answer(db.get_setting("faq_suggestion_invite_text"))
    await callback.answer()


@router.message(FaqSuggestionStates.waiting_text)
async def suggest_faq_question_received(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    await state.clear()
    db.add_faq_suggestion(message.from_user.id, text)
    await message.answer(db.get_setting("faq_suggestion_confirm_text"))


@router.callback_query(F.data == "open_rules")
async def open_rules_cb(callback: CallbackQuery):
    text = db.get_setting("rules_text")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="open_faq")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="info_menu_back")],
    ])
    await callback.message.answer(text, reply_markup=kb, protect_content=_protect_for(callback.from_user.id))
    await callback.answer()


# ---------- архив публикаций пространства (лента) ----------

_RU_MONTHS = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь",
}


def _month_label(year_month: str) -> str:
    year, month = year_month.split("-")
    return f"{_RU_MONTHS.get(month, month)} {year}"


FEED_CONTENT_LABELS = {
    "photo": "фото-публикация",
    "video": "видео-публикация",
    "video_note": "кружочек",
    "album": "фотоальбом",
}


def _feed_post_short_label(post) -> str:
    text = _strip_html_tags(post["text"] or "").strip()
    if text:
        first_line = text.split("\n")[0]
        return first_line[:50] + ("…" if len(first_line) > 50 else "")
    return FEED_CONTENT_LABELS.get(post["content_type"], "публикация")


def _feed_months_kb(months) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=_month_label(ym), callback_data=f"feed_month_{ym}")] for ym in months]
    return InlineKeyboardMarkup(inline_keyboard=rows)


FEED_MONTHS_INTRO = (
    "Здесь собраны все публикации, упорядоченные по месяцам.\n\n"
    "Выберите месяц ниже - откроется самая свежая публикация из него, а дальше "
    "листайте кнопками «Раньше» / «Позже» под ней, одну за другой."
)


async def _send_feed_months(message: Message):
    months = db.get_feed_months(_today().isoformat())
    if not months:
        await message.answer("Пока в архиве пусто.\nЗагляните позже 🌿")
        return
    await message.answer(f"{BTN_FEED}\n\n{FEED_MONTHS_INTRO}", reply_markup=_feed_months_kb(months))


@router.message(F.text == BTN_FEED)
async def show_feed(message: Message):
    await _send_feed_months(message)


@router.callback_query(F.data == "feed_months")
async def feed_months(callback: CallbackQuery):
    months = db.get_feed_months(_today().isoformat())
    if not months:
        await callback.message.edit_text("Пока в архиве пусто.\nЗагляните позже 🌿")
        await callback.answer()
        return
    await callback.message.edit_text(f"{BTN_FEED}\n\n{FEED_MONTHS_INTRO}", reply_markup=_feed_months_kb(months))
    await callback.answer()


async def _send_feed_post_content(message: Message, post, protect: bool, viewer_id: int):
    content_type = post["content_type"]
    text = _personalize(post["text"] or "", db.get_user(viewer_id))
    if content_type == "text":
        await message.answer(text, protect_content=protect)
    elif content_type == "photo":
        await message.answer_photo(post["file_id"], caption=text or None, protect_content=protect)
    elif content_type == "video":
        await message.answer_video(post["file_id"], caption=text or None, protect_content=protect)
    elif content_type == "video_note":
        await message.answer_video_note(post["file_id"], protect_content=protect)
    elif content_type == "album":
        file_ids = json.loads(post["file_ids_json"] or "[]")
        media = [InputMediaPhoto(media=fid, caption=(text or None) if i == 0 else None) for i, fid in enumerate(file_ids)]
        await message.answer_media_group(media, protect_content=protect)


async def _show_feed_post_at(message: Message, year_month: str, idx: int, posts, viewer_id: int):
    """posts — уже отсортированный список публикаций месяца (новые первые);
    idx=0 — самая свежая. Отправляет саму публикацию + отдельным сообщением
    навигацию (Раньше/Позже, к месяцам, и для админа — редактировать/удалить)."""
    post = posts[idx]
    await _send_feed_post_content(message, post, _protect_for(viewer_id), viewer_id)

    nav_row = []
    if idx + 1 < len(posts):
        nav_row.append(InlineKeyboardButton(text="⬅️ Раньше", callback_data=f"feed_nav_{year_month}_{idx + 1}"))
    if idx - 1 >= 0:
        nav_row.append(InlineKeyboardButton(text="➡️ Позже", callback_data=f"feed_nav_{year_month}_{idx - 1}"))
    nav_rows = [nav_row] if nav_row else []
    if post["allow_questions"]:
        nav_rows.append([InlineKeyboardButton(text="❓ Задать вопрос", callback_data=f"wq_ask_feed_{post['id']}")])
        qa_count = db.count_public_qa("feed_post", post["id"])
        if qa_count:
            nav_rows.append([InlineKeyboardButton(
                text=f"💬 Вопросы и ответы ({qa_count})", callback_data=f"wq_public_feed_{post['id']}"
            )])
    nav_rows.append([InlineKeyboardButton(text="🗓 К выбору месяца", callback_data="feed_months")])
    if db.is_admin(viewer_id):
        nav_rows.append([InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"feed_edit_{post['id']}")])
        nav_rows.append([InlineKeyboardButton(text="🗑 Удалить из архива", callback_data=f"feed_delete_{post['id']}")])

    label = f"📖 {_month_label(year_month)} • публикация {idx + 1} из {len(posts)}"
    await message.answer(label, reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_rows))


@router.callback_query(F.data.startswith("feed_month_"))
async def feed_month_open(callback: CallbackQuery):
    year_month = callback.data[len("feed_month_"):]
    posts = db.get_feed_posts_in_month(year_month, _today().isoformat())
    if not posts:
        await callback.answer("В этом месяце публикаций больше нет.", show_alert=True)
        return
    await _show_feed_post_at(callback.message, year_month, 0, posts, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_nav_"))
async def feed_nav(callback: CallbackQuery):
    rest = callback.data[len("feed_nav_"):]
    year_month, idx_str = rest.rsplit("_", 1)
    idx = int(idx_str)
    posts = db.get_feed_posts_in_month(year_month, _today().isoformat())
    if not posts or idx < 0 or idx >= len(posts):
        await callback.answer("Публикация не найдена - возможно, её удалили.", show_alert=True)
        return
    await _show_feed_post_at(callback.message, year_month, idx, posts, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_delete_"))
async def feed_delete(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    post_id = int(callback.data.split("_")[-1])
    db.delete_feed_post(post_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 К выбору месяца", callback_data="feed_months")]
    ])
    await callback.message.edit_text("🗑 Публикация удалена из архива ✅", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_edit_"))
async def feed_edit_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    post_id = int(callback.data[len("feed_edit_"):])
    post = db.get_feed_post(post_id)
    if not post:
        await callback.answer("Эта публикация больше недоступна", show_alert=True)
        return
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="feed_post", field=str(post_id))
    current = post["text"] or "(текста нет - только фото/видео/кружочек, без подписи)"
    await callback.message.answer(
        f"Текущий текст:\n\n{current}\n\n"
        "Пришлите новый текст целиком (он полностью заменит старый - само фото/видео/кружочек, "
        "если он есть, останется прежним, меняется только текст):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_feed_add")
async def adm_feed_add_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_feed_add"):
        return
    await state.set_state(FeedAddStates.waiting_content)
    await callback.message.answer(
        "Пришлите текст, фото, видео или кружочек - то, что нужно сохранить в архив. "
        "Это НЕ рассылка, никто ничего не получит, публикация только добавится в "
        f"«{BTN_FEED}»."
    )
    await callback.answer()


@router.message(FeedAddStates.waiting_content)
async def adm_feed_add_content(message: Message, state: FSMContext):
    if message.photo:
        content_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        content_type, file_id = "video", message.video.file_id
    elif message.video_note:
        content_type, file_id = "video_note", message.video_note.file_id
    elif message.text:
        content_type, file_id = "text", None
    else:
        await message.answer(
            "Для архива подходит текст, фото, видео или кружочек. Пришлите, пожалуйста, что-то из этого 🙏"
        )
        return
    await state.update_data(content_type=content_type, text=message.html_text or "", file_id=file_id)
    await state.set_state(FeedAddStates.waiting_days)
    await message.answer(
        "На сколько дней хранить в архиве? Пришлите число (например, 14), "
        "или «-», чтобы хранить бессрочно (пока сами не удалите):"
    )


@router.message(FeedAddStates.waiting_days)
async def adm_feed_add_days(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    text = text.strip()
    days = None
    if text != "-":
        try:
            days = int(text)
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer(
                "Нужно целое число больше нуля, или «-» для бессрочного хранения. Попробуйте ещё раз:"
            )
            return
    await state.update_data(days=days)
    await state.set_state(FeedAddStates.waiting_allow_questions)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, разрешить", callback_data="fa_q_yes")],
        [InlineKeyboardButton(text="Нет, без вопросов", callback_data="fa_q_no")],
    ])
    await message.answer("Разрешить людям задавать вопросы под этой публикацией?", reply_markup=kb)


async def _feed_add_finalize(message: Message, state: FSMContext, allow_questions: bool):
    data = await state.get_data()
    await state.clear()
    days = data.get("days")
    expires_at = (_today() + timedelta(days=days)).isoformat() if days else None
    db.add_feed_post(
        data["content_type"], data.get("text") or "", data.get("file_id"), None,
        expires_at, allow_questions=allow_questions,
    )
    await message.answer(f"Добавлено в «{BTN_FEED}» ✅")


@router.callback_query(FeedAddStates.waiting_allow_questions, F.data == "fa_q_yes")
async def adm_feed_add_questions_yes(callback: CallbackQuery, state: FSMContext):
    await _feed_add_finalize(callback.message, state, True)
    await callback.answer()


@router.callback_query(FeedAddStates.waiting_allow_questions, F.data == "fa_q_no")
async def adm_feed_add_questions_no(callback: CallbackQuery, state: FSMContext):
    await _feed_add_finalize(callback.message, state, False)
    await callback.answer()


# ---------- мой профиль ----------

@router.message(F.text == BTN_PROFILE)
async def show_profile(message: Message):
    user_id = message.from_user.id
    today = _today()

    blocks = []

    user_row = db.get_user(user_id)
    if user_row and user_row["created_at"]:
        try:
            joined_date = datetime.strptime(user_row["created_at"], "%Y-%m-%d %H:%M:%S").date()
            days = (today - joined_date).days
            join_line = (
                db.get_setting("profile_join_date_text")
                .replace("{дата}", joined_date.strftime('%d.%m.%Y'))
                .replace("{дней}", _ru_days(days))
            )
            blocks.append(join_line)
        except ValueError:
            pass

    membership = db.get_sanctum_membership(user_id)
    sanctum_header = db.get_setting("profile_sanctum_header_text").replace("{название}", html.escape(SANCTUM_FULL_NAME))
    sanctum_lines = [sanctum_header]
    sanctum_button = None
    if membership and membership["status"] != "removed" and membership["valid_until"]:
        valid_until = _parse_date(membership["valid_until"])
        if valid_until and valid_until >= today:
            sanctum_lines.append(
                db.get_setting("profile_sanctum_active_text").replace("{дата}", valid_until.strftime('%d.%m.%Y'))
            )
        else:
            date_str = valid_until.strftime('%d.%m.%Y') if valid_until else ""
            sanctum_lines.append(db.get_setting("profile_sanctum_expired_text").replace("{дата}", date_str))
            renew_label = db.get_setting("profile_btn_renew_text")
            sanctum_button = InlineKeyboardButton(text=renew_label, callback_data="sanctum_apply")
        if membership["promise_date"]:
            promise = _parse_date(membership["promise_date"])
            if promise:
                sanctum_lines.append(
                    db.get_setting("profile_sanctum_promise_text").replace("{дата}", promise.strftime('%d.%m.%Y'))
                )
    else:
        sanctum_lines.append(db.get_setting("profile_sanctum_never_joined_text"))
        sanctum_button = InlineKeyboardButton(text=BTN_SANCTUM, callback_data="open_sanctum")
    blocks.append("\n".join(sanctum_lines))

    regs = [
        r for r in db.get_user_registrations(user_id)
        if r["product_type"] == "webinar" and r["status"] == "confirmed"
    ]
    webinar_lines = [db.get_setting("profile_webinars_header_text")]
    if regs:
        for r in regs:
            webinar_lines.append(f"• {r['product_title']}")
    else:
        webinar_lines.append(db.get_setting("profile_webinars_empty_text"))
    blocks.append("\n".join(webinar_lines))

    blocks.append(
        db.get_setting("profile_meditation_reminder_text")
    )

    level = compute_ascension_level(user_id)
    luminar_count = user_row["luminar_count"] if user_row else 0
    luminar_rank = _luminar_rank(luminar_count)
    step_label = db.get_setting("profile_path_step_label_text").replace("{ступень}", ASCENSION_LEVEL_NAMES[level])
    path_lines = [step_label]
    if luminar_rank:
        rank_label = db.get_setting("profile_luminar_rank_label_text").replace("{ранг}", LUMINAR_LABELS[luminar_rank])
        if luminar_rank < 3:
            # прогресс к СЛЕДУЮЩЕМУ ключу — только пока не достигнут максимальный
            # ранг (для Люминар III следующего пока не существует)
            next_threshold = 10 if luminar_rank == 1 else 30
            next_rank_name = LUMINAR_RANK_NAMES[luminar_rank + 1]
            filled = max(0, min(10, round(luminar_count / next_threshold * 10)))
            bar = "▓" * filled + "░" * (10 - filled)
            progress_line = (
                db.get_setting("profile_luminar_progress_next_text")
                .replace("{следующий_ранг}", next_rank_name)
                .replace("{бар}", bar)
                .replace("{число}", str(luminar_count))
                .replace("{порог}", str(next_threshold))
            )
            rank_label = rank_label + "\n" + progress_line
        path_lines.append(rank_label)
    path_lines.append(_personalize(db.get_setting(f"ascension_level{level}_brief_text"), user_row))
    blocks.append("\n\n".join(path_lines))

    luminar_teaser = db.get_setting("luminar_intro_short_text")
    if luminar_rank == 0:
        # ранга ещё нет — показываем либо "путь к первому ключу" (кто-то уже
        # пришёл по ссылке), либо нейтральное приглашение (пока никто не пришёл)
        if luminar_count > 0:
            remaining = 5 - luminar_count
            luminar_teaser += "\n" + (
                db.get_setting("profile_luminar_progress_text")
                .replace("{число}", str(luminar_count))
                .replace("{человек}", _ru_human_word(luminar_count))
                .replace("{осталось}", str(remaining))
            )
        else:
            luminar_teaser += "\n" + db.get_setting("profile_luminar_progress_zero_text")

    me = await message.bot.get_me()
    ref_link = _referral_link(me.username, user_id)
    referral_template = db.get_setting("profile_referral_intro_text")
    # {ссылка} — та самая настоящая реферальная ссылка; если её случайно убрали
    # или опечатали при редактировании текста, всё равно дописываем ссылку в
    # конце, чтобы реферальная функция не сломалась из-за правки формулировки
    if "{ссылка}" in referral_template:
        referral_text = referral_template.replace("{ссылка}", ref_link)
    else:
        referral_text = f"{referral_template}\n\n{ref_link}"
    blocks.append(luminar_teaser + "\n\n" + referral_text)

    kb_rows = []
    if sanctum_button:
        kb_rows.append([sanctum_button])
    kb_rows.append([InlineKeyboardButton(
        text=db.get_setting("profile_btn_read_level_text"), callback_data=f"open_level_msg_{level}"
    )])
    kb_rows.append([InlineKeyboardButton(
        text=db.get_setting("profile_btn_luminar_intro_text"), callback_data="open_luminar_intro"
    )])
    kb_rows.append([InlineKeyboardButton(
        text=db.get_setting("profile_btn_path_overview_text"), callback_data="open_path_overview"
    )])
    meditation_btn = _meditation_button()
    if meditation_btn:
        kb_rows.append([meditation_btn])
    if membership and membership["intention_text"]:
        kb_rows.append([InlineKeyboardButton(text="✏️ Изменить намерение", callback_data="edit_intention")])
    if _ritual_is_member(user_id):
        kb_rows.append([_ritual_calendar_btn()])

    greeting = _personalize(db.get_setting("profile_greeting_text"), user_row)
    text = f"{greeting}\n\n" + "\n\n".join(blocks)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


# ---------- приём скриншота чека ----------

def _sanctum_payment_context(user_id: int) -> str:
    """Короткая подсказка для вас при рассмотрении оплаты VEDA SANCTUM — новая
    это заявка, продление ещё активной подписки, возобновление после паузы,
    или возврат человека, которого раньше убирали из канала — чтобы не
    приходилось выяснять это отдельно и не путаться."""
    membership = db.get_sanctum_membership(user_id)
    if not membership or not membership["valid_until"]:
        return "\n🆕 Первая оплата VEDA SANCTUM"
    if membership["status"] == "removed":
        return "\n🔁 Повторный вход (ранее был убран из VEDA SANCTUM)"
    valid_until = _parse_date(membership["valid_until"])
    if valid_until and valid_until >= _today():
        return f"\n🔄 Продление (сейчас активно до {valid_until.strftime('%d.%m.%Y')})"
    date_part = valid_until.strftime('%d.%m.%Y') if valid_until else "ранее"
    return f"\n🔄 Возобновление (истекло {date_part})"


async def _process_receipt_photo(message: Message, reg_id) -> bool:
    """Общая логика приёма чека — прикрепляет фото к заявке, отвечает
    человеку и уведомляет админов. Используется и в обычном сценарии
    (receipt_received, состояние в порядке), и в подстраховке (photo_fallback,
    main.py, ниже — когда состояние ожидания было потеряно). Возвращает
    False, если заявки уже нет или она не в статусе ожидания чека."""
    reg = db.get_registration(reg_id) if reg_id else None
    if not reg or reg["status"] != "awaiting_receipt":
        return False

    file_id = message.photo[-1].file_id
    db.attach_receipt(reg_id, file_id)
    await message.answer("Спасибо! Чек отправлен на проверку, я сообщу Вам о результате 🙏")

    user = message.from_user
    username_part = f"@{user.username}" if user.username else "(без username)"
    context_note = _sanctum_payment_context(user.id) if reg["product_type"] == "sanctum" else ""
    caption = (
        "🆕 Новая оплата на проверку\n\n"
        f"Пользователь: {username_part}\n"
        f"ID: {user.id}\n"
        f"Продукт: {reg['product_title']}\n"
        f"Сумма: {reg['price']}"
        f"{context_note}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"reg_confirm_{reg_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg_decline_{reg_id}"),
    ]])
    for admin_id in db.get_all_admin_ids():
        try:
            await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb)
        except Exception:
            logging.exception("Не удалось отправить чек админу %s", admin_id)
    return True


@router.message(ReceiptStates.waiting_receipt, F.photo)
async def receipt_received(message: Message, state: FSMContext):
    data = await state.get_data()
    reg_id = data.get("reg_id")
    ok = await _process_receipt_photo(message, reg_id)
    await state.clear()
    if not ok:
        await message.answer("Не нашла активную заявку. Попробуйте зарегистрироваться заново.")


@router.message(ReceiptStates.waiting_receipt)
async def receipt_wrong_type(message: Message):
    await message.answer("Пришлите, пожалуйста, именно скриншот (фото) чека 📸")


async def _mark_admin_message_done(callback: CallbackQuery, note: str):
    try:
        if callback.message.caption is not None:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n" + note)
        else:
            await callback.message.edit_text(callback.message.text + "\n\n" + note)
    except Exception:
        pass


@router.callback_query(F.data.startswith("reg_confirm_"))
async def reg_confirm(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    reg_id = int(callback.data.split("_")[-1])
    reg = db.get_registration(reg_id)
    if not reg or reg["status"] != "awaiting_confirmation":
        await callback.answer("Эта заявка уже обработана", show_alert=True)
        return

    db.set_registration_status(reg_id, "confirmed")

    if reg["product_type"] == "webinar":
        w = db.get_webinar(reg["product_id"])
        invite_link = w["invite_link"] if w else ""
    else:
        s = db.get_sanctum()
        invite_link = s["invite_link"] if s else ""

    text = f"✅ Оплата за «{reg['product_title']}» подтверждена!"
    text += f"\n\nВот Ваша ссылка:\n{invite_link}" if invite_link else "\n\nСсылку пришлю Вам отдельно."

    old_level = None
    new_level = None
    is_first_sanctum_payment = False
    if reg["product_type"] == "sanctum":
        prior_membership = db.get_sanctum_membership(reg["user_id"])
        is_first_sanctum_payment = not (prior_membership and prior_membership["valid_until"])
        old_level = compute_ascension_level(reg["user_id"])
        valid_until, locked_price = _extend_sanctum_membership(reg["user_id"], price=reg["price"])
        text += (
            f"\n\nПодписка активна до {valid_until.strftime('%d.%m.%Y')} по цене {locked_price}. "
            "Я напомню заранее, когда придёт время продлевать подписку."
        )
        new_level = compute_ascension_level(reg["user_id"])

    # Сначала — само подтверждение оплаты, и только потом (если есть переход
    # ступени) поздравление и ритуал намерения: человек должен сперва увидеть,
    # что оплата прошла, а уже следом - что это открыло ему новую ступень
    # (упорядочено 2026-09-03 - раньше сообщение о ступени/намерении уходило
    # РАНЬШЕ подтверждения оплаты, потому что _handle_ascension_transition
    # вызывался до этой отправки)
    paid_kb = None
    if reg["product_type"] == "sanctum" and is_first_sanctum_payment:
        ritual_paid_line = db.get_setting("ritual_paid_line")
        if ritual_paid_line:
            text += "\n\n" + ritual_paid_line
            paid_kb = InlineKeyboardMarkup(inline_keyboard=[[_ritual_calendar_btn()]])
    try:
        await callback.bot.send_message(reg["user_id"], text, reply_markup=paid_kb)
    except Exception:
        logging.exception("Не удалось отправить подтверждение пользователю %s", reg["user_id"])

    if reg["product_type"] == "sanctum":
        if is_first_sanctum_payment:
            await _credit_luminar_referral(callback.bot, reg["user_id"])
        await _handle_ascension_transition(callback.bot, reg["user_id"], old_level, new_level)

    await _mark_admin_message_done(callback, "✅ ПОДТВЕРЖДЕНО")
    await callback.answer("Подтверждено")


@router.callback_query(F.data.startswith("reg_decline_"))
async def reg_decline(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    reg_id = int(callback.data.split("_")[-1])
    reg = db.get_registration(reg_id)
    if not reg or reg["status"] != "awaiting_confirmation":
        await callback.answer("Эта заявка уже обработана", show_alert=True)
        return

    db.set_registration_status(reg_id, "declined")
    try:
        await callback.bot.send_message(
            reg["user_id"],
            f"❌ Оплату за «{reg['product_title']}» не удалось подтвердить.\n\n"
            "Если хотите прислать чек ещё раз - нажмите «📸 Отправить чек».\n"
            "Если остались вопросы - нажмите «💌 Личное обращение», напишите сообщение, "
            "и я отвечу Вам здесь же 🙏",
            reply_markup=_receipt_help_kb(reg_id),
        )
    except Exception:
        logging.exception("Не удалось отправить отказ пользователю %s", reg["user_id"])

    await _mark_admin_message_done(callback, "❌ ОТКЛОНЕНО")
    await callback.answer("Отклонено")


# ---------- переписка с администратором прямо в боте ----------

def _receipt_help_kb(reg_id: int) -> InlineKeyboardMarkup:
    """Для сообщений о конкретной незавершённой/отклонённой заявке — рядом
    с "написать" даём прямой путь прислать чек ещё раз, не создавая новую
    заявку, а довозвращая именно эту в очередь на проверку."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Отправить чек", callback_data=f"resend_receipt_{reg_id}")],
        [InlineKeyboardButton(text="💌 Личное обращение", callback_data="contact_admin")],
    ])


@router.callback_query(F.data.startswith("resend_receipt_"))
async def resend_receipt_start(callback: CallbackQuery, state: FSMContext):
    reg_id = int(callback.data[len("resend_receipt_"):])
    reg = db.get_registration(reg_id)
    if not reg:
        await callback.answer("Эта заявка больше не найдена.", show_alert=True)
        return
    if reg["status"] == "confirmed":
        await callback.answer("Эта оплата уже подтверждена ✅", show_alert=True)
        return
    if reg["status"] == "awaiting_confirmation":
        await callback.answer("Чек уже отправлен и ожидает проверки 🙏", show_alert=True)
        return
    await state.set_state(ReceiptStates.waiting_receipt)
    await state.update_data(reg_id=reg_id)
    await callback.message.answer(f"Пришлите, пожалуйста, скриншот чека по «{reg['product_title']}» 📸")
    await callback.answer()


@router.callback_query(F.data == "contact_admin")
async def contact_admin_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ContactAdminStates.waiting_message)
    await callback.message.answer(
        "Напишите Ваше сообщение (можно текстом, фото, голосовое) - я его передам и Вам ответят здесь же 🙏"
    )
    await callback.answer()


@router.message(ContactAdminStates.waiting_message)
async def contact_admin_received(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    user_row = db.get_user(user.id)
    name = _user_display_name(user_row) if user_row else (f"@{user.username}" if user.username else str(user.id))
    header = f"✉️ Сообщение от {html.escape(name)}\nID: {user.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Ответить", callback_data=f"admin_reply_{user.id}"),
        InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm_user_view_{user.id}"),
    ]])
    delivered = False
    for admin_id in db.get_all_admin_ids():
        try:
            await message.bot.send_message(admin_id, header, reply_markup=kb)
            await message.bot.copy_message(chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id)
            delivered = True
        except Exception:
            logging.exception("Не удалось передать сообщение администратору %s", admin_id)
    if delivered:
        await message.answer("Благодарю, я передала Ваше сообщение 🙏\nОтвет придёт здесь же, в этом чате.")
    else:
        await message.answer("Не получилось передать сообщение, попробуйте ещё раз чуть позже 🙏")


@router.callback_query(F.data.startswith("admin_reply_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    target_user_id = int(callback.data[len("admin_reply_"):])
    target_row = db.get_user(target_user_id)
    name = _user_display_name(target_row) if target_row else str(target_user_id)
    await state.set_state(AdminReplyStates.waiting_reply)
    await state.update_data(target_user_id=target_user_id)
    await callback.message.answer(f"Напишите ответ для {name} (текст, фото - что угодно):")
    await callback.answer()


@router.message(AdminReplyStates.waiting_reply)
async def admin_reply_send(message: Message, state: FSMContext):
    if not db.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    await state.clear()
    try:
        await message.bot.copy_message(chat_id=target_user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        await message.answer("Ответ отправлен ✅")
    except Exception:
        logging.exception("Не удалось отправить ответ пользователю %s", target_user_id)
        await message.answer(
            "Не удалось отправить - возможно, человек заблокировал бота или ни разу не писал ему."
        )


# ---------- вопросы под вебинарами и публикациями архива ----------

def _question_ref_label(ref_type: str, ref_title: str) -> str:
    kind = "вебинару" if ref_type == "webinar" else "публикации"
    clean_title = _strip_html_tags(ref_title).strip()
    return f"{kind} «{html.escape(clean_title)}»" if clean_title else kind


@router.callback_query(F.data.startswith("wq_ask_webinar_"))
async def wq_ask_webinar_start(callback: CallbackQuery, state: FSMContext):
    webinar_id = int(callback.data[len("wq_ask_webinar_"):])
    w = db.get_webinar(webinar_id)
    if not w or not w["allow_questions"]:
        await callback.answer("Вопросы сейчас недоступны.", show_alert=True)
        return
    await state.set_state(QuestionStates.waiting_question)
    await state.update_data(ref_type="webinar", ref_id=webinar_id, ref_title=w["title"])
    await callback.message.answer("Напишите Ваш вопрос, я передам его Алёне 🙏")
    await callback.answer()


@router.callback_query(F.data.startswith("wq_ask_feed_"))
async def wq_ask_feed_start(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data[len("wq_ask_feed_"):])
    post = db.get_feed_post(post_id)
    if not post or not post["allow_questions"]:
        await callback.answer("Вопросы сейчас недоступны.", show_alert=True)
        return
    await state.set_state(QuestionStates.waiting_question)
    await state.update_data(ref_type="feed_post", ref_id=post_id, ref_title=_feed_post_short_label(post))
    await callback.message.answer("Напишите Ваш вопрос, я передам его Алёне 🙏")
    await callback.answer()


@router.message(QuestionStates.waiting_question)
async def question_received(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    data = await state.get_data()
    await state.clear()
    ref_type = data["ref_type"]
    ref_id = data["ref_id"]
    ref_title = data.get("ref_title") or ""
    q_id = db.add_question(ref_type, ref_id, ref_title, message.from_user.id, text)

    user_row = db.get_user(message.from_user.id)
    name = _user_display_name(user_row) if user_row else str(message.from_user.id)
    header = (
        f"❓ Новый вопрос к {_question_ref_label(ref_type, ref_title)}\n\n"
        f"От: {html.escape(name)}\nID: {message.from_user.id}\n\n"
        f"{html.escape(text)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"q_answer_{q_id}"),
        InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm_user_view_{message.from_user.id}"),
    ]])
    delivered = False
    for admin_id in db.get_all_admin_ids():
        try:
            await message.bot.send_message(admin_id, header, reply_markup=kb)
            delivered = True
        except Exception:
            logging.exception("Не удалось передать вопрос администратору %s", admin_id)
    if delivered:
        await message.answer("Спасибо, я передала Ваш вопрос 🙏")
    else:
        await message.answer("Не получилось передать вопрос, попробуйте ещё раз чуть позже 🙏")


@router.callback_query(F.data.startswith("q_answer_"))
async def q_answer_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    q_id = int(callback.data[len("q_answer_"):])
    q = db.get_question(q_id)
    if not q:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await state.set_state(AnswerQuestionStates.waiting_answer)
    await state.update_data(q_id=q_id)
    await callback.message.answer(f"Вопрос:\n{html.escape(q['question_text'])}\n\nНапишите ответ:")
    await callback.answer()


@router.message(AnswerQuestionStates.waiting_answer)
async def q_answer_text_received(message: Message, state: FSMContext):
    text = await _require_html_text(message)
    if text is None:
        return
    await state.update_data(answer_text=text)
    await state.set_state(AnswerQuestionStates.waiting_visibility)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Отправить только этому человеку", callback_data="q_vis_private")],
        [InlineKeyboardButton(text="🌍 Сделать публичным - видно всем под постом", callback_data="q_vis_public")],
    ])
    await message.answer("Как отправить ответ? (по умолчанию рекомендую личный)", reply_markup=kb)


async def _q_answer_finalize(callback: CallbackQuery, state: FSMContext, is_public: bool):
    data = await state.get_data()
    await state.clear()
    q_id = data.get("q_id")
    answer_text = data.get("answer_text")
    q = db.get_question(q_id) if q_id else None
    if not q:
        await callback.answer("Вопрос уже недоступен", show_alert=True)
        return
    db.set_question_answer(q_id, answer_text, is_public)
    try:
        await callback.bot.send_message(q["user_id"], f"💬 Ответ на Ваш вопрос:\n\n{answer_text}")
    except Exception:
        logging.exception("Не удалось отправить ответ на вопрос пользователю %s", q["user_id"])
    note = "опубликован под постом ✅" if is_public else "отправлен лично ✅"
    await callback.message.edit_text(f"Ответ {note}")
    await callback.answer()


@router.callback_query(AnswerQuestionStates.waiting_visibility, F.data == "q_vis_private")
async def q_answer_private(callback: CallbackQuery, state: FSMContext):
    await _q_answer_finalize(callback, state, False)


@router.callback_query(AnswerQuestionStates.waiting_visibility, F.data == "q_vis_public")
async def q_answer_public(callback: CallbackQuery, state: FSMContext):
    await _q_answer_finalize(callback, state, True)


@router.callback_query(F.data.startswith("wq_public_webinar_"))
async def wq_public_webinar(callback: CallbackQuery):
    webinar_id = int(callback.data[len("wq_public_webinar_"):])
    qa = db.get_public_qa("webinar", webinar_id)
    if not qa:
        await callback.answer("Пока нет опубликованных вопросов.", show_alert=True)
        return
    lines = ["<b>💬 Вопросы и ответы</b>\n"]
    for q in qa:
        lines.append(f"❓ {html.escape(q['question_text'])}\n💬 {q['answer_text']}\n")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data.startswith("wq_public_feed_"))
async def wq_public_feed(callback: CallbackQuery):
    post_id = int(callback.data[len("wq_public_feed_"):])
    qa = db.get_public_qa("feed_post", post_id)
    if not qa:
        await callback.answer("Пока нет опубликованных вопросов.", show_alert=True)
        return
    lines = ["<b>💬 Вопросы и ответы</b>\n"]
    for q in qa:
        lines.append(f"❓ {html.escape(q['question_text'])}\n💬 {q['answer_text']}\n")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ---------- админ-панель: вход ----------

@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin"))
async def admin_panel_entry(message: Message):
    # /admin - запасной вход: кнопка «Админ-панель» живёт в меню внизу экрана,
    # а Telegram запоминает это меню у человека и обновляет его только когда бот
    # пришлёт новое - поэтому у только что добавленного администратора кнопки
    # может ещё не быть (см. _notify_new_admin), а команда работает сразу
    if not db.is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_panel_kb(message.from_user.id))


async def _notify_new_admin(bot: Bot, admin_id: int) -> bool:
    """Сообщает только что добавленному администратору и заодно присылает ему
    обновлённое меню - с кнопкой «Админ-панель». Без этого у него остаётся
    старое меню без неё. False, если написать не получилось (человек ещё ни
    разу не запускал бота)."""
    try:
        await bot.send_message(
            admin_id,
            "✨ Вам открыт доступ к админ-панели. Кнопка «⚙️ Админ-панель» появилась в меню внизу "
            "(если её не видно - откройте панель командой /admin).",
            reply_markup=main_menu_kb(admin_id),
        )
        return True
    except Exception:
        logging.exception("Не удалось уведомить нового администратора %s", admin_id)
        return False


@router.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await callback.message.edit_text("Админ-панель:", reply_markup=admin_panel_kb(callback.from_user.id))
    await callback.answer()


# ---------- админ-панель: аналитика ----------

def _trend_arrow(current: int, previous: int) -> str:
    diff = current - previous
    if diff > 0:
        return f"(📈 +{diff} к прошлой неделе)"
    if diff < 0:
        return f"(📉 {diff} к прошлой неделе)"
    return "(➡️ без изменений)"


def _funnel_summary(funnel: dict):
    total = sum(funnel.values())
    confirmed = funnel.get("confirmed", 0)
    pending = funnel.get("awaiting_receipt", 0) + funnel.get("awaiting_confirmation", 0)
    declined = funnel.get("declined", 0)
    rate = f"{round(confirmed / total * 100)}%" if total else "-"
    return total, confirmed, pending, declined, rate


@router.callback_query(F.data == "adm_analytics")
async def adm_analytics(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_analytics"):
        return

    today = _today()
    total_users = db.get_users_count()
    new_7 = db.get_new_users_count(f"{(today - timedelta(days=7)).isoformat()} 00:00:00")
    new_30 = db.get_new_users_count(f"{(today - timedelta(days=30)).isoformat()} 00:00:00")
    self_departed = db.get_self_departed_count()

    today_iso = today.isoformat()
    sanctum_active = len(db.get_sanctum_active_user_ids(today_iso))
    sanctum_expired = len(db.get_sanctum_expired_user_ids(today_iso))
    sanctum_removed = len(db.get_sanctum_removed_user_ids())
    sanctum_never = len(db.get_sanctum_never_user_ids())

    wb_total, wb_conf, wb_pend, wb_decl, wb_rate = _funnel_summary(db.get_registration_funnel("webinar"))
    sc_total, sc_conf, sc_pend, sc_decl, sc_rate = _funnel_summary(db.get_registration_funnel("sanctum"))

    text = (
        "<b>📊 Аналитика</b>\n\n"
        f"👥 Всего в боте: {total_users} (за 7 дней: +{new_7}, за 30 дней: +{new_30})\n"
        f"👋 Сами заблокировали бота / вышли: {self_departed}\n\n"
        f"<b>⚜️ VEDA SANCTUM</b>\n"
        f"✅ Активных: {sanctum_active}\n"
        f"❌ Истёкших: {sanctum_expired}\n"
        f"🚫 Удалённых: {sanctum_removed}\n"
        f"🌱 Никогда не были: {sanctum_never}\n\n"
        f"<b>📅 Заявки на вебинары</b>\n"
        f"Всего: {wb_total}\n"
        f"✅ Подтверждено: {wb_conf} ({wb_rate})\n"
        f"⏳ В процессе: {wb_pend}\n"
        f"❌ Отклонено: {wb_decl}\n\n"
        f"<b>⚜️ Заявки на VEDA SANCTUM</b>\n"
        f"Всего: {sc_total}\n"
        f"✅ Подтверждено: {sc_conf} ({sc_rate})\n"
        f"⏳ В процессе: {sc_pend}\n"
        f"❌ Отклонено: {sc_decl}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Динамика (тренды)", callback_data="adm_analytics_trends")],
        [InlineKeyboardButton(text="📈 По каждому вебинару", callback_data="adm_analytics_webinars")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm_analytics_trends")
async def adm_analytics_trends(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return

    today = _today()
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    this_week_start = (today - timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")
    prev_week_start = (today - timedelta(days=13)).strftime("%Y-%m-%d 00:00:00")
    prev_week_end = this_week_start

    new_this_week = db.get_users_count_in_range(this_week_start, now_str)
    new_prev_week = db.get_users_count_in_range(prev_week_start, prev_week_end)
    paid_this_week = db.get_confirmed_count_in_range(this_week_start, now_str)
    paid_prev_week = db.get_confirmed_count_in_range(prev_week_start, prev_week_end)

    users_by_day = dict(db.get_new_users_by_day(7))
    paid_by_day = dict(db.get_confirmed_by_day(7))
    day_lines = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        d_iso = d.isoformat()
        day_lines.append(f"{d.strftime('%d.%m')}: 👤{users_by_day.get(d_iso, 0)}  💳{paid_by_day.get(d_iso, 0)}")

    stall_hours = int(db.get_setting("stall_hours") or "24")
    stall_cutoff = (datetime.now(TZ) - timedelta(hours=stall_hours)).strftime("%Y-%m-%d %H:%M:%S")
    stalled_now = len(db.get_stalled_registrations(stall_cutoff))

    text = (
        "<b>📈 Динамика</b>\n\n"
        f"👤 Новых людей: эта неделя {new_this_week}, прошлая {new_prev_week} "
        f"{_trend_arrow(new_this_week, new_prev_week)}\n"
        f"💳 Подтверждённых оплат: эта неделя {paid_this_week}, прошлая {paid_prev_week} "
        f"{_trend_arrow(paid_this_week, paid_prev_week)}\n\n"
        "<b>По дням, последние 7 дней</b> (👤 новые люди, 💳 оплаты):\n" + "\n".join(day_lines) + "\n\n"
        f"⏳ Прямо сейчас зависли на оплате дольше {stall_hours} ч.: {stalled_now}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_analytics")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm_analytics_webinars")
async def adm_analytics_webinars(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    rows = db.get_webinar_registration_counts()
    if not rows:
        await callback.answer("Пока нет ни одной заявки на вебинары", show_alert=True)
        return

    per_title = {}
    for r in rows:
        per_title.setdefault(r["product_title"], {})[r["status"]] = r["n"]

    lines = ["<b>📈 По вебинарам</b>\n"]
    for title, counts in per_title.items():
        confirmed = counts.get("confirmed", 0)
        pending = counts.get("awaiting_receipt", 0) + counts.get("awaiting_confirmation", 0)
        declined = counts.get("declined", 0)
        lines.append(f"• {title or '(без названия)'} - ✅{confirmed} ⏳{pending} ❌{declined}")

    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ---------- админ-панель: автовозврат "потерянных" людей ----------

REENGAGE_FIELD_LABELS = {
    "stall_text": "текст «завис на оплате»",
    "reengage_text": "текст «пришёл и пропал»",
    "winback_text": "текст «возврат после ухода из Sanctum»",
    "sanctum_nudge_text": "текст «посмотрел, не начал»",
    "sanctum_removed_text": "текст «убрала вручную» (уходит сразу при удалении из Sanctum)",
    "sanctum_lastchance_text": "текст «последний шанс сохранить цену»",
    "sanctum_reset_text": "текст «время в поле обнулилось»",
    "sanctum_reset_luminar_line": "строку про ранг Люминара (для «время в поле обнулилось»)",
    "sanctum_reset_meditation_line": "строку про VEDA HEALING FLOW (для «время в поле обнулилось»)",
}


def _on_off(value: str) -> str:
    return "✅ включено" if value == "1" else "🚫 выключено"


def _reengage_screen_text() -> str:
    stall_enabled = db.get_setting("stall_enabled") or "1"
    stall_hours = db.get_setting("stall_hours") or "24"
    stall_text = db.get_setting("stall_text") or ""
    reengage_enabled = db.get_setting("reengage_enabled") or "1"
    reengage_days = db.get_setting("reengage_days_silent") or "3"
    reengage_text = db.get_setting("reengage_text") or ""
    winback_enabled = db.get_setting("winback_enabled") or "1"
    winback_days = db.get_setting("winback_days_after_expiry") or "7"
    winback_text = db.get_setting("winback_text") or ""
    nudge_enabled = db.get_setting("sanctum_nudge_enabled") or "1"
    nudge_hours = db.get_setting("sanctum_nudge_hours") or "5"
    nudge_text = db.get_setting("sanctum_nudge_text") or ""
    return (
        "<b>🧲 Автовозврат потерянных людей</b>\n\n"
        "Бот сам, один раз на человека, мягко напоминает о себе - без повторов и без спама.\n\n"
        f"<b>1. Завис на оплате</b> - {_on_off(stall_enabled)}\n"
        f"Через {stall_hours} ч. после начала оформления, если чек не пришёл.\n"
        f"Текст: {html.escape(stall_text[:150])}{'…' if len(stall_text) > 150 else ''}\n\n"
        f"<b>2. Пришёл и пропал</b> - {_on_off(reengage_enabled)}\n"
        f"Через {reengage_days} дн. после /start, если человек ничего не покупал и не подавал заявку.\n"
        f"Текст: {html.escape(reengage_text[:150])}{'…' if len(reengage_text) > 150 else ''}\n\n"
        f"<b>3. Возврат после ухода из Sanctum</b> - {_on_off(winback_enabled)}\n"
        f"Через {winback_days} дн. после истечения доступа, если человек так и не продлил "
        "(не касается тех, кого Вы убрали вручную).\n"
        f"Текст: {html.escape(winback_text[:150])}{'…' if len(winback_text) > 150 else ''}\n\n"
        f"<b>4. Посмотрел Sanctum, но не начал оформление</b> - {_on_off(nudge_enabled)}\n"
        f"Через {nudge_hours} ч. после того, как открыл(а) экран VEDA SANCTUM, если так и не нажал(а) "
        "«Инициировать шаг». Получает это напоминание ВМЕСТО «Пришёл и пропал», не вместе с ним.\n"
        f"Текст: {html.escape(nudge_text[:150])}{'…' if len(nudge_text) > 150 else ''}\n\n"
        "<b>5. Правила ухода из Sanctum</b>\n"
        f"Цена сохраняется {db.get_setting('price_lock_days') or '30'} дн. после ухода (потом - как для новых). "
        f"За {db.get_setting('lastchance_days_before') or '5'} дн. до конца срока приходит «последний шанс». "
        f"Через {db.get_setting('stage_reset_days') or '90'} дн. отсутствия время в поле для ступеней "
        "обнуляется (ранг Люминара и покупка медитаций остаются). При удалении вручную человеку сразу "
        "уходит письмо с этими правилами."
    )


def _reengage_screen_kb() -> InlineKeyboardMarkup:
    stall_enabled = db.get_setting("stall_enabled") or "1"
    reengage_enabled = db.get_setting("reengage_enabled") or "1"
    winback_enabled = db.get_setting("winback_enabled") or "1"
    nudge_enabled = db.get_setting("sanctum_nudge_enabled") or "1"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("🚫 Выключить" if stall_enabled == "1" else "✅ Включить") + " «завис на оплате»",
            callback_data="adm_reeng_toggle_stall",
        )],
        [InlineKeyboardButton(text="✏️ Текст «завис на оплате»", callback_data="adm_rg_stall_text")],
        [InlineKeyboardButton(text="⏱ Через сколько часов", callback_data="adm_rg_stall_hours")],
        [InlineKeyboardButton(
            text=("🚫 Выключить" if reengage_enabled == "1" else "✅ Включить") + " «пришёл и пропал»",
            callback_data="adm_reeng_toggle_silent",
        )],
        [InlineKeyboardButton(text="✏️ Текст «пришёл и пропал»", callback_data="adm_rg_reengage_text")],
        [InlineKeyboardButton(text="⏱ Через сколько дней", callback_data="adm_rg_reengage_days_silent")],
        [InlineKeyboardButton(
            text=("🚫 Выключить" if winback_enabled == "1" else "✅ Включить") + " «возврат после ухода из Sanctum»",
            callback_data="adm_reeng_toggle_winback",
        )],
        [InlineKeyboardButton(text="✏️ Текст «возврат после ухода из Sanctum»", callback_data="adm_rg_winback_text")],
        [InlineKeyboardButton(text="⏱ Через сколько дней", callback_data="adm_rg_winback_days_after_expiry")],
        [InlineKeyboardButton(
            text=("🚫 Выключить" if nudge_enabled == "1" else "✅ Включить") + " «посмотрел, не начал»",
            callback_data="adm_reeng_toggle_nudge",
        )],
        [InlineKeyboardButton(text="✏️ Текст «посмотрел, не начал»", callback_data="adm_rg_sanctum_nudge_text")],
        [InlineKeyboardButton(text="⏱ Через сколько часов", callback_data="adm_rg_sanctum_nudge_hours")],
        [InlineKeyboardButton(text="✏️ Текст «убрала вручную»", callback_data="adm_rg_sanctum_removed_text")],
        [InlineKeyboardButton(text="✏️ Текст «последний шанс»", callback_data="adm_rg_sanctum_lastchance_text")],
        [InlineKeyboardButton(text="✏️ Текст «время в поле обнулилось»", callback_data="adm_rg_sanctum_reset_text")],
        [InlineKeyboardButton(text="✏️ Строка про Люминара", callback_data="adm_rg_sanctum_reset_luminar_line"),
         InlineKeyboardButton(text="✏️ Строка про медитации", callback_data="adm_rg_sanctum_reset_meditation_line")],
        [InlineKeyboardButton(text="⏱ Дней сохранения цены", callback_data="adm_rg_price_lock_days"),
         InlineKeyboardButton(text="⏱ Дней до обнуления", callback_data="adm_rg_stage_reset_days")],
        [InlineKeyboardButton(text="⏱ За сколько дней напомнить", callback_data="adm_rg_lastchance_days_before")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])


@router.callback_query(F.data == "adm_reengage")
async def adm_reengage(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_reengage"):
        return
    await callback.message.edit_text(_reengage_screen_text(), reply_markup=_reengage_screen_kb())
    await callback.answer()


@router.callback_query(F.data == "adm_reeng_toggle_stall")
async def adm_reeng_toggle_stall(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    current = db.get_setting("stall_enabled") or "1"
    db.set_setting("stall_enabled", "0" if current == "1" else "1")
    await callback.message.edit_text(_reengage_screen_text(), reply_markup=_reengage_screen_kb())
    await callback.answer()


@router.callback_query(F.data == "adm_reeng_toggle_silent")
async def adm_reeng_toggle_silent(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    current = db.get_setting("reengage_enabled") or "1"
    db.set_setting("reengage_enabled", "0" if current == "1" else "1")
    await callback.message.edit_text(_reengage_screen_text(), reply_markup=_reengage_screen_kb())
    await callback.answer()


@router.callback_query(F.data == "adm_reeng_toggle_winback")
async def adm_reeng_toggle_winback(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    current = db.get_setting("winback_enabled") or "1"
    db.set_setting("winback_enabled", "0" if current == "1" else "1")
    await callback.message.edit_text(_reengage_screen_text(), reply_markup=_reengage_screen_kb())
    await callback.answer()


@router.callback_query(F.data == "adm_reeng_toggle_nudge")
async def adm_reeng_toggle_nudge(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    current = db.get_setting("sanctum_nudge_enabled") or "1"
    db.set_setting("sanctum_nudge_enabled", "0" if current == "1" else "1")
    await callback.message.edit_text(_reengage_screen_text(), reply_markup=_reengage_screen_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rg_"))
async def adm_reengage_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_rg_"):]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="reengage", field=field)
    current = db.get_setting(field) or ""
    current_block = f"Сейчас:\n{html.escape(current)}\n\n"
    if field == "stall_hours":
        prompt = (
            f"{current_block}Через сколько часов после начала оформления напоминать, если чек не пришёл? "
            "Пришлите число (например, 24):"
        )
    elif field == "reengage_days_silent":
        prompt = (
            f"{current_block}Через сколько дней после /start напоминать, если человек ничего не покупал? "
            "Пришлите число (например, 3):"
        )
    elif field == "winback_days_after_expiry":
        prompt = (
            f"{current_block}Через сколько дней после истечения доступа напоминать, если человек так и "
            "не продлил? Пришлите число (например, 7):"
        )
    elif field in ("price_lock_days", "stage_reset_days", "lastchance_days_before"):
        what = {
            "price_lock_days": "сколько дней после ухода из Sanctum сохраняется прежняя цена (сейчас по договорённости - 30)",
            "stage_reset_days": "через сколько дней отсутствия обнуляется время в поле для ступеней (сейчас по договорённости - 90)",
            "lastchance_days_before": "за сколько дней до конца срока сохранения цены прислать «последний шанс» (сейчас - 5)",
        }[field]
        prompt = f"{current_block}Пришлите число: {what}."
    elif field in ("sanctum_removed_text", "sanctum_lastchance_text"):
        prompt = (
            f"{current_block}Пришлите новый {REENGAGE_FIELD_LABELS[field]}.\n\n"
            "Можно вставить <code>{имя}</code>, <code>{дата}</code> (когда закончился доступ), "
            "<code>{дата_до}</code> (последний день сохранения цены) и <code>{цена}</code>."
        )
    elif field == "sanctum_reset_text":
        prompt = (
            f"{current_block}Пришлите новый {REENGAGE_FIELD_LABELS[field]}.\n\n"
            "Можно вставить <code>{имя}</code>. Метка <code>{сохранено}</code> - сюда бот сам подставит "
            "строки про ранг Люминара и про VEDA HEALING FLOW, но только тем, у кого они есть."
        )
    elif field == "sanctum_nudge_hours":
        prompt = (
            f"{current_block}Через сколько часов после просмотра VEDA SANCTUM напоминать, если человек "
            "так и не нажал «Инициировать шаг»? Пришлите число (например, 5):"
        )
    elif field == "stall_text":
        prompt = (
            f"{current_block}Пришлите новый {REENGAGE_FIELD_LABELS[field]}.\n\n"
            "Можно вставить <code>{product}</code> (название того, что не оплачено) "
            "и <code>{имя}</code> (бот подставит имя человека)."
        )
    elif field == "winback_text":
        prompt = (
            f"{current_block}Пришлите новый {REENGAGE_FIELD_LABELS[field]}.\n\n"
            "Можно вставить <code>{имя}</code> (имя человека), <code>{дата}</code> (когда истёк доступ), "
            "<code>{дата_до}</code> (последний день сохранения цены) и <code>{цена}</code> (закреплённая за ним цена)."
        )
    else:
        prompt = (
            f"{current_block}Пришлите новый {REENGAGE_FIELD_LABELS[field]}.\n\n"
            "Можно вставить <code>{имя}</code> - бот подставит имя человека (или «друг», если имени нет)."
        )
    await callback.message.answer(prompt)
    await callback.answer()


# ---------- админ-панель: вебинары ----------

async def _render_webinars_list(callback: CallbackQuery):
    webinars = db.get_all_webinars()
    rows = []
    for w in webinars:
        status = "🟢" if w["is_active"] else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{status} {_strip_html_tags(w['title'])}", callback_data=f"adm_wb_edit_{w['id']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить вебинар", callback_data="adm_wb_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("Вебинары:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _render_webinar_card(callback: CallbackQuery, webinar_id: int) -> bool:
    w = db.get_webinar(webinar_id)
    if not w:
        await callback.answer("Вебинар не найден", show_alert=True)
        return False
    status_text = "включён, открыт для регистрации 🟢" if w["is_active"] else "выключен, показывается людям как прошедший 📜"
    photo_status = "установлено" if w["photo"] else "не установлено"
    questions_status = "разрешены ✅" if w["allow_questions"] else "выключены 🔴"
    qa_count = db.count_public_qa("webinar", webinar_id)
    type_label = EVENT_TYPE_LABELS.get(w["event_type"] or "webinar", "Вебинар")
    dt_status = "задана ✅ (напоминания будут работать)" if w["event_dt"] else "не задана ⚠️ (автоматические напоминания работать не будут)"
    text = (
        f"{w['title']}\n\n"
        f"{w['description']}\n\n"
        f"Тип: {type_label}\n"
        f"🗓 {w['date_text']}\n"
        f"Настоящая дата/время для напоминаний: {dt_status}\n"
        f"💳 {w['price']}\n"
        f"🔗 {w['invite_link'] or '-'}\n"
        f"🖼 Фото: {photo_status}\n"
        f"❓ Вопросы от людей: {questions_status} (опубликовано ответов: {qa_count})\n\n"
        f"Статус: {status_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"adm_wbf_t_{webinar_id}"),
         InlineKeyboardButton(text="✏️ Тип", callback_data=f"adm_wbtype_{webinar_id}")],
        [InlineKeyboardButton(text="✏️ Описание", callback_data=f"adm_wbf_d_{webinar_id}")],
        [InlineKeyboardButton(text="✏️ Дата и время", callback_data=f"adm_wbf_dt_{webinar_id}"),
         InlineKeyboardButton(text="✏️ Цена", callback_data=f"adm_wbf_p_{webinar_id}")],
        [InlineKeyboardButton(text="✏️ Ссылка", callback_data=f"adm_wbf_l_{webinar_id}"),
         InlineKeyboardButton(text="🖼 Фото", callback_data=f"adm_photo_webinar_{webinar_id}")],
        [InlineKeyboardButton(
            text=("📜 Перевести в прошедшие" if w["is_active"] else "🟢 Вернуть в активные"),
            callback_data=f"adm_wb_toggle_{webinar_id}",
        )],
        [InlineKeyboardButton(
            text=("🔴 Выключить вопросы" if w["allow_questions"] else "❓ Разрешить вопросы"),
            callback_data=f"adm_wbq_toggle_{webinar_id}",
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_wb_delete_{webinar_id}")],
        [InlineKeyboardButton(text="⬅️ К списку вебинаров", callback_data="adm_webinars")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    return True


@router.callback_query(F.data == "adm_webinars")
async def adm_webinars(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_webinars"):
        return
    await _render_webinars_list(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wb_edit_"))
async def adm_wb_edit(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data.split("_")[-1])
    if await _render_webinar_card(callback, webinar_id):
        await callback.answer()


@router.callback_query(F.data.startswith("adm_wb_toggle_"))
async def adm_wb_toggle(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data.split("_")[-1])
    db.toggle_webinar_active(webinar_id)
    if await _render_webinar_card(callback, webinar_id):
        await callback.answer("Обновлено")


@router.callback_query(F.data.startswith("adm_wbq_toggle_"))
async def adm_wb_toggle_questions(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data.split("_")[-1])
    db.toggle_webinar_questions(webinar_id)
    if await _render_webinar_card(callback, webinar_id):
        await callback.answer("Обновлено")


@router.callback_query(F.data.startswith("adm_wb_delete_"))
async def adm_wb_delete(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data.split("_")[-1])
    db.delete_webinar(webinar_id)
    await _render_webinars_list(callback)
    await callback.answer("Вебинар удалён")


def _wb_add_back_kb(target_step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_wb_add_back_{target_step}")
    ]])


async def _wb_add_ask_title(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.title)
    await answer("Введите название вебинара (или /cancel для отмены):")


async def _wb_add_ask_type(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.event_type)
    kb_rows = _event_type_kb("wb_addtype_").inline_keyboard + [
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_wb_add_back_title")]
    ]
    await answer("Это вебинар, практика или расстановка?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


async def _wb_add_ask_description(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.description)
    await answer("Введите описание:", reply_markup=_wb_add_back_kb("event_type"))


async def _wb_add_ask_date(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.date_text)
    await answer(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ (например: 15.09.2026 18:00):",
        reply_markup=_wb_add_back_kb("description"),
    )


async def _wb_add_ask_price(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.price)
    await answer("Введите стоимость (например: 500 грн):", reply_markup=_wb_add_back_kb("date_text"))


async def _wb_add_ask_invite_link(answer, state: FSMContext):
    await state.set_state(WebinarAddStates.invite_link)
    await answer(
        "Введите ссылку на вебинар (Zoom / Google Meet и т.п.) - она будет отправлена "
        "участнику после подтверждения оплаты:",
        reply_markup=_wb_add_back_kb("price"),
    )


@router.callback_query(F.data == "adm_wb_add")
async def adm_wb_add_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await _wb_add_ask_title(callback.message.answer, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wb_add_back_"))
async def adm_wb_add_back(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    target_step = callback.data[len("adm_wb_add_back_"):]
    if target_step == "title":
        await _wb_add_ask_title(callback.message.answer, state)
    elif target_step == "event_type":
        await _wb_add_ask_type(callback.message.answer, state)
    elif target_step == "description":
        await _wb_add_ask_description(callback.message.answer, state)
    elif target_step == "date_text":
        await _wb_add_ask_date(callback.message.answer, state)
    elif target_step == "price":
        await _wb_add_ask_price(callback.message.answer, state)
    await callback.answer()


@router.message(WebinarAddStates.title)
async def adm_wb_add_title(message: Message, state: FSMContext):
    text = await _require_html_text(message)
    if text is None:
        return
    await state.update_data(title=text)
    await _wb_add_ask_type(message.answer, state)


@router.callback_query(WebinarAddStates.event_type, F.data.startswith("wb_addtype_"))
async def adm_wb_add_type(callback: CallbackQuery, state: FSMContext):
    event_type = callback.data[len("wb_addtype_"):]
    await state.update_data(event_type=event_type)
    await _wb_add_ask_description(callback.message.answer, state)
    await callback.answer()


@router.message(WebinarAddStates.description)
async def adm_wb_add_description(message: Message, state: FSMContext):
    text = await _require_html_text(message)
    if text is None:
        return
    await state.update_data(description=text)
    await _wb_add_ask_date(message.answer, state)


@router.message(WebinarAddStates.date_text)
async def adm_wb_add_date(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    try:
        dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "Не получилось распознать дату и время.\n"
            "Пришлите в формате ДД.ММ.ГГГГ ЧЧ:ММ (например: 15.09.2026 18:00):"
        )
        return
    await state.update_data(event_dt=dt.isoformat(), date_text=_format_event_dt(dt))
    await _wb_add_ask_price(message.answer, state)


@router.message(WebinarAddStates.price)
async def adm_wb_add_price(message: Message, state: FSMContext):
    text = await _require_html_text(message)
    if text is None:
        return
    await state.update_data(price=text)
    await _wb_add_ask_invite_link(message.answer, state)


@router.message(WebinarAddStates.invite_link)
async def adm_wb_add_invite(message: Message, state: FSMContext):
    text = await _require_html_text(message)
    if text is None:
        return
    data = await state.get_data()
    db.add_webinar(
        data["title"], data["description"], data["date_text"], data["price"], text,
        event_type=data.get("event_type", "webinar"), event_dt=data.get("event_dt"),
    )
    await state.clear()
    await message.answer(f"Вебинар «{data['title']}» добавлен и уже виден в меню ✅")


@router.callback_query(F.data.startswith("adm_wbf_"))
async def adm_wb_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split("_")
    code, webinar_id = parts[-2], int(parts[-1])
    field = CODE_TO_FIELD[code]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="webinar", target_id=webinar_id, field=field)
    w = db.get_webinar(webinar_id)
    current = (w[field] if w else "") or ""
    current_block = f"Сейчас:\n{current}\n\n" if current else ""
    if field == "date_text":
        await callback.message.answer(
            f"{current_block}Введите новую дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ (например: 15.09.2026 18:00):"
        )
    else:
        await callback.message.answer(f"{current_block}Введите новое значение для «{FIELD_LABELS[field]}»:")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wbtype_"))
async def adm_wb_type_start(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data[len("adm_wbtype_"):])
    await callback.message.answer(
        "Это вебинар, практика или расстановка?",
        reply_markup=_event_type_kb(f"adm_wbtyset_{webinar_id}_"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wbtyset_"))
async def adm_wb_type_set(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id_str, event_type = callback.data[len("adm_wbtyset_"):].split("_", 1)
    db.update_webinar_type(int(webinar_id_str), event_type)
    await callback.message.answer(f"Тип обновлён: {EVENT_TYPE_LABELS[event_type]} ✅")
    await callback.answer()


# ---------- админ-панель: VEDA SANCTUM ----------

@router.callback_query(F.data == "adm_sanctum")
async def adm_sanctum(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_sanctum"):
        return
    s = db.get_sanctum()

    def _preview(value):
        value = value or ""
        return html.escape(value[:120] + ("…" if len(value) > 120 else ""))

    photo_status = "установлено" if s["intro_photo"] else "не установлено"
    text = (
        f"<b>⚜️ {html.escape(SANCTUM_FULL_NAME)} - настройки</b>\n\n"
        f"Фото перед сообщением 1: {photo_status}\n\n"
        f"Сообщение 1 (приглашение):\n{_preview(s['intro_text'])}\n\n"
        f"Сообщение 2 (законы):\n{_preview(s['laws_text'])}\n\n"
        f"💳 Цена: {s['price']}\n"
        f"🔗 Ссылка на канал: {s['invite_link'] or '-'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Фото перед сообщением 1", callback_data="adm_photo_sanctum_intro")],
        [InlineKeyboardButton(text="✏️ Сообщение 1 (приглашение)", callback_data="adm_sf_intro_text")],
        [InlineKeyboardButton(text="✏️ Сообщение 2 (законы)", callback_data="adm_sf_laws_text")],
        [InlineKeyboardButton(text="✏️ Цена", callback_data="adm_sf_price"),
         InlineKeyboardButton(text="✏️ Ссылка на канал", callback_data="adm_sf_invite_link")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_sf_"))
async def adm_sanctum_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_sf_"):]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="sanctum", field=field)
    s = db.get_sanctum()
    current = (s[field] if s else "") or ""
    current_block = f"Сейчас:\n{current}\n\n" if current else ""
    hint = ""
    if field in ("intro_text", "laws_text"):
        hint = (
            "\n\n⚠️ В этом тексте поддерживается жирный шрифт через теги "
            "<code>&lt;b&gt;текст&lt;/b&gt;</code>. Если не уверены, как их писать - "
            "просто пришлите текст обычным сообщением, а жирные места укажите мне отдельно - "
            "разметку я расставлю."
        )
    await callback.message.answer(f"{current_block}Введите новое значение для «{FIELD_LABELS[field]}»:{hint}")
    await callback.answer()


def _grant_back_kb(target_step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_grant_back_{target_step}")
    ]])


GRANT_LIST_LIMIT = 40  # больше кнопок в одном сообщении неудобно листать - остальных можно по ID


def _member_short_name(m) -> str:
    return f"@{m['username']}" if m["username"] else (m["preferred_name"] or m["first_name"] or str(m["user_id"]))


def _grant_pick_kb() -> InlineKeyboardMarkup:
    """Список тех, кто уже есть в Sanctum (с датой и ценой прямо на кнопке) -
    чтобы выбрать человека нажатием, а не вводить ID вручную и сразу видеть, у
    кого какая цена."""
    today = _today()
    rows = []
    for m in db.get_all_sanctum_memberships()[:GRANT_LIST_LIMIT]:
        valid_until = _parse_date(m["valid_until"]) if m["valid_until"] else None
        if m["status"] == "removed":
            mark = "🚫"
        elif valid_until and valid_until >= today:
            mark = "✅"
        else:
            mark = "❌"
        date_text = valid_until.strftime("%d.%m.%Y") if valid_until else "-"
        price = _strip_html_tags(m["price"] or "-")
        label = f"{mark} {_member_short_name(m)} - до {date_text} - {price}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm_grant_pick_{m['user_id']}")])
    rows.append([InlineKeyboardButton(text="➕ Другой подписчик бота (ещё не в Sanctum)", callback_data="adm_grant_others")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _grant_ask_user_id(answer, state: FSMContext):
    await state.set_state(GrantAccessStates.waiting_user_id)
    await answer(
        f"Кому выдать/продлить доступ в {html.escape(SANCTUM_FULL_NAME)}? Нажмите на человека в списке "
        "(✅ доступ активен, ❌ закончился, 🚫 убран; рядом - до какого числа оплачено и по какой цене). "
        "Если человека нет в списке - нажмите «Другой подписчик» или пришлите его Telegram ID "
        "(или /cancel для отмены).\n\n"
        "Важно: этот человек должен был хотя бы раз нажать /start в этом боте - иначе бот не сможет ему написать.",
        reply_markup=_grant_pick_kb(),
    )


async def _grant_after_target(answer, state: FSMContext, target_user_id: int):
    """Человек выбран (кнопкой или по ID): показываем, что о нём известно
    сейчас, и идём дальше к дате."""
    await state.update_data(target_user_id=target_user_id)
    u = db.get_user(target_user_id)
    name = html.escape(_user_display_name(u)) if u else "(карточки в боте нет)"
    membership = db.get_sanctum_membership(target_user_id)
    if membership and membership["valid_until"]:
        vu = _parse_date(membership["valid_until"])
        status = " (убран)" if membership["status"] == "removed" else ""
        now_line = (
            f"Сейчас: доступ до {vu.strftime('%d.%m.%Y') if vu else '-'}{status}, "
            f"цена {html.escape(_strip_html_tags(membership['price'] or '-'))}."
        )
    else:
        now_line = "Сейчас в VEDA SANCTUM его нет."
    await answer(f"Выбран: <b>{name}</b> (ID {target_user_id}).\n{now_line}")
    await _grant_ask_valid_until(answer, state)


async def _grant_ask_valid_until(answer, state: FSMContext):
    await state.set_state(GrantAccessStates.waiting_valid_until)
    await answer(
        "До какой даты действует доступ?\n\n"
        "Пришлите дату в формате ДД.ММ.ГГГГ (например: 31.08.2026) - удобно для переноса тех, "
        "кто уже платит Вам за Санктум и знает свою дату окончания.\n\n"
        "Или отправьте «-», чтобы посчитать автоматически по обычным правилам "
        "(до конца текущего месяца, либо до конца следующего, если у человека уже есть активный период).",
        reply_markup=_grant_back_kb("user_id"),
    )


async def _grant_ask_price(answer, state: FSMContext):
    current_base_price = db.get_sanctum()["price"]
    await state.set_state(GrantAccessStates.waiting_price)
    target = (await state.get_data()).get("target_user_id")
    membership = db.get_sanctum_membership(target) if target else None
    locked = (
        f"Сейчас за ним закреплена: {html.escape(_strip_html_tags(membership['price']))}.\n\n"
        if membership and membership["price"] else ""
    )
    await answer(
        f"Какая цена закреплена за этим человеком?\n\n{locked}"
        f"Пришлите сумму (например: 2222 грн) - для действующих подписчиков со старой ценой это важно, "
        f"иначе при продлении подставится текущая базовая цена ({current_base_price}).\n\n"
        "Или отправьте «-», чтобы взять цену автоматически (его текущую закреплённую, если она уже есть, "
        "иначе - текущую базовую).",
        reply_markup=_grant_back_kb("valid_until"),
    )


async def _grant_ask_accumulated_months(answer, state: FSMContext):
    await state.set_state(GrantAccessStates.waiting_accumulated_months)
    await answer(
        "Нужно ли зачесть время в поле, накопленное ДО этого бота (например, человек уже давно платит Вам за "
        "Sanctum в обход бота)?\n\n"
        "Если да - пришлите, сколько месяцев уже накоплено (можно дробное число, например 6.5) - это "
        "заменит накопленное время в поле целиком на указанное.\n\n"
        "Если пересчитывать ничего не нужно (обычное продление) - пришлите «-».",
        reply_markup=_grant_back_kb("price"),
    )


@router.callback_query(F.data == "adm_grant_access")
async def adm_grant_access_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_grant_access"):
        return
    await _grant_ask_user_id(callback.message.answer, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_grant_back_"))
async def adm_grant_access_back(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    target_step = callback.data[len("adm_grant_back_"):]
    if target_step == "user_id":
        await _grant_ask_user_id(callback.message.answer, state)
    elif target_step == "valid_until":
        await _grant_ask_valid_until(callback.message.answer, state)
    elif target_step == "price":
        await _grant_ask_price(callback.message.answer, state)
    await callback.answer()


@router.message(GrantAccessStates.waiting_user_id)
async def adm_grant_access_user_id(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    try:
        target_user_id = int(text.strip())
    except ValueError:
        await message.answer("ID должен быть числом. Попробуйте ещё раз или отправьте /cancel")
        return
    await _grant_after_target(message.answer, state, target_user_id)


@router.callback_query(GrantAccessStates.waiting_user_id, F.data.startswith("adm_grant_pick_"))
async def adm_grant_pick(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_grant_access"):
        return
    await _grant_after_target(callback.message.answer, state, int(callback.data[len("adm_grant_pick_"):]))
    await callback.answer()


@router.callback_query(GrantAccessStates.waiting_user_id, F.data == "adm_grant_others")
async def adm_grant_others(callback: CallbackQuery, state: FSMContext):
    """Подписчики бота, которых ещё нет в Sanctum (например, давние участники,
    которых только предстоит внести)."""
    if not await _require_permission(callback, "adm_grant_access"):
        return
    in_sanctum = {m["user_id"] for m in db.get_all_sanctum_memberships()}
    others = [u for u in db.get_all_users_full() if u["user_id"] not in in_sanctum]
    rows = [
        [InlineKeyboardButton(text=f"{_user_display_name(u)} - ID {u['user_id']}"[:64],
                              callback_data=f"adm_grant_pick_{u['user_id']}")]
        for u in others[:GRANT_LIST_LIMIT]
    ]
    if not rows:
        await callback.answer("Все подписчики бота уже есть в списке Sanctum", show_alert=True)
        return
    await callback.message.answer(
        "Подписчики бота, которых ещё нет в Sanctum. Нажмите на нужного или пришлите его ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.message(GrantAccessStates.waiting_valid_until)
async def adm_grant_access_valid_until(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    raw = text.strip()

    if raw == "-":
        await state.update_data(explicit_valid_until=None)
    else:
        try:
            valid_until = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            await message.answer(
                "Не получилось распознать дату. Пришлите в формате ДД.ММ.ГГГГ (например: 31.08.2026) или «-»."
            )
            return
        await state.update_data(explicit_valid_until=valid_until.isoformat())

    await _grant_ask_price(message.answer, state)


@router.message(GrantAccessStates.waiting_price)
async def adm_grant_access_price(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    raw_price = text.strip()
    await state.update_data(explicit_price=None if raw_price == "-" else raw_price)
    await _grant_ask_accumulated_months(message.answer, state)


@router.message(GrantAccessStates.waiting_accumulated_months)
async def adm_grant_access_finish(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    raw_months = text.strip()
    accumulated_months = None
    if raw_months != "-":
        try:
            accumulated_months = float(raw_months.replace(",", "."))
            if accumulated_months < 0:
                raise ValueError
        except ValueError:
            await message.answer(
                "Не получилось распознать число месяцев. Пришлите число (например: 6 или 6.5) или «-»."
            )
            return

    data = await state.get_data()
    target_user_id = data["target_user_id"]
    explicit_valid_until = data.get("explicit_valid_until")
    explicit_price = data.get("explicit_price")

    old_level = compute_ascension_level(target_user_id)
    if explicit_valid_until is None:
        valid_until, locked_price = _extend_sanctum_membership(target_user_id, price=explicit_price)
    else:
        valid_until = datetime.strptime(explicit_valid_until, "%Y-%m-%d").date()
        locked_price = _resolve_price(target_user_id, explicit_price)
        db.upsert_sanctum_membership(target_user_id, explicit_valid_until, locked_price)
    if accumulated_months is not None:
        db.set_accumulated_days(target_user_id, round(accumulated_months * 30))
    new_level = compute_ascension_level(target_user_id)

    await state.clear()

    s = db.get_sanctum()
    invite_link = s["invite_link"] if s else ""
    user_text = (
        f"✨ Вам открыт доступ в {html.escape(SANCTUM_FULL_NAME)} до {valid_until.strftime('%d.%m.%Y')} "
        f"по цене {locked_price}."
    )
    if invite_link:
        user_text += f"\n\nВаша ссылка:\n{invite_link}"

    try:
        await message.bot.send_message(target_user_id, user_text)
        await message.answer(
            f"Готово ✅ Доступ выдан до {valid_until.strftime('%d.%m.%Y')} по цене {locked_price}, человек уведомлён."
        )
    except Exception:
        await message.answer(
            f"Доступ в базе выдан до {valid_until.strftime('%d.%m.%Y')} по цене {locked_price}, но уведомить не "
            "получилось - скорее всего, этот человек ещё не нажимал /start в боте. Попросите его сделать это, "
            "ссылку на канал придётся прислать ему самостоятельно."
        )
    await _handle_ascension_transition(message.bot, target_user_id, old_level, new_level)


@router.callback_query(F.data == "adm_sanctum_list")
async def adm_sanctum_list(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_sanctum_list"):
        return
    today = _today()
    reminder_from = today + timedelta(days=config.SANCTUM_REMINDER_DAYS_BEFORE)
    current_base_price = db.get_sanctum()["price"]
    members = db.get_all_sanctum_memberships()

    if not members:
        await callback.message.answer("Пока нет ни одного подписчика VEDA SANCTUM.")
        await callback.answer()
        return

    lines = [f"<b>📋 Подписчики {html.escape(SANCTUM_FULL_NAME)}</b>\n"]
    for m in members:
        valid_until = _parse_date(m["valid_until"])
        name = f"@{m['username']}" if m["username"] else (m["preferred_name"] or m["first_name"] or str(m["user_id"]))

        if m["status"] == "removed":
            status = "🚫 удалён"
        elif valid_until is None:
            status = "❔"
        elif valid_until < today:
            status = "❌ просрочено"
        elif valid_until <= reminder_from:
            status = "⚠️ скоро истекает"
        else:
            status = "✅ активна"

        date_text = valid_until.strftime("%d.%m.%Y") if valid_until else "-"
        price = m["price"] or "-"
        # сравниваем без учёта форматирования (жирный/ссылки) — иначе одна и та же
        # цена, оформленная по-разному, ошибочно считалась бы "разной"
        rate_tag = "🆕 новая цена" if _strip_html_tags(price) == _strip_html_tags(current_base_price) else "🕰 старая цена"
        full = db.get_sanctum_membership(m["user_id"])
        acc_days = (full["accumulated_days"] or 0) if full else 0
        level = compute_ascension_level(m["user_id"])
        id_link = f'<a href="tg://user?id={m["user_id"]}">ID {m["user_id"]}</a>'
        line = (
            f"{status} - {html.escape(name)} ({id_link}) - до {date_text} - {price} ({rate_tag})\n"
            f"   🪜 {ASCENSION_LEVEL_NAMES[level]} - в поле по оплатам: {acc_days} дн. (~{acc_days / 30:.1f} мес.)"
        )
        if m["promise_date"]:
            promise = _parse_date(m["promise_date"])
            if promise:
                line += f"\n   ⏰ обещал оплатить: {promise.strftime('%d.%m.%Y')}"
        lines.append(line)

    await callback.message.answer("\n".join(lines))

    active_members = [m for m in members if m["status"] != "removed"]
    if active_members:
        rows = []
        for m in active_members:
            name = f"@{m['username']}" if m["username"] else (m["preferred_name"] or m["first_name"] or str(m["user_id"]))
            rows.append([InlineKeyboardButton(text=f"🚫 Убрать {name}", callback_data=f"adm_sanctum_kick_{m['user_id']}")])
        await callback.message.answer(
            "Нажмите, чтобы убрать человека из VEDA SANCTUM:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_sanctum_kick_"))
async def adm_sanctum_kick(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    user_id = int(callback.data.split("_")[-1])
    db.set_sanctum_status(user_id, "removed")

    # человеку сразу уходит письмо с правилами возврата: сколько сохраняется его
    # цена и что дальше (её решение 2026-09-20) - от ДАТЫ УБИРАНИЯ считаются и
    # срок сохранения цены, и срок обнуления времени в поле
    membership = db.get_sanctum_membership(user_id)
    user_row = db.get_user(user_id)
    delivered = False
    if membership and user_row:
        removed_on = _lapse_anchor(membership) or _today()
        text = _lapse_text(
            db.get_setting("sanctum_removed_text"), user_row, removed_on,
            removed_on + timedelta(days=_int_setting("price_lock_days", 30)),
            membership["price"] or db.get_sanctum()["price"],
        )
        try:
            await callback.bot.send_message(user_id, text, reply_markup=_return_kb())
            delivered = True
        except Exception:
            logging.exception("Не удалось отправить письмо об уходе из Sanctum пользователю %s", user_id)
    note = "письмо с правилами возврата отправлено" if delivered else "письмо отправить не получилось (нет карточки или человек закрыл бота)"
    await callback.message.edit_text(f"🚫 Убран из VEDA SANCTUM ✅\n{note}")
    await callback.answer()


REMINDER_TEXT_LABELS = {
    "sanctum_reminder_text_early": "текст напоминания заранее (за 2 дня до нового месяца)",
    "sanctum_reminder_text_due": "текст напоминания в день истечения",
    "sanctum_promise_reminder_text": "текст напоминания об обещанной дате оплаты",
}

# (target, field) — поля, которые уходят пользователю без html.escape, поэтому
# при вводе сохраняем HTML-разметку (жирный текст и т.п.), а не голый текст.
HTML_TRUSTED_FIELDS = {
    ("webinar", "title"),
    ("webinar", "description"),
    ("webinar", "price"),
    ("webinar", "invite_link"),
    ("sanctum", "intro_text"),
    ("sanctum", "laws_text"),
    ("sanctum", "price"),
    ("sanctum", "invite_link"),
    ("about", "about_text"),
    ("faq", "faq_text"),
    ("faq_suggestion_invite", "faq_suggestion_invite_text"),
    ("faq_suggestion_confirm", "faq_suggestion_confirm_text"),
    ("rules", "rules_text"),
    ("welcome_text", "welcome_text"),
    ("meditation_text", "meditation_text"),
    ("meditation_coming_soon", "meditation_coming_soon_text"),
    ("reminder_text", "sanctum_reminder_text_early"),
    ("reminder_text", "sanctum_reminder_text_due"),
    ("reminder_text", "sanctum_promise_reminder_text"),
    ("payment", "payment_requisites"),
    ("payment", "payment_purpose_webinar"),
    ("payment", "payment_purpose_sanctum"),
    ("reengage", "stall_text"),
    ("reengage", "reengage_text"),
    ("reengage", "winback_text"),
    ("reengage", "sanctum_removed_text"),
    ("reengage", "sanctum_lastchance_text"),
    ("reengage", "sanctum_reset_text"),
    ("reengage", "sanctum_reset_luminar_line"),
    ("reengage", "sanctum_reset_meditation_line"),
    ("webinar_reminder_text", "webinar_reminder_5d_text"),
    ("webinar_reminder_text", "webinar_reminder_24h_text"),
    ("webinar_reminder_text", "webinar_reminder_1h_text"),
    ("ascension_text", "ascension_level1_text"),
    ("ascension_text", "ascension_level2_text"),
    ("ascension_text", "ascension_level1_brief_text"),
    ("ascension_text", "ascension_level2_brief_text"),
    ("ascension_text", "ascension_level3_brief_text"),
    ("ascension_text", "ascension_level3_text"),
    ("ascension_text", "ascension_overview_text"),
    ("ascension_text", "luminar_intro_short_text"),
    ("profile_text", "profile_greeting_text"),
    ("profile_text", "profile_meditation_reminder_text"),
    ("profile_text", "profile_join_date_text"),
    ("profile_text", "profile_sanctum_header_text"),
    ("profile_text", "profile_sanctum_active_text"),
    ("profile_text", "profile_sanctum_expired_text"),
    ("profile_text", "profile_sanctum_promise_text"),
    ("profile_text", "profile_sanctum_never_joined_text"),
    ("profile_text", "profile_webinars_header_text"),
    ("profile_text", "profile_webinars_empty_text"),
    ("profile_text", "profile_path_step_label_text"),
    ("profile_text", "profile_luminar_rank_label_text"),
    ("profile_text", "profile_referral_intro_text"),
    ("profile_text", "profile_luminar_progress_zero_text"),
    ("profile_text", "profile_luminar_progress_text"),
    ("profile_text", "profile_luminar_progress_next_text"),
    # кнопки (profile_btn_*) НЕ добавляем сюда нарочно — Telegram не показывает
    # HTML-разметку в тексте кнопки, поэтому для них нужен обычный текст без
    # форматирования (см. edit_field_value: html_trusted решается через
    # членство именно в этом множестве)
    ("ascension_text", "ascension_intention_invite_text"),
    ("ascension_text", "ascension_intention_confirmation_text"),
    ("ascension_text", "referral_welcome_text"),
    ("ascension_text", "luminar_referral_ping_text_1"),
    ("ascension_text", "luminar_referral_ping_text_2"),
    ("ascension_text", "luminar_referral_ping_text_3"),
    ("ascension_text", "ascension_intention_recall_text"),
    ("ascension_text", "luminar_intro_text"),
    ("ascension_text", "luminar_1_text"),
    ("ascension_text", "luminar_2_text"),
    ("ascension_text", "luminar_3_text"),
}


@router.callback_query(F.data == "adm_reminder_texts")
async def adm_reminder_texts(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_reminder_texts"):
        return
    early = db.get_setting("sanctum_reminder_text_early")
    due = db.get_setting("sanctum_reminder_text_due")
    promise = db.get_setting("sanctum_promise_reminder_text")
    text = (
        "<b>✉️ Тексты напоминаний VEDA SANCTUM</b>\n\n"
        f"Заранее (за 2 дня до нового месяца):\n{html.escape(early)}\n\n"
        f"В день истечения:\n{html.escape(due)}\n\n"
        f"Об обещанной дате оплаты (за день до неё):\n{html.escape(promise)}\n\n"
        "Подсказка: <code>{date}</code> - дата, <code>{price}</code> - цена ИМЕННО этого человека "
        "(у каждого может быть своя) - бот сам подставит нужные значения."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст «заранее»", callback_data="adm_rt_sanctum_reminder_text_early")],
        [InlineKeyboardButton(text="✏️ Текст «в день истечения»", callback_data="adm_rt_sanctum_reminder_text_due")],
        [InlineKeyboardButton(text="✏️ Текст «обещание оплаты»", callback_data="adm_rt_sanctum_promise_reminder_text")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rt_"))
async def adm_reminder_text_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_rt_"):]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="reminder_text", field=field)
    current = db.get_setting(field) or ""
    await callback.message.answer(
        f"Сейчас:\n{current}\n\n"
        f"Пришлите новый {REMINDER_TEXT_LABELS[field]}.\n\n"
        "Можно вставить <code>{date}</code> (дата окончания) и/или <code>{price}</code> "
        "(цена именно этого человека) в нужных местах текста."
    )
    await callback.answer()


# ---------- админ-панель: тексты напоминаний о вебинарах ----------

WEBINAR_REMINDER_TEXT_LABELS = {
    "webinar_reminder_5d_text": "текст напоминания за 5 дней",
    "webinar_reminder_24h_text": "текст напоминания за 24 часа",
    "webinar_reminder_1h_text": "текст напоминания за 1 час",
}


@router.callback_query(F.data == "adm_webinar_reminder_texts")
async def adm_webinar_reminder_texts(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_webinar_reminder_texts"):
        return
    d5 = db.get_setting("webinar_reminder_5d_text")
    d24 = db.get_setting("webinar_reminder_24h_text")
    d1 = db.get_setting("webinar_reminder_1h_text")
    text = (
        "<b>✉️ Тексты напоминаний о вебинарах</b>\n\n"
        "Отправляются автоматически всем, кто уже подтверждённо оплатил конкретный "
        "вебинар/практику/расстановку - за 5 дней и за 24 часа до начала (с кнопкой "
        "«Подробнее о вебинаре», её удобно пересылать) и за 1 час до начала (только "
        "практическая информация, без кнопки).\n\n"
        f"За 5 дней:\n{html.escape(d5)}\n\n"
        f"За 24 часа:\n{html.escape(d24)}\n\n"
        f"За 1 час:\n{html.escape(d1)}\n\n"
        "Подсказка: <code>{имя}</code>, <code>{тип}</code> (вебинар/практика/расстановка), "
        "<code>{название}</code>, <code>{дата и время}</code> - бот сам подставит нужные "
        "значения. В тексте «за 1 час» ещё доступен <code>{ссылка}</code> - подставится "
        "ссылка на подключение, если она заполнена у этого вебинара, иначе просто исчезнет."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст «за 5 дней»", callback_data="adm_wrt_webinar_reminder_5d_text")],
        [InlineKeyboardButton(text="✏️ Текст «за 24 часа»", callback_data="adm_wrt_webinar_reminder_24h_text")],
        [InlineKeyboardButton(text="✏️ Текст «за 1 час»", callback_data="adm_wrt_webinar_reminder_1h_text")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wrt_"))
async def adm_webinar_reminder_text_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_wrt_"):]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="webinar_reminder_text", field=field)
    current = db.get_setting(field) or ""
    hint = "<code>{имя}</code>, <code>{тип}</code>, <code>{название}</code>"
    if field != "webinar_reminder_1h_text":
        hint += ", <code>{дата и время}</code>"
    else:
        hint += ", <code>{ссылка}</code>"
    await callback.message.answer(
        f"Сейчас:\n{current}\n\nПришлите новый {WEBINAR_REMINDER_TEXT_LABELS[field]}.\n\nМожно вставить {hint}."
    )
    await callback.answer()


# ---------- админ-панель: тексты Пути Восхождения и Люминаров ----------

ASCENSION_TEXT_LABELS = {
    "ascension_level1_brief_text": "краткая сводка «Первое Касание» (видна в профиле)",
    "ascension_level1_text": "полное послание «Первое Касание» (кнопка «Читать послание» + сообщение сразу после /start)",
    "ascension_level2_brief_text": "краткая сводка «Искра» (видна в профиле)",
    "ascension_level2_text": "полное послание «Искра» (кнопка «Читать послание» + сообщение при переходе)",
    "ascension_level3_brief_text": "краткая сводка «Исследователь Глубины» (видна в профиле)",
    "ascension_level3_text": "полное послание «Исследователь Глубины» (кнопка «Читать послание» + сообщение при переходе)",
    "ascension_overview_text": "«Как устроен Путь?» - справка (кнопка ℹ️ в профиле)",
    "ascension_intention_invite_text": "приглашение написать намерение (сразу после «Искры»)",
    "ascension_intention_confirmation_text": "ответ сразу после того, как человек написал намерение",
    "referral_welcome_text": "особое приветствие для пришедших по реферальной ссылке (перед вопросом об имени)",
    "luminar_referral_ping_text_1": "мгновенное уведомление о реферале - вариант 1 (случайно выбирается один из трёх)",
    "luminar_referral_ping_text_2": "мгновенное уведомление о реферале - вариант 2",
    "luminar_referral_ping_text_3": "мгновенное уведомление о реферале - вариант 3",
    "ascension_intention_recall_text": "ежемесячное напоминание о намерении",
    "luminar_intro_short_text": "«Созвездие Люминаров» - краткая строка (видна в профиле всегда)",
    "luminar_intro_text": "«Созвездие Люминаров» - полный текст (кнопка «Подробнее» в профиле)",
    "luminar_1_text": "поздравление с Люминар I (5 приглашённых)",
    "luminar_2_text": "поздравление с Люминар II (10 приглашённых)",
    "luminar_3_text": "поздравление с Люминар III (30 приглашённых)",
}

ASCENSION_TEXT_PLACEHOLDERS = {
    "ascension_level1_text": ["{имя}"],
    "ascension_level2_text": ["{имя}"],
    "ascension_level3_text": ["{имя}"],
    "ascension_overview_text": ["{имя}"],
    "ascension_intention_confirmation_text": ["{имя}"],
    "referral_welcome_text": ["{пригласивший}", "{название}"],
    "ascension_intention_recall_text": ["{намерение}"],
    "luminar_1_text": ["{имя}"],
    "luminar_2_text": ["{имя}"],
    "luminar_3_text": ["{имя}"],
}


@router.callback_query(F.data == "adm_ascension_texts")
async def adm_ascension_texts(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_ascension_texts"):
        return
    text = (
        "<b>✉️ Тексты Пути Восхождения и Люминаров</b>\n\n"
        "Тексты ступеней 1-3 показываются в «✨ Мой профиль», а для 2-й и 3-й ступени "
        "ещё и приходят отдельным сообщением ровно в момент перехода (сразу ПОСЛЕ "
        "подтверждения оплаты, не раньше). Приглашение, подтверждение и ежемесячное "
        "напоминание - три момента ритуала намерения на ступени «Искра», по порядку: "
        "приглашение уходит сразу после текста «Искра», подтверждение - сразу как "
        "человек напишет своё намерение в ответ (и в нём же - напоминание о личном "
        "разборе в дар), напоминание - раз в месяц после этого. "
        "Поздравления Люминаров уходят при достижении каждого ранга. Справка «Как устроен Путь?» "
        "открывается по отдельной кнопке ℹ️ в профиле, по желанию человека.\n\n"
        "Нажмите на нужный текст ниже, чтобы отредактировать (можно форматировать - "
        "жирный, курсив, ссылки - прямо при вводе в Telegram).\n\n"
        "Подсказки по местам вставки:\n"
        "• Ступени 1-3, подтверждение намерения: <code>{имя}</code>\n"
        "• Напоминание о намерении: <code>{намерение}</code>\n"
        "• Поздравления Люминаров: <code>{имя}</code>, <code>{число}</code>, <code>{дар}</code>"
    )
    rows = [
        [InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"adm_asc_{key}")]
        for key, label in ASCENSION_TEXT_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_asc_"))
async def adm_ascension_text_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_asc_"):]
    current = db.get_setting(field) or ""
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="ascension_text", field=field)
    prompt = f"Сейчас:\n{current}\n\nПришлите новый {ASCENSION_TEXT_LABELS[field]}:"
    placeholders = ASCENSION_TEXT_PLACEHOLDERS.get(field)
    if placeholders:
        ph_hint = ", ".join(f"<code>{html.escape(p)}</code>" for p in placeholders)
        prompt += f"\n\nВажно: именно фигурные скобки - {ph_hint} (не круглые), иначе не подставится."
    await callback.message.answer(prompt)
    await callback.answer()


# ---------- админ-панель: реквизиты оплаты ----------

PAYMENT_FIELD_LABELS = {
    "payment_requisites": "карта / получатель",
    "payment_purpose_webinar": "назначение платежа - вебинары",
    "payment_purpose_sanctum": f"назначение платежа - {SANCTUM_FULL_NAME}",
}


@router.callback_query(F.data == "adm_payment")
async def adm_payment(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_payment"):
        return
    card = db.get_setting("payment_requisites")
    purpose_w = db.get_setting("payment_purpose_webinar")
    purpose_s = db.get_setting("payment_purpose_sanctum")
    # card/purpose_w/purpose_s — доверенные HTML-поля, не экранируем (см. _payment_block).
    text = (
        "<b>💳 Реквизиты оплаты</b>\n\n"
        f"Карта / получатель:\n{card}\n\n"
        f"Назначение - вебинары:\n{purpose_w}\n\n"
        f"Назначение - {html.escape(SANCTUM_FULL_NAME)}:\n{purpose_s}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Карта / получатель", callback_data="adm_pf_payment_requisites")],
        [InlineKeyboardButton(text="✏️ Назначение - вебинары", callback_data="adm_pf_payment_purpose_webinar")],
        [InlineKeyboardButton(text="✏️ Назначение - VEDA SANCTUM", callback_data="adm_pf_payment_purpose_sanctum")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_pf_"))
async def adm_payment_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_pf_"):]
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="payment", field=field)
    current = db.get_setting(field)
    await callback.message.answer(
        f"Текущее значение:\n\n{current}\n\n"
        f"Пришлите новое значение для «{PAYMENT_FIELD_LABELS[field]}»:"
    )
    await callback.answer()


# ---------- админ-панель: текст «Обо мне» ----------

@router.callback_query(F.data == "adm_about")
async def adm_about(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_about"):
        return
    current = db.get_setting("about_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="about", field="about_text")
    await callback.message.answer(
        f"Текущий текст «Философия Alena Veda»:\n\n{current}\n\n"
        f"Пришлите новый текст (он будет показан пользователю по кнопке «{html.escape(BTN_ABOUT)}»):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_faq")
async def adm_faq(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_faq"):
        return
    current = db.get_setting("faq_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="faq", field="faq_text")
    await callback.message.answer(
        f"Текущий текст «Частые вопросы»:\n\n{current}\n\n"
        f"Пришлите новый текст (он показывается по кнопке «{html.escape(BTN_INFO)}» → «❓ Частые вопросы»):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_faq_suggestion_invite")
async def adm_faq_suggestion_invite(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_faq_suggestion_invite"):
        return
    current = db.get_setting("faq_suggestion_invite_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="faq_suggestion_invite", field="faq_suggestion_invite_text")
    await callback.message.answer(
        f"Текущий текст:\n\n{current}\n\n"
        "Пришлите новый текст (он показывается по кнопке «💡 Предложить свой вопрос»):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_faq_suggestion_confirm")
async def adm_faq_suggestion_confirm(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_faq_suggestion_confirm"):
        return
    current = db.get_setting("faq_suggestion_confirm_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="faq_suggestion_confirm", field="faq_suggestion_confirm_text")
    await callback.message.answer(
        f"Текущий текст:\n\n{current}\n\n"
        "Пришлите новый текст (он приходит сразу после того, как человек пришлёт свой вопрос):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_faq_suggestions")
async def adm_faq_suggestions(callback: CallbackQuery):
    """Список вопросов, которые люди сами предложили добавить в «Частые
    вопросы» (см. suggest_faq_question_received) - без пересылки лично в
    чат, специально по её просьбе: она сама заглядывает сюда, когда удобно,
    переносит нужное в текст FAQ (adm_faq) вручную, и отмечает «Учтено»."""
    if not await _require_permission(callback, "adm_faq_suggestions"):
        return
    rows_data = db.get_pending_faq_suggestions()
    if not rows_data:
        text = "<b>💡 Вопросы от людей для FAQ</b>\n\nПока никто ничего не предложил."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
        return

    lines = [
        "<b>💡 Вопросы от людей для FAQ</b>",
        "",
        "Перенесите нужное в текст «Частые вопросы» (✏️ Текст «Частые вопросы») вручную, "
        "затем отметьте здесь «Учтено», чтобы вопрос ушёл из списка.",
    ]
    rows = []
    for r in rows_data:
        name = f"@{r['username']}" if r["username"] else (r["preferred_name"] or r["first_name"] or str(r["user_id"]))
        date_part = (r["created_at"] or "").split(" ")[0]
        lines.append(f"\n<b>{html.escape(name)}</b> ({date_part}):\n«{html.escape(r['question_text'])}»")
        rows.append([InlineKeyboardButton(
            text=f"✅ Учтено - {name}", callback_data=f"adm_faq_sugg_done_{r['id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_faq_sugg_done_"))
async def adm_faq_suggestion_done(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_faq_suggestions"):
        return
    suggestion_id = int(callback.data[len("adm_faq_sugg_done_"):])
    db.mark_faq_suggestion_reviewed(suggestion_id)
    await adm_faq_suggestions(callback)


@router.callback_query(F.data == "adm_rules")
async def adm_rules(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_rules"):
        return
    current = db.get_setting("rules_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="rules", field="rules_text")
    await callback.message.answer(
        f"Текущий текст «Правила пространства»:\n\n{current}\n\n"
        f"Пришлите новый текст (он показывается по кнопке «{html.escape(BTN_INFO)}» → «📜 Правила пространства»):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_welcome_text")
async def adm_welcome_text(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_welcome_text"):
        return
    current = db.get_setting("welcome_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="welcome_text", field="welcome_text")
    await callback.message.answer(
        f"Текущий текст приветствия:\n\n{current}\n\n"
        "Пришлите новый текст (он появится сразу после /start, под фото приветствия, если оно есть):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_meditation_text")
async def adm_meditation_text(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_meditation_text"):
        return
    current = db.get_setting("meditation_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="meditation_text", field="meditation_text")
    await callback.message.answer(
        f"Текущий текст VEDA HEALING FLOW:\n\n{current}\n\n"
        "Пришлите новый текст (он показывается по кнопке 🧘🏽‍♀️ VEDA HEALING FLOW):"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_meditation_coming_soon")
async def adm_meditation_coming_soon(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_meditation_coming_soon"):
        return
    current = db.get_setting("meditation_coming_soon_text")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="meditation_coming_soon", field="meditation_coming_soon_text")
    await callback.message.answer(
        f"Текущий текст:\n\n{current}\n\n"
        "Пришлите новый текст - он показывается ТОЛЬКО пока ссылка на VEDA HEALING FLOW не задана "
        "(добавляется под основным текстом, там, где иначе была бы кнопка). Как только зададите "
        "ссылку - этот текст перестанет показываться сам."
    )
    await callback.answer()


@router.callback_query(F.data == "adm_meditation_link")
async def adm_meditation_link(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_meditation_link"):
        return
    current = db.get_setting("meditation_bot_link")
    status = current if current else "пока не задана - кнопка «Перейти в VEDA HEALING FLOW» нигде не показывается людям"
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="meditation_link", field="meditation_bot_link")
    await callback.message.answer(
        f"Сейчас: {status}\n\n"
        "Пришлите ссылку на бот с медитациями (например: https://t.me/ВашБот). "
        "Как только пришлёте - кнопка «Перейти в VEDA HEALING FLOW» сразу появится везде "
        "(в этом разделе, в профиле и в напоминаниях)."
    )
    await callback.answer()


@router.callback_query(F.data == "adm_personal_link")
async def adm_personal_link(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_personal_link"):
        return
    current = db.get_setting("admin_personal_chat_link")
    status = current if current else "пока не задана"
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="personal_link", field="admin_personal_chat_link")
    await callback.message.answer(
        f"Сейчас: {status}\n\n"
        "Пришлите ссылку на Ваш личный чат в Telegram (например: https://t.me/Alena_Devi_Veda или просто "
        "@Alena_Devi_Veda). Она используется в кнопке-ссылке на Ваш личный чат, которая приходит вместе с "
        "сообщением о переходе на ступень «Искра», после того как человек напишет намерение, в ежемесячном "
        "напоминании о намерении, во всех трёх поздравлениях с рангом Люминара и в сообщениях с реквизитами "
        "оплаты (вебинары и VEDA SANCTUM)."
    )
    await callback.answer()


PROFILE_TEXT_LABELS = {
    "profile_greeting_text": "приветствие в начале «Мой профиль»",
    "profile_join_date_text": "строка «С нами с ... уже N дней»",
    "profile_sanctum_header_text": "заголовок блока VEDA SANCTUM",
    "profile_sanctum_active_text": "строка «доступ активен до ...»",
    "profile_sanctum_expired_text": "строка «доступ закончился ...»",
    "profile_sanctum_promise_text": "строка «Вы планировали оплатить ...»",
    "profile_sanctum_never_joined_text": "строка для тех, кто ещё не в VEDA SANCTUM",
    "profile_webinars_header_text": "заголовок блока «Вебинары»",
    "profile_webinars_empty_text": "строка, если вебинаров ещё не было",
    "profile_meditation_reminder_text": "напоминание о VEDA HEALING FLOW в профиле",
    "profile_path_step_label_text": "строка «Ступень: ...»",
    "profile_luminar_rank_label_text": "строка ранга Люминара (видна только если ранг уже есть)",
    "profile_referral_intro_text": "строка перед реферальной ссылкой",
    "profile_btn_renew_text": "кнопка «Продлить» (когда доступ VEDA SANCTUM истёк)",
    "profile_btn_read_level_text": "кнопка «Читать послание ступени»",
    "profile_btn_luminar_intro_text": "кнопка «Подробнее о Созвездии Люминаров»",
    "profile_btn_path_overview_text": "кнопка «Как устроен Путь?»",
    "profile_luminar_progress_zero_text": "строка, если ещё никто не пришёл по ссылке (0 приглашённых)",
    "profile_luminar_progress_text": "строка «путь к первому ключу» (1-4 приглашённых)",
    "profile_luminar_progress_next_text": "строка «путь к следующему ключу» (после первого ранга)",
}

# поля с плейсхолдерами — та же защита от опечатки (круглые скобки вместо
# фигурных), что и у ASCENSION_TEXT_PLACEHOLDERS
PROFILE_TEXT_PLACEHOLDERS = {
    "profile_greeting_text": ["{имя}"],
    "profile_join_date_text": ["{дата}", "{дней}"],
    "profile_sanctum_header_text": ["{название}"],
    "profile_sanctum_active_text": ["{дата}"],
    "profile_sanctum_expired_text": ["{дата}"],
    "profile_sanctum_promise_text": ["{дата}"],
    "profile_path_step_label_text": ["{ступень}"],
    "profile_luminar_rank_label_text": ["{ранг}"],
    "profile_referral_intro_text": ["{ссылка}"],
    "profile_luminar_progress_text": ["{число}", "{человек}", "{осталось}"],
    "profile_luminar_progress_next_text": ["{следующий_ранг}", "{бар}", "{число}", "{порог}"],
}


@router.callback_query(F.data == "adm_profile_texts")
async def adm_profile_texts(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_profile_texts"):
        return
    text = (
        "<b>✨ Тексты «Мой профиль»</b>\n\n"
        "Здесь можно изменить абсолютно весь текст экрана «Мой профиль», включая заголовки блоков "
        "и подписи кнопок. Там, где текст зависит от реальных данных человека (дата, статус "
        "Санктума, ранг Люминара, ссылка) - используются метки в фигурных скобках, бот сам "
        "подставит нужное значение на их место; можно переставлять их куда угодно внутри текста.\n\n"
        "Нажмите на нужный текст ниже, чтобы увидеть его целиком и отредактировать."
    )
    rows = [
        [InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"adm_pt_{key}")]
        for key, label in PROFILE_TEXT_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_pt_"))
async def adm_profile_text_field_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_pt_"):]
    current = db.get_setting(field) or ""
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="profile_text", field=field)
    prompt = f"Сейчас:\n{current}\n\nПришлите новый {PROFILE_TEXT_LABELS[field]}:"
    placeholders = PROFILE_TEXT_PLACEHOLDERS.get(field)
    if placeholders:
        ph_hint = ", ".join(f"<code>{html.escape(p)}</code>" for p in placeholders)
        prompt += f"\n\nВажно: именно фигурные скобки - {ph_hint} (не круглые), иначе не подставится."
    await callback.message.answer(prompt)
    await callback.answer()


# ---------- админ-панель: подписчики бота ----------

def _user_display_name(u) -> str:
    return f"@{u['username']}" if u["username"] else (u["preferred_name"] or u["first_name"] or str(u["user_id"]))


def _users_list_kb(users) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        joined = (u["created_at"] or "").split(" ")[0]
        mark = "🚫" if u["blocked"] else ("👋" if u["self_departed"] else "✅")
        label = f"{mark} {_user_display_name(u)} - с {joined}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm_user_view_{u['user_id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_users_list")
async def adm_users_list(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_users_list"):
        return
    users = db.get_all_users_full()
    if not users:
        await callback.message.edit_text("Пока никто не запускал бота.")
        await callback.answer()
        return
    text = f"<b>👥 Подписчики бота ({len(users)})</b>\n\nНажмите на имя, чтобы посмотреть и управлять."
    await callback.message.edit_text(text, reply_markup=_users_list_kb(users))
    await callback.answer()


async def _render_user_detail(callback: CallbackQuery, user_id: int):
    u = db.get_user(user_id)
    if not u:
        await callback.message.edit_text(
            "Этого человека больше нет среди подписчиков бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm_users_list")]
            ]),
        )
        return
    joined = (u["created_at"] or "").split(" ")[0]
    if u["blocked"]:
        status = "🚫 заблокирован Вами"
    elif u["self_departed"]:
        status = "👋 сам заблокировал бота / вышел"
    else:
        status = "✅ активен"
    meditation_status = "✅ куплен" if u["bought_meditation_bot"] else "— пока не отмечен"
    text = (
        f"<b>{html.escape(_user_display_name(u))}</b>\n"
        f'ID: <a href="tg://user?id={u["user_id"]}">{u["user_id"]}</a>\n'
        f"С нами с: {joined}\n"
        f"Статус: {status}\n"
        f"VEDA HEALING FLOW: {meditation_status}"
    )
    block_label = "✅ Разблокировать" if u["blocked"] else "🚫 Заблокировать"
    meditation_label = "↩️ Снять отметку VEDA HEALING FLOW" if u["bought_meditation_bot"] else "🧘 Отметить покупку VEDA HEALING FLOW"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать", callback_data=f"admin_reply_{user_id}")],
        [InlineKeyboardButton(text=meditation_label, callback_data=f"adm_user_meditation_toggle_{user_id}")],
        [InlineKeyboardButton(text=block_label, callback_data=f"adm_user_toggle_{user_id}")],
        [InlineKeyboardButton(text="🔄 Знакомство заново (сбросить только имя)", callback_data=f"adm_user_reset_{user_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm_users_list")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm_user_view_"))
async def adm_user_view(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await _render_user_detail(callback, int(callback.data.split("_")[-1]))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_user_toggle_"))
async def adm_user_toggle(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    user_id = int(callback.data.split("_")[-1])
    now_blocked = not db.is_user_blocked(user_id)
    db.set_user_blocked(user_id, now_blocked)
    await _render_user_detail(callback, user_id)
    await callback.answer("Заблокирован 🚫" if now_blocked else "Разблокирован ✅")


@router.callback_query(F.data.startswith("adm_user_meditation_toggle_"))
async def adm_user_meditation_toggle(callback: CallbackQuery):
    """VEDA HEALING FLOW - отдельный, никак технически не связанный бот, поэтому
    факт покупки там бот-информатор узнать сам не может - отмечается здесь
    вручную, когда Вы узнали об оплате (из того бота или от самого человека).
    Влияет на 3-ю ступень Пути (см. compute_ascension_level), поэтому при
    появлении отметки сразу проверяем, не открылась ли она."""
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    user_id = int(callback.data[len("adm_user_meditation_toggle_"):])
    u = db.get_user(user_id)
    if not u:
        await callback.answer("Этого человека больше нет среди подписчиков.", show_alert=True)
        return
    old_level = compute_ascension_level(user_id)
    now_bought = not bool(u["bought_meditation_bot"])
    db.set_bought_meditation_bot(user_id, now_bought)
    new_level = compute_ascension_level(user_id)
    if new_level > old_level:
        try:
            await _handle_ascension_transition(callback.bot, user_id, old_level, new_level)
        except Exception:
            logging.exception("Не удалось отправить поздравление со ступенью пользователю %s", user_id)
    await _render_user_detail(callback, user_id)
    await callback.answer("Отмечено ✅" if now_bought else "Отметка снята")


@router.callback_query(F.data.startswith("adm_user_reset_"))
async def adm_user_reset(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    user_id = int(callback.data.split("_")[-1])
    db.reset_user_onboarding(user_id)
    users = db.get_all_users_full()
    text = (
        f"<b>👥 Подписчики бота ({len(users)})</b>\n\n"
        "Имя сброшено: при следующем /start бот заново спросит имя и повторит знакомство. "
        "Ступень, ранг Люминара, покупка медитаций, Sanctum и цена остались как были.\n\n"
        "Нажмите на имя, чтобы посмотреть и управлять."
    )
    await callback.message.edit_text(text, reply_markup=_users_list_kb(users))
    await callback.answer("Имя сброшено 🔄")


# ---------- админ-панель: администраторы ----------

def _admin_perm_picker_kb(selected) -> InlineKeyboardMarkup:
    selected = set(selected)
    rows = []
    for label, buttons in ADMIN_PERMISSION_SECTIONS:
        rows.append(_divider(label))
        for key, text in buttons:
            mark = "✅ " if key in selected else ""
            rows.append([InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"admperm_toggle_{key}")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="admperm_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_admins_screen(callback: CallbackQuery):
    admin_ids = db.get_all_admin_ids()
    lines = ["<b>Администраторы:</b>"]
    rows = []
    for a in admin_ids:
        u = db.get_user(a)
        # имя - из карточки подписчика (кликабельная ссылка на профиль в Telegram);
        # если человек ещё не запускал бота, карточки нет и остаётся только ID
        who = f'<a href="tg://user?id={a}">{html.escape(_user_display_name(u))}</a> ' if u else ""
        if db.is_owner(a):
            lines.append(f"• {who}<code>{a}</code> (владелец - полный доступ)")
        else:
            lines.append(f"• {who}<code>{a}</code>" + ("" if u else " (ещё не запускал бота)"))
            label = _user_display_name(u) if u else str(a)
            rows.append([
                InlineKeyboardButton(text=f"⚙️ Права: {label}"[:40], callback_data=f"adm_perm_edit_{a}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_admin_remove_{a}"),
            ])
    rows.append([InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adm_admin_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm_admins")
async def adm_admins(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_admins"):
        return
    await _render_admins_screen(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_admin_remove_"))
async def adm_admin_remove(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_admins"):
        return
    target_admin_id = int(callback.data[len("adm_admin_remove_"):])
    if db.is_owner(target_admin_id):
        # подстраховка: владельца через эту кнопку в принципе не предлагают
        # удалить (см. _render_admins_screen), но проверяем и здесь на всякий случай
        await callback.answer("Владельца удалить нельзя", show_alert=True)
        return
    db.remove_admin(target_admin_id)
    await _render_admins_screen(callback)
    await callback.answer(f"Администратор {target_admin_id} удалён")


@router.callback_query(F.data.startswith("adm_perm_edit_"))
async def adm_perm_edit_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_admins"):
        return
    target_admin_id = int(callback.data[len("adm_perm_edit_"):])
    current = db.get_admin_permissions(target_admin_id)
    await state.set_state(AdminPermStates.picking)
    await state.update_data(target_admin_id=target_admin_id, selected_perms=list(current))
    await callback.message.answer(
        f"Права администратора {html.escape(_user_display_name(db.get_user(target_admin_id))) if db.get_user(target_admin_id) else target_admin_id}:",
        reply_markup=_admin_perm_picker_kb(current),
    )
    await callback.answer()


@router.callback_query(AdminPermStates.picking, F.data.startswith("admperm_toggle_"))
async def admin_perm_toggle(callback: CallbackQuery, state: FSMContext):
    key = callback.data[len("admperm_toggle_"):]
    data = await state.get_data()
    selected = set(data.get("selected_perms", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    await state.update_data(selected_perms=list(selected))
    await callback.message.edit_reply_markup(reply_markup=_admin_perm_picker_kb(selected))
    await callback.answer()


@router.callback_query(AdminPermStates.picking, F.data == "admperm_done")
async def admin_perm_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_admin_id = data["target_admin_id"]
    selected = data.get("selected_perms", [])
    db.set_admin_permissions(target_admin_id, selected)
    is_new = bool(data.get("new_admin"))
    await state.clear()
    notified = ""
    if is_new:
        ok = await _notify_new_admin(callback.bot, target_admin_id)
        notified = (
            "\n\nЯ написала ему и прислала меню с кнопкой «Админ-панель»." if ok
            else "\n\nНаписать ему не получилось - пусть нажмёт /start, затем откроет панель командой /admin."
        )
    if selected:
        labels = "\n".join(f"• {ADMIN_PERMISSIONS[k]}" for k in selected)
    else:
        labels = "(ничего не выбрано - у этого администратора пока нет доступа ни к одному разделу)"
    known = db.get_user(target_admin_id)
    who = f"{html.escape(_user_display_name(known))} (ID {target_admin_id})" if known else f"ID {target_admin_id}"
    await callback.message.edit_text(f"Права обновлены ✅\n\nДоступно администратору {who}:\n{labels}{notified}")
    await callback.answer()


@router.callback_query(F.data == "adm_admin_add")
async def adm_admin_add_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_admins"):
        return
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="admin_add", field=None)
    await callback.message.answer(
        "Пришлите Telegram ID нового администратора (число).\n\n"
        "Узнать свой ID человек может, например, у бота @userinfobot."
    )
    await callback.answer()


@router.message(EditFieldStates.waiting_value)
async def edit_field_value(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришлите, пожалуйста, обычным текстом 🙏")
        return
    data = await state.get_data()
    target = data["target"]
    field = data.get("field")
    # Эти поля отправляются пользователям без экранирования (html.escape) — значит,
    # жирный шрифт и другая разметка, которую вы выделяете прямо в Telegram при вводе,
    # должна сохраняться как есть. Для остальных полей (название вебинара, реквизиты
    # и т.п.) берём чистый текст — они выводятся с экранированием, и HTML-теги внутри
    # показались бы как есть, а не как форматирование.
    # feed_post — отдельный случай: field это ID конкретной публикации (число,
    # разное каждый раз), а не фиксированное имя поля, поэтому его нельзя
    # перечислить в HTML_TRUSTED_FIELDS заранее — доверяем ему всегда,
    # раз исходный текст поста тоже сохранялся с HTML-разметкой при рассылке
    html_trusted = (
        (target, field) in HTML_TRUSTED_FIELDS or target == "feed_post" or target == "ritual_text"
        or (target == "ritual_event" and field in ("meaning", "practice"))
    )
    value = message.html_text if html_trusted else message.text

    if target == "webinar":
        if field == "date_text":
            try:
                dt = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
            except ValueError:
                await message.answer(
                    "Не получилось распознать дату и время.\n"
                    "Пришлите в формате ДД.ММ.ГГГГ ЧЧ:ММ (например: 15.09.2026 18:00):"
                )
                return
            db.update_webinar_datetime(data["target_id"], dt.isoformat(), _format_event_dt(dt))
            await message.answer("Дата и время обновлены ✅")
        else:
            db.update_webinar_field(data["target_id"], field, value)
            await message.answer("Обновлено ✅")
    elif target == "sanctum":
        db.update_sanctum_field(field, value)
        await message.answer("Обновлено ✅")
    elif target == "payment":
        db.set_setting(field, value)
        await message.answer("Обновлено ✅")
    elif target == "reengage":
        if field in ("stall_hours", "reengage_days_silent", "winback_days_after_expiry", "sanctum_nudge_hours",
                     "price_lock_days", "stage_reset_days", "lastchance_days_before"):
            try:
                n = int(value.strip())
                if n <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Нужно целое число больше нуля (например, 24 или 3). Попробуйте ещё раз:")
                return
            db.set_setting(field, str(n))
        else:
            db.set_setting(field, value)
        await message.answer("Обновлено ✅")
    elif target == "reminder_text":
        db.set_setting(field, value)
        await message.answer("Текст напоминания обновлён ✅")
    elif target == "webinar_reminder_text":
        db.set_setting(field, value)
        await message.answer("Текст напоминания о вебинаре обновлён ✅")
    elif target == "ascension_text":
        db.set_setting(field, value)
        reply = f"«{ASCENSION_TEXT_LABELS[field]}» обновлён ✅"
        # частая опечатка при ручном наборе — круглые скобки вместо фигурных,
        # тогда имя/намерение просто не подставится, а плейсхолдер останется
        # виден человеку как есть (реальный случай, пойманный 2026-09-02)
        for ph in ASCENSION_TEXT_PLACEHOLDERS.get(field, []):
            word = ph.strip("{}")
            if ph not in value and f"({word})" in value:
                reply += (
                    f"\n\n⚠️ Похоже, вместо {ph} (фигурные скобки) в тексте написано ({word}) "
                    f"(круглые) - оно не заменится на имя, а покажется людям как есть. "
                    "Если это не нарочно - пришлите текст ещё раз с фигурными скобками."
                )
        await message.answer(reply)
    elif target == "profile_text":
        db.set_setting(field, value)
        reply = f"«{PROFILE_TEXT_LABELS[field]}» обновлён ✅"
        for ph in PROFILE_TEXT_PLACEHOLDERS.get(field, []):
            word = ph.strip("{}")
            if ph not in value and f"({word})" in value:
                reply += (
                    f"\n\n⚠️ Похоже, вместо {ph} (фигурные скобки) написано ({word}) (круглые) - "
                    "оно не заменится, а покажется людям как есть. Если это не нарочно - пришлите "
                    "текст ещё раз с фигурными скобками."
                )
        if field == "profile_referral_intro_text" and "{ссылка}" not in value:
            reply += (
                "\n\nНа заметку: даже без {ссылка} в тексте бот всё равно допишет настоящую "
                "реферальную ссылку отдельной строкой в конце - она не потеряется."
            )
        await message.answer(reply)
    elif target == "feed_post":
        db.update_feed_post_text(int(field), value)
        await message.answer("Текст публикации в архиве обновлён ✅")
    elif target == "about":
        db.set_setting("about_text", value)
        await message.answer("Текст «Философия Alena Veda» обновлён ✅")
    elif target == "faq":
        db.set_setting("faq_text", value)
        await message.answer("Текст «Частые вопросы» обновлён ✅")
    elif target == "faq_suggestion_invite":
        db.set_setting("faq_suggestion_invite_text", value)
        await message.answer("Текст приглашения предложить вопрос обновлён ✅")
    elif target == "faq_suggestion_confirm":
        db.set_setting("faq_suggestion_confirm_text", value)
        await message.answer("Текст подтверждения обновлён ✅")
    elif target == "rules":
        db.set_setting("rules_text", value)
        await message.answer("Текст «Правила пространства» обновлён ✅")
    elif target == "welcome_text":
        db.set_setting("welcome_text", value)
        await message.answer("Текст приветствия обновлён ✅")
    elif target == "meditation_text":
        db.set_setting("meditation_text", value)
        await message.answer("Текст VEDA HEALING FLOW обновлён ✅")
    elif target == "meditation_coming_soon":
        db.set_setting("meditation_coming_soon_text", value)
        await message.answer("Текст «скоро откроется» обновлён ✅")
    elif target == "meditation_link":
        raw = value.strip().lstrip("@")
        if raw.startswith("http://") or raw.startswith("https://"):
            pass
        elif raw.startswith("t.me/"):
            raw = f"https://{raw}"
        else:
            raw = f"https://t.me/{raw}"
        db.set_setting("meditation_bot_link", raw)
        await message.answer(f"Ссылка на VEDA HEALING FLOW обновлена ✅\n{raw}\n\nКнопка теперь видна людям.")
    elif target == "personal_link":
        raw = value.strip().lstrip("@")
        if raw.startswith("http://") or raw.startswith("https://"):
            pass
        elif raw.startswith("t.me/"):
            raw = f"https://{raw}"
        else:
            raw = f"https://t.me/{raw}"
        db.set_setting("admin_personal_chat_link", raw)
        await message.answer(f"Ссылка на личный чат обновлена ✅\n{raw}")
    elif target in ("ritual_text", "ritual_event", "ritual_add"):
        if await _ritual_edit_value(message, state, data, target, field, value):
            return
    elif target == "admin_add":
        try:
            new_admin_id = int(value.strip())
        except ValueError:
            await message.answer("ID должен быть числом. Попробуйте ещё раз или отправьте /cancel")
            return
        db.add_admin(new_admin_id)
        await state.set_state(AdminPermStates.picking)
        await state.update_data(
            target_admin_id=new_admin_id, selected_perms=list(DEFAULT_HELPER_PERMISSIONS), new_admin=True
        )
        known = db.get_user(new_admin_id)
        who = f"{html.escape(_user_display_name(known))} (ID {new_admin_id})" if known else f"ID {new_admin_id}"
        warn = "" if known else (
            "\n\n⚠️ Этот человек ещё ни разу не запускал бота - написать ему я пока не смогу. "
            "Пусть нажмёт /start, и тогда откроет панель командой /admin."
        )
        await message.answer(
            f"Администратор {who} добавлен ✅{warn}\n\n"
            "Теперь отметьте, какие разделы ему доступны (по умолчанию отмечен обычный набор для "
            "помощника - можно менять). Нажмите «✅ Готово», когда закончите:",
            reply_markup=_admin_perm_picker_kb(DEFAULT_HELPER_PERMISSIONS),
        )
        return
    await state.clear()


# ---------- админ-панель: фото на разных экранах ----------

@router.callback_query(F.data == "adm_photo_welcome")
async def adm_photo_welcome_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_photo_welcome"):
        return
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="welcome")
    current = "уже установлено" if db.get_setting("welcome_photo") else "не установлено"
    await callback.message.answer(
        f"Пришлите фото, которое будет показываться первым после /start (сейчас {current}).\n\n"
        "Или отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


@router.callback_query(F.data == "adm_photo_about")
async def adm_photo_about_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_photo_about"):
        return
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="about")
    current = "уже установлено" if db.get_setting("about_photo") else "не установлено"
    await callback.message.answer(
        f"Пришлите фото, которое будет показываться перед текстом «Философия Alena Veda» "
        f"(сейчас {current}).\n\n"
        "Или отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


ASCENSION_PHOTO_LABELS = {
    "ascension_level1_photo": "Фото «Первое Касание»",
    "ascension_level2_photo": "Фото «Искра»",
    "ascension_level3_photo": "Фото «Исследователь Глубины»",
    "luminar_1_photo": "Фото поздравления Люминар I",
    "luminar_2_photo": "Фото поздравления Люминар II",
    "luminar_3_photo": "Фото поздравления Люминар III",
}


@router.callback_query(F.data == "adm_ascension_photos")
async def adm_ascension_photos(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_ascension_photos"):
        return
    lines = [
        "<b>🖼 Фото Пути Восхождения и Люминаров</b>",
        "",
        "Если фото задано - сообщение уйдёт одним целым (фото с подписью), а не отдельно текстом. "
        "У Telegram подпись к фото ограничена 1024 символами - если сам текст длиннее (сейчас так "
        "только у «Первое Касание»), фото и полный текст всё равно придут вместе, но двумя "
        "сообщениями подряд, чтобы ни слова не потерялось.",
        "",
    ]
    for key, label in ASCENSION_PHOTO_LABELS.items():
        status = "✅ задано" if db.get_setting(key) else "- не задано"
        lines.append(f"{label}: {status}")
    rows = [
        [InlineKeyboardButton(text=f"🖼 {label}", callback_data=f"adm_ap_{key}")]
        for key, label in ASCENSION_PHOTO_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ap_"))
async def adm_ascension_photo_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_ap_"):]
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="ascension_photo", field=field)
    current = "уже установлено" if db.get_setting(field) else "не установлено"
    await callback.message.answer(
        f"Пришлите фото для «{ASCENSION_PHOTO_LABELS[field]}» (сейчас {current}).\n\n"
        "Или отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


# (label, тип содержимого) - на будущее легко добавить сюда и для других ступеней,
# сейчас заведено только для «Искра», как она попросила первой
ASCENSION_EXTRA_MEDIA_LABELS = {
    "ascension_level2_video_note": ("🎥 Видеокружок «Искра»", "video_note"),
    "ascension_level2_voice": ("🎤 Голосовое «Искра»", "voice"),
}


class AscensionMediaStates(StatesGroup):
    waiting_media = State()


@router.callback_query(F.data == "adm_ascension_media")
async def adm_ascension_media(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_ascension_media"):
        return
    lines = [
        "<b>🎥 Видео и голос на переходе ступени</b>",
        "",
        "Необязательное личное дополнение к тексту-поздравлению - отправляется отдельным "
        "сообщением ПЕРЕД самим текстом (у кружочков и голосовых в Telegram не бывает "
        "подписи, поэтому не совмещаются с текстом в одно сообщение, как фото).",
        "",
    ]
    for key, (label, _) in ASCENSION_EXTRA_MEDIA_LABELS.items():
        status = "✅ задано" if db.get_setting(key) else "- не задано"
        lines.append(f"{label}: {status}")
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm_am_{key}")]
        for key, (label, _) in ASCENSION_EXTRA_MEDIA_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_am_"))
async def adm_ascension_media_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    field = callback.data[len("adm_am_"):]
    label, kind = ASCENSION_EXTRA_MEDIA_LABELS[field]
    await state.set_state(AscensionMediaStates.waiting_media)
    await state.update_data(field=field, kind=kind)
    current = "уже установлено" if db.get_setting(field) else "не установлено"
    kind_label = "видеокружок" if kind == "video_note" else "голосовое сообщение"
    await callback.message.answer(
        f"Пришлите {kind_label} для «{label}» (сейчас {current}).\n\n"
        "Проще всего: запишите несколько дублей, сохраните их себе в «Избранное» в Telegram, "
        "выберите лучший - и перешлите (кнопка «Переслать») именно его сюда, в этот чат.\n\n"
        "Или отправьте «-», чтобы убрать текущее."
    )
    await callback.answer()


@router.message(AscensionMediaStates.waiting_media)
async def ascension_media_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field, kind = data["field"], data["kind"]
    if kind == "video_note" and message.video_note:
        file_id = message.video_note.file_id
    elif kind == "voice" and message.voice:
        file_id = message.voice.file_id
    elif message.text and message.text.strip() == "-":
        file_id = ""
    else:
        wrong = "видеокружок" if kind == "video_note" else "голосовое сообщение"
        await message.answer(
            f"Пришлите, пожалуйста, именно {wrong} (можно переслать из «Избранного»), "
            "или «-», чтобы убрать текущее."
        )
        return
    db.set_setting(field, file_id)
    await state.clear()
    await message.answer("Обновлено ✅" if file_id else "Убрано ✅")


@router.callback_query(F.data == "adm_photo_meditation")
async def adm_photo_meditation_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_photo_meditation"):
        return
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="meditation")
    current = "уже установлено" if db.get_setting("meditation_photo") else "не установлено"
    await callback.message.answer(
        f"Пришлите фото, которое будет показываться перед описанием VEDA HEALING FLOW (сейчас {current}).\n\n"
        "Или отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


@router.callback_query(F.data == "adm_photo_sanctum_intro")
async def adm_photo_sanctum_intro_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="sanctum_intro")
    s = db.get_sanctum()
    current = "уже установлено" if s["intro_photo"] else "не установлено"
    await callback.message.answer(
        f"Пришлите фото, которое будет показываться перед сообщением 1 (приглашение) VEDA SANCTUM "
        f"(сейчас {current}).\n\n"
        "Или отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_photo_webinar_"))
async def adm_photo_webinar_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    webinar_id = int(callback.data.split("_")[-1])
    await state.set_state(PhotoUploadStates.waiting_photo)
    await state.update_data(target="webinar", target_id=webinar_id)
    w = db.get_webinar(webinar_id)
    current = "уже установлено" if (w and w["photo"]) else "не установлено"
    await callback.message.answer(
        f"Пришлите фото для этого вебинара (сейчас {current}).\n\nИли отправьте «-», чтобы убрать фото."
    )
    await callback.answer()


@router.message(PhotoUploadStates.waiting_photo)
async def photo_upload_value(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data["target"]

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.text and message.text.strip() == "-":
        file_id = ""
    else:
        await message.answer("Пришлите, пожалуйста, фото, или «-», чтобы убрать текущее.")
        return

    if target == "welcome":
        db.set_setting("welcome_photo", file_id)
    elif target == "meditation":
        db.set_setting("meditation_photo", file_id)
    elif target == "about":
        db.set_setting("about_photo", file_id)
    elif target == "sanctum_intro":
        db.update_sanctum_field("intro_photo", file_id)
    elif target == "webinar":
        db.update_webinar_field(data["target_id"], "photo", file_id)
    elif target == "ascension_photo":
        db.set_setting(data["field"], file_id)

    await state.clear()
    await message.answer("Фото обновлено ✅" if file_id else "Фото убрано ✅")


# ---------- админ-панель: заявки на подтверждение ----------

@router.callback_query(F.data == "adm_pending")
async def adm_pending(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_pending"):
        return
    pending = db.get_pending_registrations()
    if not pending:
        await callback.answer("Нет заявок, ожидающих проверки", show_alert=True)
        return
    await callback.answer()
    for reg in pending:
        context_note = _sanctum_payment_context(reg["user_id"]) if reg["product_type"] == "sanctum" else ""
        caption = (
            "🆕 Заявка на проверку\n\n"
            f"ID пользователя: {reg['user_id']}\n"
            f"Продукт: {reg['product_title']}\n"
            f"Сумма: {reg['price']}"
            f"{context_note}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"reg_confirm_{reg['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg_decline_{reg['id']}"),
        ]])
        if reg["receipt_file_id"]:
            await callback.message.answer_photo(reg["receipt_file_id"], caption=caption, reply_markup=kb)
        else:
            await callback.message.answer(caption, reply_markup=kb)


@router.callback_query(F.data == "adm_stalled")
async def adm_stalled(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_stalled"):
        return
    stalled = db.get_all_awaiting_receipt()
    if not stalled:
        await callback.answer("Сейчас нет заявок, застрявших на этапе оплаты 🙌", show_alert=True)
        return
    lines = [
        "<b>⏳ Зависшие заявки (чек ещё не прислали)</b>\n"
        "Люди, которые начали оформление (вебинар/практику/расстановку или VEDA SANCTUM), "
        "но скриншот оплаты пока не прислали.\n"
    ]
    for reg in stalled:
        user_row = db.get_user(reg["user_id"])
        name = _user_display_name(user_row) if user_row else str(reg["user_id"])
        started = (reg["created_at"] or "").split(" ")[0]
        lines.append(
            f"👤 {html.escape(name)} (ID {reg['user_id']}) - {html.escape(reg['product_title'] or '-')} - "
            f"{reg['price']} - начал(а) {started}"
        )
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ---------- админ-панель: рассылка ----------

def _segment_user_ids_multi(segments: list) -> list:
    ids = set()
    for seg in segments:
        ids.update(_segment_user_ids(seg))
    ids -= set(db.get_blocked_user_ids())
    ids -= set(db.get_self_departed_user_ids())
    return list(ids)


def _personalize(text, user_row):
    """Подставляет имя конкретного получателя вместо метки {имя} в тексте
    рассылки — в первую очередь то имя, которое человек сам назвал боту
    (preferred_name), и только если его нет — имя из профиля Telegram.
    Если вообще ничего нет — нейтральное "друг", чтобы фраза не обрывалась."""
    if not text or "{имя}" not in text:
        return text
    name = ""
    if user_row:
        name = (user_row["preferred_name"] or user_row["first_name"] or "").strip()
    return text.replace("{имя}", name or "друг")


def _segment_picker_kb(selected: list) -> InlineKeyboardMarkup:
    rows = []
    for seg, label in SEGMENT_LABELS.items():
        count = len(_segment_user_ids(seg))
        mark = "✅ " if seg in selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label} ({count})", callback_data=f"bc_segtoggle_{seg}")])
    total = len(_segment_user_ids_multi(selected))
    done_text = f"➡️ Дальше ({total} чел.)" if selected else "➡️ Дальше"
    rows.append([InlineKeyboardButton(text=done_text, callback_data="bc_seg_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bc_back_kb(extra_rows: list = None) -> InlineKeyboardMarkup:
    """Общая кнопка «Назад» для мастера рассылки. Пути внутри него сходятся
    (например, на «Добавить в архив?» попадают и с выбора кнопки, и в обход
    неё для альбома) — поэтому вместо фиксированной цели на каждый экран
    ведём историю РЕАЛЬНО пройденных шагов (bc_history в данных состояния) и
    возвращаемся туда, откуда человек в этот раз пришёл на самом деле, а не
    куда-то заранее угаданное."""
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="bc_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _bc_push(state: FSMContext, step_name: str):
    data = await state.get_data()
    history = data.get("bc_history", [])
    history.append(step_name)
    await state.update_data(bc_history=history)


async def _bc_ask_segment(answer, state: FSMContext):
    data = await state.get_data()
    selected = data.get("segments", [])
    await state.set_state(BroadcastStates.waiting_segment)
    await answer(
        "Кому отправить рассылку? Можно выбрать несколько групп - тапайте по нужным, "
        "они отметятся галочкой, потом нажмите «Дальше».",
        reply_markup=_segment_picker_kb(selected),
    )


async def _bc_ask_content(answer, state: FSMContext):
    data = await state.get_data()
    selected = data.get("segments", [])
    await state.set_state(BroadcastStates.waiting_content)
    header = ""
    if selected:
        count = len(_segment_user_ids_multi(selected))
        labels = ", ".join(SEGMENT_LABELS[s] for s in selected)
        header = f"Выбрано: {labels} ({count} чел.)\n\n"
    await answer(
        f"{header}Пришлите пост для рассылки - текст, фото, видео или кружочек (можно с подписью, "
        "кроме кружочка - у него подписи не бывает).\n\n"
        "Подсказка: если написать {имя} где-нибудь в тексте, каждому человеку подставится "
        "именно его имя из Telegram.",
        reply_markup=_bc_back_kb(),
    )


async def _bc_ask_more_photos(answer, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    await state.set_state(BroadcastStates.waiting_more_photos)
    await answer(
        f"Фото добавлено ({len(photos)}). Если нужен альбом из нескольких фото - присылайте ещё, "
        "по одному. Когда фото достаточно - нажмите «Готово».",
        reply_markup=_bc_back_kb([[InlineKeyboardButton(text="✅ Готово, фото достаточно", callback_data="bc_photos_done")]]),
    )


async def _bc_ask_button_choice(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_button_choice)
    kb = _bc_back_kb([
        [InlineKeyboardButton(text="🔗 Обычная ссылка", callback_data="bc_btn_url")],
        [InlineKeyboardButton(text="📅 Кнопка «Зарегистрироваться на вебинар»", callback_data="bc_btn_webinar")],
        [InlineKeyboardButton(text="🔁 Кнопка «Поделиться ботом»", callback_data="bc_btn_share")],
        [InlineKeyboardButton(text="Без кнопки", callback_data="bc_btn_no")],
    ])
    await answer("Добавить кнопку под постом?", reply_markup=kb)


async def _bc_ask_button_text(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_button_text)
    await answer("Введите текст на кнопке (например: Подробнее):", reply_markup=_bc_back_kb())


async def _bc_ask_button_url(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_button_url)
    await answer("Теперь пришлите ссылку для кнопки (например: https://t.me/ваш_канал):", reply_markup=_bc_back_kb())


async def _bc_ask_archive_choice(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_archive_choice)
    kb = _bc_back_kb([
        [InlineKeyboardButton(text="✅ Да, добавить в архив", callback_data="bc_archive_yes")],
        [InlineKeyboardButton(text="Нет, только разовая рассылка", callback_data="bc_archive_no")],
    ])
    await answer(f"Добавить этот пост в «{BTN_FEED}»?", reply_markup=kb)


async def _bc_ask_archive_days(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_archive_days)
    await answer(
        "На сколько дней хранить в архиве? Пришлите число (например, 14), "
        "или «-», чтобы хранить бессрочно (пока сами не удалите):",
        reply_markup=_bc_back_kb(),
    )


async def _bc_ask_allow_questions(answer, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_allow_questions)
    kb = _bc_back_kb([
        [InlineKeyboardButton(text="✅ Да, разрешить", callback_data="bc_q_yes")],
        [InlineKeyboardButton(text="Нет, без вопросов", callback_data="bc_q_no")],
    ])
    await answer("Разрешить людям задавать вопросы под этой публикацией в архиве?", reply_markup=kb)


BC_ASK_FUNCS = {
    "segment": _bc_ask_segment,
    "content": _bc_ask_content,
    "more_photos": _bc_ask_more_photos,
    "button_choice": _bc_ask_button_choice,
    "button_text": _bc_ask_button_text,
    "button_url": _bc_ask_button_url,
    "archive_choice": _bc_ask_archive_choice,
    "archive_days": _bc_ask_archive_days,
    "allow_questions": _bc_ask_allow_questions,
}


@router.callback_query(F.data == "bc_back")
async def adm_broadcast_back(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    data = await state.get_data()
    history = data.get("bc_history", [])
    if not history:
        await callback.answer()
        return
    prev_step = history.pop()
    await state.update_data(bc_history=history)
    await BC_ASK_FUNCS[prev_step](callback.message.answer, state)
    await callback.answer()


@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_broadcast"):
        return
    await state.update_data(segments=[], bc_history=[])
    await _bc_ask_segment(callback.message.answer, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_segment, F.data.startswith("bc_segtoggle_"))
async def adm_broadcast_segment_toggle(callback: CallbackQuery, state: FSMContext):
    segment = callback.data[len("bc_segtoggle_"):]
    data = await state.get_data()
    selected = data.get("segments", [])
    if segment in selected:
        selected.remove(segment)
    else:
        selected.append(segment)
    await state.update_data(segments=selected)
    await callback.message.edit_reply_markup(reply_markup=_segment_picker_kb(selected))
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_segment, F.data == "bc_seg_done")
async def adm_broadcast_segment_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("segments", [])
    if not selected:
        await callback.answer("Выберите хотя бы одну группу", show_alert=True)
        return
    count = len(_segment_user_ids_multi(selected))
    if count == 0:
        await callback.answer("В выбранных группах пока нет ни одного человека", show_alert=True)
        return
    await _bc_push(state, "segment")
    await _bc_ask_content(callback.message.answer, state)
    await callback.answer()


@router.message(BroadcastStates.waiting_content)
async def adm_broadcast_content(message: Message, state: FSMContext):
    if message.photo:
        await state.update_data(photos=[message.photo[-1].file_id], text=message.html_text or "")
        await _bc_push(state, "content")
        await _bc_ask_more_photos(message.answer, state)
        return
    elif message.video:
        content_type, file_id = "video", message.video.file_id
    elif message.video_note:
        content_type, file_id = "video_note", message.video_note.file_id
    elif message.text:
        content_type, file_id = "text", None
    else:
        await message.answer(
            "Для рассылки подходит только текст, фото, видео или кружочек. "
            "Пришлите, пожалуйста, что-то из этого 🙏"
        )
        return

    await state.update_data(content_type=content_type, text=message.html_text or "", file_id=file_id)
    await _bc_push(state, "content")
    await _bc_ask_button_choice(message.answer, state)


@router.message(BroadcastStates.waiting_more_photos, F.photo)
async def adm_broadcast_more_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(
        f"Фото добавлено ({len(photos)}). Присылайте ещё или нажмите «Готово».",
        reply_markup=_bc_back_kb([[InlineKeyboardButton(text="✅ Готово, фото достаточно", callback_data="bc_photos_done")]]),
    )


@router.message(BroadcastStates.waiting_more_photos)
async def adm_broadcast_more_photo_wrong_type(message: Message):
    await message.answer("Пришлите, пожалуйста, ещё одно фото, или нажмите «Готово» под предыдущим сообщением 🙏")


@router.callback_query(BroadcastStates.waiting_more_photos, F.data == "bc_photos_done")
async def adm_broadcast_photos_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) == 1:
        await state.update_data(content_type="photo", file_id=photos[0])
        await _bc_push(state, "more_photos")
        await _bc_ask_button_choice(callback.message.answer, state)
    else:
        await state.update_data(content_type="album", file_ids=photos)
        await callback.message.answer(
            f"Альбом из {len(photos)} фото готов. У альбомов в Telegram нельзя добавить кнопку под постом "
            "(так устроен сам Telegram) - переходим сразу к отправке."
        )
        await _bc_push(state, "more_photos")
        await _bc_ask_archive_choice(callback.message.answer, state)
    await callback.answer()


async def _broadcast_ask_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    segments = data.get("segments", ["all"])
    count = len(_segment_user_ids_multi(segments))
    await state.set_state(BroadcastStates.waiting_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📤 Отправить ({count} чел.)", callback_data="bc_send")],
        [InlineKeyboardButton(text="Отмена", callback_data="bc_cancel")],
    ])
    await message.answer("Готово к отправке. Подтверждаете рассылку?", reply_markup=kb)


@router.callback_query(BroadcastStates.waiting_archive_choice, F.data == "bc_archive_no")
async def adm_broadcast_archive_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(archive=False)
    await _bc_push(state, "archive_choice")
    await _broadcast_ask_confirm(callback.message, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_archive_choice, F.data == "bc_archive_yes")
async def adm_broadcast_archive_yes(callback: CallbackQuery, state: FSMContext):
    await state.update_data(archive=True)
    await _bc_push(state, "archive_choice")
    await _bc_ask_archive_days(callback.message.answer, state)
    await callback.answer()


@router.message(BroadcastStates.waiting_archive_days)
async def adm_broadcast_archive_days(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    text = text.strip()
    if text == "-":
        await state.update_data(archive_days=None)
    else:
        try:
            n = int(text)
            if n <= 0:
                raise ValueError
        except ValueError:
            await message.answer(
                "Нужно целое число больше нуля, или «-» для бессрочного хранения. Попробуйте ещё раз:"
            )
            return
        await state.update_data(archive_days=n)
    await _bc_push(state, "archive_days")
    await _bc_ask_allow_questions(message.answer, state)


@router.callback_query(BroadcastStates.waiting_allow_questions, F.data == "bc_q_yes")
async def adm_broadcast_allow_questions_yes(callback: CallbackQuery, state: FSMContext):
    await state.update_data(allow_questions=True)
    await _bc_push(state, "allow_questions")
    await _broadcast_ask_confirm(callback.message, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_allow_questions, F.data == "bc_q_no")
async def adm_broadcast_allow_questions_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(allow_questions=False)
    await _bc_push(state, "allow_questions")
    await _broadcast_ask_confirm(callback.message, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_button_choice, F.data == "bc_btn_no")
async def adm_broadcast_no_button(callback: CallbackQuery, state: FSMContext):
    await state.update_data(button_kind=None)
    await _bc_push(state, "button_choice")
    await _bc_ask_archive_choice(callback.message.answer, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_button_choice, F.data == "bc_btn_url")
async def adm_broadcast_url_button(callback: CallbackQuery, state: FSMContext):
    await state.update_data(button_kind="url")
    await _bc_push(state, "button_choice")
    await _bc_ask_button_text(callback.message.answer, state)
    await callback.answer()


@router.message(BroadcastStates.waiting_button_text)
async def adm_broadcast_button_text(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    await state.update_data(button_text=text)
    await _bc_push(state, "button_text")
    await _bc_ask_button_url(message.answer, state)


@router.message(BroadcastStates.waiting_button_url)
async def adm_broadcast_button_url(message: Message, state: FSMContext):
    text = await _require_text(message)
    if text is None:
        return
    await state.update_data(button_url=text)
    await _bc_push(state, "button_url")
    await _bc_ask_archive_choice(message.answer, state)


@router.callback_query(BroadcastStates.waiting_button_choice, F.data == "bc_btn_webinar")
async def adm_broadcast_webinar_button(callback: CallbackQuery):
    webinars = [w for w in db.get_active_webinars() if w["title"] and w["price"]]
    if not webinars:
        await callback.answer("Пока нет ни одного активного вебинара с заполненными данными", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_strip_html_tags(w["title"]), callback_data=f"bc_wbpick_{w['id']}")]
        for w in webinars
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="bc_btn_webinar_back")]])
    await callback.message.answer("На какой вебинар должна вести кнопка?", reply_markup=kb)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_button_choice, F.data == "bc_btn_webinar_back")
async def adm_broadcast_webinar_button_back(callback: CallbackQuery, state: FSMContext):
    # это НЕ откат по истории (bc_back) — выбор конкретного вебинара для кнопки
    # не был отдельным шагом состояния (всё ещё waiting_button_choice), поэтому
    # просто перерисовываем тот же самый экран "Добавить кнопку под постом?",
    # не трогая bc_history - иначе откатило бы на шаг дальше, чем нужно
    await _bc_ask_button_choice(callback.message.answer, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_button_choice, F.data.startswith("bc_wbpick_"))
async def adm_broadcast_webinar_button_pick(callback: CallbackQuery, state: FSMContext):
    webinar_id = int(callback.data.split("_")[-1])
    w = db.get_webinar(webinar_id)
    if not w:
        await callback.answer("Вебинар не найден", show_alert=True)
        return
    await state.update_data(
        button_kind="callback",
        button_text="Зарегистрироваться на вебинар",
        button_data=f"wb_view_{webinar_id}",
    )
    await _bc_push(state, "button_choice")
    await _bc_ask_archive_choice(callback.message.answer, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_button_choice, F.data == "bc_btn_share")
async def adm_broadcast_share_button(callback: CallbackQuery, state: FSMContext):
    me = await callback.bot.get_me()
    bot_link = f"https://t.me/{me.username}"
    share_text = "Загляните сюда: вебинары, практики и VEDA SANCTUM ✨"
    share_url = "https://t.me/share/url?" + urllib.parse.urlencode({"url": bot_link, "text": share_text})
    await state.update_data(button_kind="url", button_text="🔁 Поделиться ботом", button_url=share_url)
    await _bc_push(state, "button_choice")
    await _bc_ask_archive_choice(callback.message.answer, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_confirm, F.data == "bc_cancel")
async def adm_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_confirm, F.data == "bc_send")
async def adm_broadcast_send(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await callback.answer("Начинаю рассылку…")

    kb = None
    if data.get("button_kind") == "url" and data.get("button_text") and data.get("button_url"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=data["button_text"], url=data["button_url"])]
        ])
    elif data.get("button_kind") == "callback" and data.get("button_text") and data.get("button_data"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=data["button_text"], callback_data=data["button_data"])]
        ])

    if data.get("archive"):
        # сохраняем "сырой" текст (с {имя} как есть) — при просмотре в архиве
        # он будет подставляться заново под каждого конкретного зрителя
        days = data.get("archive_days")
        expires_at = (_today() + timedelta(days=days)).isoformat() if days else None
        file_ids_json = json.dumps(data["file_ids"]) if data.get("content_type") == "album" else None
        db.add_feed_post(
            data.get("content_type"), data.get("text") or "", data.get("file_id"), file_ids_json,
            expires_at, allow_questions=data.get("allow_questions", False),
        )

    sent, failed = 0, 0
    for user_id in _segment_user_ids_multi(data.get("segments", ["all"])):
        protect = _protect_for(user_id)
        text = _personalize(data.get("text"), db.get_user(user_id))
        try:
            if data["content_type"] == "photo":
                await _send_with_optional_photo(
                    callback.bot, user_id, text or "", data["file_id"], reply_markup=kb, protect_content=protect
                )
            elif data["content_type"] == "video":
                try:
                    await callback.bot.send_video(
                        user_id, data["file_id"], caption=text or None, reply_markup=kb, protect_content=protect
                    )
                except TelegramBadRequest:
                    # тот же лимит подписи (1024 символа), что и у фото - видео
                    # отдельно, полный текст следом, чтобы ничего не потерять
                    await callback.bot.send_video(user_id, data["file_id"], protect_content=protect)
                    await callback.bot.send_message(user_id, text, reply_markup=kb, protect_content=protect)
            elif data["content_type"] == "video_note":
                await callback.bot.send_video_note(user_id, data["file_id"], reply_markup=kb, protect_content=protect)
            elif data["content_type"] == "album":
                media_group = [InputMediaPhoto(media=fid) for fid in data["file_ids"]]
                if text:
                    media_group[0] = InputMediaPhoto(media=data["file_ids"][0], caption=text)
                await callback.bot.send_media_group(user_id, media_group, protect_content=protect)
            else:
                await callback.bot.send_message(user_id, text, reply_markup=kb, protect_content=protect)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(config.BROADCAST_DELAY)

    await callback.message.answer(f"Рассылка завершена ✅\nДоставлено: {sent}\nНе доставлено: {failed}")


# ---------- напоминания о продлении VEDA SANCTUM ----------

async def check_sanctum_reminders(bot: Bot):
    logging.info("[планировщик] check_sanctum_reminders: старт")
    today = _today()
    renew_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продлить", callback_data="sanctum_apply")],
        [InlineKeyboardButton(text="⏰ Оплачу позже - назначить дату", callback_data="sanctum_promise")],
    ])

    reminder_date = (today + timedelta(days=config.SANCTUM_REMINDER_DAYS_BEFORE)).isoformat()
    early_template = db.get_setting("sanctum_reminder_text_early")
    for m in db.get_memberships_expiring_on(reminder_date, "reminder_3d_sent_for"):
        date_text = datetime.strptime(m["valid_until"], "%Y-%m-%d").strftime("%d.%m.%Y")
        price_text = m["price"] or db.get_sanctum()["price"]
        text = early_template.replace("{date}", date_text).replace("{price}", price_text)
        try:
            await bot.send_message(m["user_id"], text, reply_markup=renew_kb)
        except Exception:
            logging.exception("Не удалось отправить напоминание пользователю %s", m["user_id"])
        db.mark_reminder_sent(m["user_id"], "reminder_3d_sent_for", m["valid_until"])

    expiry_date = today.isoformat()
    due_template = db.get_setting("sanctum_reminder_text_due")
    for m in db.get_memberships_expiring_on(expiry_date, "reminder_0d_sent_for"):
        date_text = datetime.strptime(m["valid_until"], "%Y-%m-%d").strftime("%d.%m.%Y")
        price_text = m["price"] or db.get_sanctum()["price"]
        text = due_template.replace("{date}", date_text).replace("{price}", price_text)
        try:
            await bot.send_message(m["user_id"], text, reply_markup=renew_kb)
        except Exception:
            logging.exception("Не удалось отправить напоминание пользователю %s", m["user_id"])
        db.mark_reminder_sent(m["user_id"], "reminder_0d_sent_for", m["valid_until"])

    # напоминание за день до даты, которую человек сам назвал ("оплачу позже")
    promise_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продлить", callback_data="sanctum_apply")]
    ])
    promise_reminder_date = (today + timedelta(days=1)).isoformat()
    promise_template = db.get_setting("sanctum_promise_reminder_text")
    for m in db.get_promises_due_on(promise_reminder_date):
        date_text = datetime.strptime(m["promise_date"], "%Y-%m-%d").strftime("%d.%m.%Y")
        price_text = _price_for_user(m["user_id"])
        text = promise_template.replace("{date}", date_text).replace("{price}", price_text)
        try:
            await bot.send_message(m["user_id"], text, reply_markup=promise_kb)
        except Exception:
            logging.exception("Не удалось отправить напоминание об обещании пользователю %s", m["user_id"])
        db.mark_promise_reminder_sent(m["user_id"], m["promise_date"])
    logging.info("[планировщик] check_sanctum_reminders: завершено")


# ---------- автовозврат "потерянных" людей ----------

async def check_reengagement(bot: Bot):
    logging.info("[планировщик] check_reengagement: старт")
    now = datetime.now(TZ)

    if (db.get_setting("stall_enabled") or "1") == "1":
        stall_hours = int(db.get_setting("stall_hours") or "24")
        cutoff = (now - timedelta(hours=stall_hours)).strftime("%Y-%m-%d %H:%M:%S")
        template = db.get_setting("stall_text") or ""
        for reg in db.get_stalled_registrations(cutoff):
            user_row = db.get_user(reg["user_id"])
            text = _personalize(template, user_row).replace("{product}", reg["product_title"] or "")
            try:
                await bot.send_message(reg["user_id"], text, reply_markup=_receipt_help_kb(reg["id"]))
            except Exception:
                logging.exception("Не удалось отправить напоминание о зависшей оплате пользователю %s", reg["user_id"])
            db.mark_stall_reminder_sent(reg["id"])

    # единственная кнопка в этом напоминании — самый лёгкий шаг (VEDA HEALING FLOW),
    # намеренно без кнопки на VEDA SANCTUM (приглашение туда звучит только словом
    # в тексте). Пока ссылка на VEDA HEALING FLOW не задана в админ-панели, это
    # напоминание вообще НЕ отправляем — текст целиком построен вокруг перехода
    # туда, а reengage_sent ставится один раз и навсегда, так что отправить его
    # "без кнопки" означало бы безвозвратно сжечь единственный шанс достучаться
    # до этого человека.
    reengage_btn = _meditation_button("🧘🏽‍♀️ Перейти в VEDA HEALING FLOW")
    if (db.get_setting("reengage_enabled") or "1") == "1" and reengage_btn:
        days_silent = int(db.get_setting("reengage_days_silent") or "3")
        cutoff = (now - timedelta(days=days_silent)).strftime("%Y-%m-%d %H:%M:%S")
        template = db.get_setting("reengage_text") or ""
        reengage_kb = InlineKeyboardMarkup(inline_keyboard=[[reengage_btn]])
        for user_row in db.get_silent_never_purchased_user_ids(cutoff):
            text = _personalize(template, user_row)
            try:
                await bot.send_message(user_row["user_id"], text, reply_markup=reengage_kb)
            except Exception:
                logging.exception("Не удалось отправить напоминание \"пришёл и пропал\" пользователю %s", user_row["user_id"])
            db.mark_reengage_sent(user_row["user_id"])

    # возврат тех, кто БЫЛ в VEDA SANCTUM и не продлил (в отличие от блока выше -
    # там люди, которые вообще никогда ничего не покупали) - её явное решение
    # 2026-09-17, закрывает пробел: раньше после дня истечения бот больше
    # никогда сам не напоминал о себе таким людям
    if (db.get_setting("winback_enabled") or "1") == "1":
        winback_days = int(db.get_setting("winback_days_after_expiry") or "7")
        cutoff_date = (_today() - timedelta(days=winback_days)).isoformat()
        template = db.get_setting("winback_text") or ""
        lock_days = _int_setting("price_lock_days", 30)
        for m in db.get_lapsed_sanctum_user_ids(cutoff_date):
            expired = datetime.strptime(m["valid_until"], "%Y-%m-%d").date()
            deadline = expired + timedelta(days=lock_days)
            # человек сам отметил в боте, что оплатит позже, и дата ещё не
            # наступила - не беспокоим (и НЕ отмечаем как отправленное, вернёмся
            # к нему, если дата пройдёт без оплаты)
            if m["promise_date"]:
                promise = _parse_date(m["promise_date"])
                if promise and promise >= _today():
                    continue
            if _today() > deadline or _today() >= deadline - timedelta(days=_int_setting("lastchance_days_before", 5)):
                # срок сохранения цены уже прошёл - письмо "цена сохраняется"
                # было бы неправдой; а если он вот-вот закончится - вместо этого
                # письма уйдёт "последний шанс" (ниже), два почти одинаковых
                # письма в один день не нужны. Просто закрываем этот случай.
                db.mark_winback_sent(m["user_id"])
                continue
            price_text = m["sanctum_price"] or db.get_sanctum()["price"]
            text = _lapse_text(template, m, expired, deadline, price_text)
            try:
                await bot.send_message(m["user_id"], text, reply_markup=_return_kb(with_promise=True))
            except Exception:
                logging.exception("Не удалось отправить возвратное напоминание пользователю %s", m["user_id"])
            db.mark_winback_sent(m["user_id"])

    # правила ухода из Sanctum (её решение 2026-09-20): 1) незадолго до конца
    # срока сохранения цены - последний шанс вернуться по прежней цене;
    # 2) через stage_reset_days отсутствия - время в поле для ступеней Пути
    # обнуляется (ранг Люминара и покупка VEDA HEALING FLOW остаются навсегда)
    lock_days = _int_setting("price_lock_days", 30)
    reset_days = _int_setting("stage_reset_days", 90)
    before_days = _int_setting("lastchance_days_before", 5)
    today = _today()
    for m in db.get_memberships_for_rules():
        if m["blocked"] or not _is_lapsed(m):
            continue
        anchor = _lapse_anchor(m)
        deadline = anchor + timedelta(days=lock_days)

        if not m["lastchance_sent"] and (deadline - timedelta(days=before_days)) <= today <= deadline:
            if not _promise_active(m):
                price_text = m["price"] or db.get_sanctum()["price"]
                text = _lapse_text(db.get_setting("sanctum_lastchance_text"), m, anchor, deadline, price_text)
                try:
                    await bot.send_message(m["user_id"], text, reply_markup=_return_kb(with_promise=True))
                except Exception:
                    logging.exception("Не удалось отправить напоминание 'последний шанс' пользователю %s", m["user_id"])
                db.mark_lastchance_sent(m["user_id"])

        if not m["stage_reset_done"] and today >= anchor + timedelta(days=reset_days):
            had_stage = (m["accumulated_days"] or 0) / 30 >= 2  # была ступень "Искра" и выше
            db.mark_stage_reset_done(m["user_id"])
            if had_stage:
                kept = []
                if _luminar_rank(m["luminar_count"]) >= 1:
                    kept.append(db.get_setting("sanctum_reset_luminar_line") or "")
                if m["bought_meditation_bot"]:
                    kept.append(db.get_setting("sanctum_reset_meditation_line") or "")
                text = _personalize(db.get_setting("sanctum_reset_text") or "", m).replace(
                    "{сохранено}", "\n".join(line for line in kept if line)
                )
                text = re.sub(r"\n{3,}", "\n\n", text)
                try:
                    await bot.send_message(m["user_id"], text, reply_markup=_return_kb())
                except Exception:
                    logging.exception("Не удалось отправить письмо об обнулении времени в поле пользователю %s", m["user_id"])

    # "поведенческое" напоминание - человек смотрел информацию о Sanctum, но
    # не дошёл до "Инициировать шаг" (её явное решение 2026-09-17: тоньше и
    # уместнее общего "пришёл и пропал" - реагирует на конкретный интерес,
    # а не просто на календарную тишину)
    if (db.get_setting("sanctum_nudge_enabled") or "1") == "1":
        nudge_hours = int(db.get_setting("sanctum_nudge_hours") or "5")
        cutoff = (now - timedelta(hours=nudge_hours)).strftime("%Y-%m-%d %H:%M:%S")
        template = db.get_setting("sanctum_nudge_text") or ""
        nudge_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚜️ Вернуться к VEDA SANCTUM", callback_data="open_sanctum")]
        ])
        for user_row in db.get_sanctum_intro_viewers_due(cutoff):
            text = _personalize(template, user_row)
            try:
                await bot.send_message(user_row["user_id"], text, reply_markup=nudge_kb)
            except Exception:
                logging.exception(
                    "Не удалось отправить напоминание \"посмотрел, не начал\" пользователю %s", user_row["user_id"]
                )
            db.mark_sanctum_nudge_sent(user_row["user_id"])
    logging.info("[планировщик] check_reengagement: завершено")


# ---------- напоминания о вебинарах/практиках/расстановках ----------

# (задержка до начала, столбец-флажок "уже отправлено", ключ настройки с текстом,
# прикреплять ли кнопку "Подробнее о вебинаре" — только для ранних двух уровней)
WEBINAR_REMINDER_TIERS = [
    (timedelta(days=5), "reminder_5d_sent", "webinar_reminder_5d_text", True),
    (timedelta(hours=24), "reminder_24h_sent", "webinar_reminder_24h_text", True),
    (timedelta(hours=1), "reminder_1h_sent", "webinar_reminder_1h_text", False),
]


async def check_webinar_reminders(bot: Bot):
    logging.info("[планировщик] check_webinar_reminders: старт")
    now = datetime.now(TZ)
    cutoff = (now - timedelta(days=1)).isoformat()
    for w in db.get_webinars_with_upcoming_event(cutoff):
        event_dt = datetime.fromisoformat(w["event_dt"])
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=TZ)
        event_type_word = EVENT_TYPE_WORDS.get(w["event_type"] or "webinar", "вебинар")
        date_display = _format_event_dt(event_dt)

        for delta, sent_col, text_key, attach_card in WEBINAR_REMINDER_TIERS:
            if now < event_dt - delta:
                continue
            regs = db.get_confirmed_registrations_unsent(w["id"], sent_col)
            if not regs:
                continue
            template = db.get_setting(text_key) or ""
            kb = None
            if attach_card:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📄 Подробнее о вебинаре", callback_data=f"wb_view_{w['id']}")
                ]])
            link_block = f"🔗 {w['invite_link']}\n\n" if (not attach_card and w["invite_link"]) else ""
            base_text = (
                template
                .replace("{тип}", event_type_word)
                .replace("{название}", w["title"])
                .replace("{дата и время}", date_display)
                .replace("{ссылка}", link_block)
            )
            for reg in regs:
                user_row = db.get_user(reg["user_id"])
                text = _personalize(base_text, user_row)
                try:
                    await bot.send_message(reg["user_id"], text, reply_markup=kb)
                except Exception:
                    logging.exception(
                        "Не удалось отправить напоминание о вебинаре пользователю %s", reg["user_id"]
                    )
                db.mark_webinar_reminder_sent(reg["id"], sent_col)
    logging.info("[планировщик] check_webinar_reminders: завершено")


# ---------- админ-панель: намерения участников ----------

@router.callback_query(F.data == "adm_intentions_list")
async def adm_intentions_list(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_intentions_list"):
        return
    rows_data = db.get_all_intentions_for_admin()
    if not rows_data:
        text = "<b>🕯 Намерения участников</b>\n\nПока никто не написал намерение."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
        return

    lines = [
        "<b>🕯 Намерения участников</b>",
        "",
        "Напоминание приходит всем 8 и 22 числа. Отметьте «Разбор дан», когда лично разберёте намерение "
        "человека - кнопка «написать лично» перестанет приходить ему в напоминаниях (само напоминание "
        "останется). Если человек потом изменит текст намерения - отметка снимется сама.",
    ]
    rows = []
    for r in rows_data:
        name = f"@{r['username']}" if r["username"] else (r["preferred_name"] or r["first_name"] or str(r["user_id"]))
        status_icon = "✅" if r["intention_reviewed"] else "◻️"
        lines.append(f"\n{status_icon} <b>{html.escape(name)}</b>:\n«{html.escape(r['intention_text'])}»")
        toggle_label = "◻️ Снять отметку" if r["intention_reviewed"] else "✅ Разбор дан"
        rows.append([InlineKeyboardButton(
            text=f"{toggle_label} - {name}", callback_data=f"adm_intent_toggle_{r['user_id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_intent_toggle_"))
async def adm_intent_toggle(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_intentions_list"):
        return
    user_id = int(callback.data[len("adm_intent_toggle_"):])
    membership = db.get_sanctum_membership(user_id)
    currently_reviewed = bool(membership["intention_reviewed"]) if membership else False
    db.set_intention_reviewed(user_id, not currently_reviewed)
    await adm_intentions_list(callback)


# ---------- напоминание о намерении (ступень «Искра», 8 и 22 числа каждого месяца) ----------

async def check_intention_reminders(bot: Bot):
    """Работает по фиксированному для всех расписанию (8 и 22 число каждого
    месяца — см. day="8,22" в регистрации задачи в main()), а не по личному
    дню каждого человека. Кнопка "написать лично" пропадает после того, как
    она отметит разбор данным (intention_reviewed) — само напоминание при
    этом продолжает приходить, это самостоятельная практика, не ожидание
    ответа от неё."""
    logging.info("[планировщик] check_intention_reminders: старт")
    today = _today()
    for m in db.get_active_intentions():
        template = db.get_setting("ascension_intention_recall_text") or ""
        text = template.replace("{намерение}", m["intention_text"])
        rows = [[InlineKeyboardButton(text="✏️ Изменить намерение", callback_data="edit_intention")]]
        if not m["intention_reviewed"]:
            personal_link = db.get_setting("admin_personal_chat_link")
            if personal_link:
                rows.append([InlineKeyboardButton(text="💌 Написать Алёне лично", url=personal_link)])
        try:
            await bot.send_message(m["user_id"], text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            logging.exception("Не удалось отправить напоминание о намерении пользователю %s", m["user_id"])
        db.mark_intention_reminded(m["user_id"], today.isoformat())
    logging.info("[планировщик] check_intention_reminders: завершено")


@router.callback_query(F.data == "edit_intention")
async def edit_intention_start(callback: CallbackQuery, state: FSMContext):
    membership = db.get_sanctum_membership(callback.from_user.id)
    current = membership["intention_text"] if membership else None
    if not current:
        await callback.answer("У Вас пока нет записанного намерения.", show_alert=True)
        return
    await state.set_state(IntentionStates.waiting_text)
    await callback.message.answer(
        f"Сейчас записано:\n«{current}»\n\n"
        "Пришлите новый текст намерения полностью - он заменит прежний."
    )
    await callback.answer()


async def cleanup_feed_posts():
    """Тихая ежедневная уборка публикаций с истёкшим сроком хранения в архиве —
    без уведомлений, просто освобождает место, как и договаривались."""
    logging.info("[планировщик] cleanup_feed_posts: старт")
    removed = db.delete_expired_feed_posts(_today().isoformat())
    if removed:
        logging.info("Архив публикаций: удалено по истечении срока — %s", removed)
    logging.info("[планировщик] cleanup_feed_posts: завершено")


async def backup_database_job():
    """Ежедневная резервная копия базы — тихая, без уведомлений в чат,
    видна только в bot.log и в самой папке backups."""
    logging.info("[планировщик] backup_database: старт")
    try:
        path = db.backup_database()
        logging.info("[планировщик] backup_database: завершено, файл %s", path)
    except Exception:
        logging.exception("[планировщик] backup_database: упало с ошибкой")


@router.message(F.photo)
async def photo_fallback(message: Message):
    """Подстраховка на самом конце ВСЕХ обработчиков фото в файле - порядок
    регистрации здесь принципиален: этот обработчик не имеет фильтра по
    состоянию и потому обязан идти последним, иначе он перехватит фото,
    предназначенные другим сценариям (загрузка фото в панели, фото для
    рассылки и т.п.), которые должны сработать первыми.
    Реальный случай (2026-09-06): человек прислал чек об оплате, но
    состояние ожидания было потеряно (успел ещё раз нажать /start между
    оформлением заявки и отправкой чека - /start всегда очищает состояние),
    и фото просто пропало для бота молча, ни ей, ни ему не пришло никакого
    сообщения. Теперь при потере состояния бот всё равно находит
    незавершённую заявку по факту в базе и принимает чек как обычно."""
    reg = db.get_awaiting_receipt_for_user(message.from_user.id)
    if not reg:
        return
    await _process_receipt_photo(message, reg["id"])


# ---------- запуск ----------

# ---------- календарь ритуалов (участники Санктума) ----------
# Даты лежат в таблице ritual_events (см. rituals.py) и правятся в админ-панели;
# бот сам ничего не считает "на лету". Участник = действующий доступ в Санктум
# (для проверки экранов администраторы тоже видят полный вид).

def _ritual_is_member(user_id: int) -> bool:
    if db.is_admin(user_id):
        return True
    m = db.get_sanctum_membership(user_id)
    if not m or m["status"] == "removed" or not m["valid_until"]:
        return False
    valid_until = _parse_date(m["valid_until"])
    return bool(valid_until and valid_until >= _today())


def _ritual_calendar_btn(text: str = "🌙 Календарь ритуалов") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="rit_open")


def _ritual_kb(ym: str, user_row) -> InlineKeyboardMarkup:
    reminders_on = bool(user_row and user_row["ritual_reminders"])
    opted_out = bool(user_row and user_row["ritual_optout"])
    rows = []
    if ym != "x":
        rows.append([InlineKeyboardButton(text="📖 О практиках месяца", callback_data=f"rit_about_{ym}")])
    rows.append([InlineKeyboardButton(
        text="🔕 Не напоминать в день" if reminders_on else "🔔 Напоминать в день",
        callback_data=f"rit_remind_{ym}",
    )])
    rows.append([InlineKeyboardButton(
        text="🔔 Присылать календарь 1-го числа" if opted_out else "🔕 Больше не присылать календарь",
        callback_data=f"rit_optout_{ym}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _split_message(text: str, limit: int = 3900) -> list:
    """Делит длинный текст по абзацам, чтобы уложиться в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > limit:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


async def _send_long(bot, chat_id: int, text: str, reply_markup=None):
    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await bot.send_message(chat_id, chunk, reply_markup=reply_markup if last else None)


def _ritual_two_months():
    today = _today()
    first = today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first.strftime("%Y-%m"), nxt.strftime("%Y-%m")


@router.callback_query(F.data == "rit_open")
async def ritual_open(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _ritual_is_member(user_id):
        text = db.get_setting("ritual_teaser_text")
        # "sanctum_apply" - тот же самый обработчик, что и на экране законов
        # (кнопка "Инициировать шаг") - ведёт сразу к оплате, минуя манифест
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚜️ Что такое VEDA SANCTUM", callback_data="open_sanctum")],
            [InlineKeyboardButton(text="Оформить подписку", callback_data="sanctum_apply")],
        ])
        await callback.message.answer(text, reply_markup=kb, protect_content=_protect_for(user_id))
        await callback.answer()
        return
    ym_now, ym_next = _ritual_two_months()
    blocks = ["🌙 <b>Календарь ритуалов</b>"]
    for ym in (ym_now, ym_next):
        # в текущем месяце уже прошедшие даты не показываем
        evs = [e for e in rituals.events_of_month(ym) if e["event_date"] >= _today().isoformat()]
        body ="\n".join(rituals.fmt_line(e) for e in evs) if evs else "даты скоро появятся"
        blocks.append(f"<b>{rituals.month_title(ym)}</b>\n{body}")
    blocks.append("Время везде киевское.")
    user_row = db.get_user(user_id)
    await _send_long(callback.bot, callback.message.chat.id, "\n\n".join(blocks), _ritual_kb(ym_now, user_row))
    await callback.answer()


@router.callback_query(F.data.startswith("rit_about_"))
async def ritual_about(callback: CallbackQuery):
    if not _ritual_is_member(callback.from_user.id):
        await callback.answer("Этот раздел для участников VEDA SANCTUM", show_alert=True)
        return
    ym = callback.data[len("rit_about_"):]
    text = rituals.about_text(ym)
    if not text:
        await callback.answer("На этот месяц пока нет дат", show_alert=True)
        return
    await _send_long(callback.bot, callback.message.chat.id, text)
    await callback.answer()


@router.callback_query(F.data.startswith("rit_remind_"))
async def ritual_toggle_remind(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _ritual_is_member(user_id):
        await callback.answer("Этот раздел для участников VEDA SANCTUM", show_alert=True)
        return
    ym = callback.data[len("rit_remind_"):]
    user_row = db.get_user(user_id)
    new_value = 0 if (user_row and user_row["ritual_reminders"]) else 1
    rituals.set_user_flag(user_id, "ritual_reminders", new_value)
    try:
        await callback.message.edit_reply_markup(reply_markup=_ritual_kb(ym, db.get_user(user_id)))
    except Exception:
        pass
    await callback.answer(
        "Напоминания включены: в день события утром придёт сообщение 🔔" if new_value
        else "Напоминания выключены 🔕",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("rit_optout_"))
async def ritual_toggle_optout(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _ritual_is_member(user_id):
        await callback.answer("Этот раздел для участников VEDA SANCTUM", show_alert=True)
        return
    ym = callback.data[len("rit_optout_"):]
    user_row = db.get_user(user_id)
    new_value = 0 if (user_row and user_row["ritual_optout"]) else 1
    rituals.set_user_flag(user_id, "ritual_optout", new_value)
    try:
        await callback.message.edit_reply_markup(reply_markup=_ritual_kb(ym, db.get_user(user_id)))
    except Exception:
        pass
    await callback.answer(
        "Хорошо, календарь 1-го числа больше не пришлю. Открыть его всегда можно в «Инфо» и в профиле."
        if new_value else "Календарь снова будет приходить 1-го числа каждого месяца ✅",
        show_alert=True,
    )


def _user_first_name_for_text(u) -> str:
    return ((u["preferred_name"] or u["first_name"] or "") if u else "").strip()


async def send_ritual_monthly(bot: Bot):
    """1-го числа каждого месяца в 11:11 по Киеву: календарь на месяц всем
    действующим участникам Санктума, кроме отказавшихся. ritual_last_month
    защищает от повторной отправки, если задача сработает дважды."""
    logging.info("[планировщик] send_ritual_monthly: старт")
    ym = _today().strftime("%Y-%m")
    if not rituals.events_of_month(ym):
        logging.warning("Календарь ритуалов: на %s нет ни одной даты - рассылка пропущена", ym)
        return
    sent = 0
    for u in rituals.active_members():
        if u["ritual_optout"] or u["ritual_last_month"] == ym:
            continue
        text = rituals.month_text(ym, _user_first_name_for_text(u))
        try:
            await _send_long(bot, u["user_id"], text, _ritual_kb(ym, u))
            rituals.set_user_flag(u["user_id"], "ritual_last_month", ym)
            sent += 1
        except Exception:
            logging.exception("Не удалось отправить календарь ритуалов пользователю %s", u["user_id"])
        await asyncio.sleep(0.05)
    logging.info("[планировщик] send_ritual_monthly: завершено, отправлено %s", sent)


async def send_ritual_daily(bot: Bot):
    """Каждое утро: тем, кто включил «Напоминать в день», если сегодня в
    календаре есть событие."""
    logging.info("[планировщик] send_ritual_daily: старт")
    today_iso = _today().isoformat()
    evs = rituals.events_on(today_iso)
    if not evs:
        return
    blocks = []
    for e in evs:
        emoji = rituals.KINDS.get(e["kind"], ("•", ""))[0]
        title = e["title"] + (f" ({e['detail']})" if e["detail"] else "")
        block = f"{emoji} <b>{title}</b>\n{rituals.meaning_of(e)}"
        practice = rituals.practice_of(e)
        if practice:
            block += f"\n\n{practice}"
        blocks.append(block)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_ritual_calendar_btn()]])
    sent = 0
    for u in rituals.active_members():
        if not u["ritual_reminders"] or u["ritual_last_day"] == today_iso:
            continue
        header = (db.get_setting("ritual_day_text") or "").replace("{имя}", _user_first_name_for_text(u) or "друг")
        try:
            await _send_long(bot, u["user_id"], header + "\n\n" + "\n\n".join(blocks), kb)
            rituals.set_user_flag(u["user_id"], "ritual_last_day", today_iso)
            sent += 1
        except Exception:
            logging.exception("Не удалось отправить напоминание календаря пользователю %s", u["user_id"])
        await asyncio.sleep(0.05)
    logging.info("[планировщик] send_ritual_daily: завершено, отправлено %s", sent)


# ---------- календарь ритуалов: админ-панель ----------

RITUAL_ADMIN_BACK = [InlineKeyboardButton(text="⬅️ Календарь ритуалов", callback_data="adm_rituals")]


def _ritual_admin_screen():
    st = rituals.stats()
    months = rituals.months_with_counts()
    total = sum(m["n"] for m in months)
    span = f"{months[0]['ym']} ... {months[-1]['ym']}" if months else "нет"
    upcoming = rituals.events_between(_today().isoformat(), "2100-01-01")
    nxt = rituals.fmt_line(upcoming[0], with_year=True) if upcoming else "нет"
    announced = db.get_setting("ritual_announced_at") or "ещё не отправляли"
    text = (
        "🌙 <b>Календарь ритуалов</b>\n\n"
        f"Дат в таблице: {total} (период: {span})\n"
        f"Ближайшая: {nxt}\n\n"
        f"Участников Санктума сейчас: {st['members']}\n"
        f"Включили «напоминать в день»: {st['reminders']}\n"
        f"Отказались от ежемесячного календаря: {st['optout']}\n"
        f"Анонс участникам: {announced}\n\n"
        "Ежемесячное сообщение уходит 1-го числа в 11:11 по Киеву, напоминания в день события - в 09:00. "
        "Даты бот не считает сам: он берёт их из этой таблицы, поэтому любую можно поправить здесь."
    )
    if months and months[-1]["ym"] < (_today() + timedelta(days=120)).strftime("%Y-%m"):
        text += "\n\n⚠️ Даты заканчиваются меньше чем через 4 месяца, пора вносить новые."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Даты по месяцам", callback_data="adm_rit_months")],
        [InlineKeyboardButton(text="➕ Добавить дату", callback_data="adm_rit_add")],
        [InlineKeyboardButton(text="✏️ Тексты календаря", callback_data="adm_rit_texts")],
        [InlineKeyboardButton(text="📨 Прислать мне пробное сообщение", callback_data="adm_rit_test")],
        [InlineKeyboardButton(text="📣 Анонс календаря участникам", callback_data="adm_rit_announce")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])
    return text, kb


@router.callback_query(F.data == "adm_rituals")
async def adm_rituals(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    text, kb = _ritual_admin_screen()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm_rit_months")
async def adm_rit_months(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    rows = [[InlineKeyboardButton(
        text=f"{rituals.month_title(m['ym']).capitalize()} - {m['n']}", callback_data=f"adm_rit_m_{m['ym']}"
    )] for m in rituals.months_with_counts()]
    rows.append(RITUAL_ADMIN_BACK)
    await callback.message.edit_text("Выберите месяц:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rit_m_"))
async def adm_rit_month(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    ym = callback.data[len("adm_rit_m_"):]
    rows = []
    for e in rituals.events_of_month(ym):
        d = datetime.strptime(e["event_date"], "%Y-%m-%d").strftime("%d.%m")
        emoji = rituals.KINDS.get(e["kind"], ("•", ""))[0]
        mark = " ✍️" if rituals.practice_of(e) else ""
        rows.append([InlineKeyboardButton(text=f"{emoji} {d} {e['title']}{mark}", callback_data=f"adm_rit_e_{e['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ К месяцам", callback_data="adm_rit_months")])
    await callback.message.edit_text(
        f"<b>{rituals.month_title(ym).capitalize()}</b>\nНажмите на дату, чтобы открыть или изменить. "
        "✍️ - у даты есть Ваша практика.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


def _ritual_event_screen(event_id: int):
    e = rituals.get_event(event_id)
    if not e:
        return "Такой даты уже нет.", InlineKeyboardMarkup(inline_keyboard=[RITUAL_ADMIN_BACK])
    d = datetime.strptime(e["event_date"], "%Y-%m-%d").strftime("%d.%m.%Y")
    kind_name = rituals.KINDS.get(e["kind"], ("", e["kind"]))[1]
    own_practice = (e["practice"] or "").strip()
    general_practice = (db.get_setting(f"ritual_practice_{e['kind']}") or "").strip()
    text = (
        f"{rituals.KINDS.get(e['kind'], ('•', ''))[0]} <b>{e['title']}</b>\n"
        f"Тип: {kind_name}\nДата: {d}\n"
        f"Уточнение: {e['detail'] or '(нет)'}\n\n"
        f"<b>Смысл:</b> {rituals.meaning_of(e) or '(нет)'}"
        f"{'' if e['meaning'] else ' (общий для типа)'}\n\n"
        f"<b>Практика для этой даты:</b> {own_practice or '(не задана - используется общая для типа, если есть)'}\n"
        f"<b>Общая практика для «{kind_name}»:</b> {general_practice or '(не задана)'}\n"
        f"<b>В сообщениях сейчас покажется:</b> {rituals.practice_of(e) or '(ничего - практика нигде не задана)'}"
    )
    ym = e["event_date"][:7]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Дату", callback_data=f"adm_rit_ef_{event_id}_event_date"),
         InlineKeyboardButton(text="✏️ Название", callback_data=f"adm_rit_ef_{event_id}_title")],
        [InlineKeyboardButton(text="✏️ Уточнение", callback_data=f"adm_rit_ef_{event_id}_detail"),
         InlineKeyboardButton(text="✏️ Смысл", callback_data=f"adm_rit_ef_{event_id}_meaning")],
        [InlineKeyboardButton(text="✍️ Практика", callback_data=f"adm_rit_ef_{event_id}_practice")],
        [InlineKeyboardButton(text="🗑 Удалить дату", callback_data=f"adm_rit_del_{event_id}")],
        [InlineKeyboardButton(text="⬅️ К месяцу", callback_data=f"adm_rit_m_{ym}")],
    ])
    return text, kb


@router.callback_query(F.data.startswith("adm_rit_e_"))
async def adm_rit_event(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    text, kb = _ritual_event_screen(int(callback.data[len("adm_rit_e_"):]))
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


RITUAL_FIELD_PROMPTS = {
    "event_date": "Пришлите новую дату в формате ДД.ММ.ГГГГ (например, 25.10.2026):",
    "title": "Пришлите новое название (например, «Экадаши Рама»):",
    "detail": "Пришлите уточнение в скобках (например, «новолуние 09.11 в 09:02» или «у вайшнавов: 19.01»). "
              "Отправьте «-», чтобы убрать:",
    "meaning": "Пришлите короткий смысл именно этого дня. Отправьте «-», чтобы вернуть общий смысл типа:",
    "practice": "Пришлите практику именно для ЭТОЙ даты - она заменит собой общую практику для этого типа "
                "события (если она есть). Отправьте «-», чтобы убрать и снова показывать общую:",
}


@router.callback_query(F.data.startswith("adm_rit_ef_"))
async def adm_rit_edit_field(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_rituals"):
        return
    rest = callback.data[len("adm_rit_ef_"):]
    event_id_s, field = rest.split("_", 1)
    if field not in rituals.EVENT_FIELDS or not rituals.get_event(int(event_id_s)):
        await callback.answer("Не получилось открыть", show_alert=True)
        return
    e = rituals.get_event(int(event_id_s))
    current = e[field] if field != "event_date" else datetime.strptime(e[field], "%Y-%m-%d").strftime("%d.%m.%Y")
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="ritual_event", field=field, target_id=int(event_id_s))
    await callback.message.answer(
        f"Сейчас: {current or '(пусто)'}\n\n{RITUAL_FIELD_PROMPTS[field]}\n\n(или /cancel)",
        parse_mode=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rit_del_"))
async def adm_rit_delete_ask(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    event_id = int(callback.data[len("adm_rit_del_"):])
    e = rituals.get_event(event_id)
    if not e:
        await callback.answer("Уже удалено", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"adm_rit_delok_{event_id}")],
        [InlineKeyboardButton(text="⬅️ Нет, назад", callback_data=f"adm_rit_e_{event_id}")],
    ])
    await callback.message.edit_text(f"Удалить дату «{e['title']}» ({e['event_date']})?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rit_delok_"))
async def adm_rit_delete_do(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    event_id = int(callback.data[len("adm_rit_delok_"):])
    e = rituals.get_event(event_id)
    ym = e["event_date"][:7] if e else None
    rituals.delete_event(event_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К месяцу", callback_data=f"adm_rit_m_{ym}")] if ym else RITUAL_ADMIN_BACK
    ])
    await callback.message.edit_text("Дата удалена ✅", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm_rit_add")
async def adm_rit_add(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    rows = [[InlineKeyboardButton(text=f"{emoji} {name}", callback_data=f"adm_rit_addk_{kind}")]
            for kind, (emoji, name) in rituals.KINDS.items()]
    rows.append(RITUAL_ADMIN_BACK)
    await callback.message.edit_text("Какого типа новая дата?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rit_addk_"))
async def adm_rit_add_kind(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_rituals"):
        return
    kind = callback.data[len("adm_rit_addk_"):]
    if kind not in rituals.KINDS:
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="ritual_add", field="date", kind=kind)
    await callback.message.answer(
        "Пришлите дату в формате ДД.ММ.ГГГГ (например, 25.10.2026)\n\n(или /cancel)", parse_mode=None
    )
    await callback.answer()


async def _ritual_edit_value(message: Message, state: FSMContext, data: dict, target: str, field, value: str) -> bool:
    """Ввод значений для календаря ритуалов из общего обработчика edit_field_value.
    Возвращает True, если диалог продолжается (состояние сбрасывать нельзя)."""
    if target == "ritual_text":
        rituals_text_key = field
        db.set_setting(rituals_text_key, value)
        reply = f"Текст «{rituals.TEXT_LABELS.get(field, field)}» обновлён ✅"
        for ph in ("{имя}", "{месяц}"):
            word = ph.strip("{}")
            if ph not in value and f"({word})" in value:
                reply += f"\n\n⚠️ Вместо {ph} (фигурные скобки) написано ({word}) - оно не заменится."
        await message.answer(reply, parse_mode=None)
        return False

    if target == "ritual_event":
        event_id = data["target_id"]
        raw = value.strip()
        if field == "event_date":
            try:
                new_date = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
            except ValueError:
                await message.answer("Не получилось распознать дату. Формат: ДД.ММ.ГГГГ. Попробуйте ещё раз:")
                return True
            rituals.update_event(event_id, "event_date", new_date)
        elif field == "title":
            if not raw or raw == "-":
                await message.answer("Название не может быть пустым. Пришлите текст:")
                return True
            rituals.update_event(event_id, "title", html.escape(raw))
        elif field == "detail":
            rituals.update_event(event_id, "detail", "" if raw == "-" else html.escape(raw))
        else:  # meaning / practice: доверенный HTML (жирный и т.п. сохраняется)
            rituals.update_event(event_id, field, "" if raw == "-" else value)
        text, kb = _ritual_event_screen(event_id)
        await message.answer("Обновлено ✅\n\n" + text, reply_markup=kb)
        return False

    if target == "ritual_add":
        raw = value.strip()
        if field == "date":
            try:
                new_date = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
            except ValueError:
                await message.answer("Не получилось распознать дату. Формат: ДД.ММ.ГГГГ. Попробуйте ещё раз:")
                return True
            await state.update_data(field="title", event_date=new_date)
            default_title = rituals.KINDS[data["kind"]][1]
            await message.answer(
                f"Теперь название (например, «{default_title}»). Отправьте «-», чтобы взять «{default_title}»:",
                parse_mode=None,
            )
            return True
        title = rituals.KINDS[data["kind"]][1] if raw in ("", "-") else html.escape(raw)
        new_id = rituals.add_event(data["event_date"], data["kind"], title)
        text, kb = _ritual_event_screen(new_id)
        await message.answer("Дата добавлена ✅ Уточнение, смысл и практику можно дописать ниже.\n\n" + text, reply_markup=kb)
        return False
    return False


@router.callback_query(F.data == "adm_rit_texts")
async def adm_rit_texts(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    rows = [[InlineKeyboardButton(text=f"✏️ {label[:48]}", callback_data=f"adm_rit_t_{key}")]
            for key, label in rituals.TEXT_LABELS.items()]
    rows.append(RITUAL_ADMIN_BACK)
    await callback.message.edit_text(
        "Какой текст изменить? (В смыслах и практиках можно использовать жирный шрифт.)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rit_t_"))
async def adm_rit_text_edit(callback: CallbackQuery, state: FSMContext):
    if not await _require_permission(callback, "adm_rituals"):
        return
    key = callback.data[len("adm_rit_t_"):]
    if key not in rituals.TEXT_LABELS:
        await callback.answer("Не получилось открыть", show_alert=True)
        return
    await state.set_state(EditFieldStates.waiting_value)
    await state.update_data(target="ritual_text", field=key)
    await callback.message.answer(
        f"Сейчас:\n\n{db.get_setting(key)}\n\nПришлите новый текст: {rituals.TEXT_LABELS[key]}.\n"
        "(или /cancel)"
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rit_test")
async def adm_rit_test(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    ym = _today().strftime("%Y-%m")
    text = rituals.month_text(ym, _user_first_name_for_text(db.get_user(callback.from_user.id)))
    if not text:
        await callback.answer("На текущий месяц нет дат", show_alert=True)
        return
    await _send_long(callback.bot, callback.from_user.id, text, _ritual_kb(ym, db.get_user(callback.from_user.id)))
    await callback.answer("Пробное сообщение отправлено Вам ✅")


@router.callback_query(F.data == "adm_rit_announce")
async def adm_rit_announce(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    count = len(rituals.active_members())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить {count} участникам", callback_data="adm_rit_announce_go")],
        RITUAL_ADMIN_BACK,
    ])
    already = db.get_setting("ritual_announced_at")
    note = f"\n\n⚠️ Анонс уже отправляли ({already}). Повторно отправлять не нужно." if already else ""
    await callback.message.edit_text(
        f"Разовый анонс придёт всем участникам Санктума с действующим доступом ({count} чел.).\n\n"
        f"Текст (метку {{имя}} бот заменит именем):\n\n{db.get_setting('ritual_announce_text')}{note}",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rit_announce_go")
async def adm_rit_announce_go(callback: CallbackQuery):
    if not await _require_permission(callback, "adm_rituals"):
        return
    template = db.get_setting("ritual_announce_text") or ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[_ritual_calendar_btn("🌙 Открыть календарь ритуалов")]])
    await callback.answer("Отправляю...")
    sent = failed = 0
    for u in rituals.active_members():
        text = template.replace("{имя}", _user_first_name_for_text(u) or "друг")
        try:
            await _send_long(callback.bot, u["user_id"], text, kb)
            sent += 1
        except Exception:
            failed += 1
            logging.exception("Не удалось отправить анонс календаря пользователю %s", u["user_id"])
        await asyncio.sleep(0.05)
    db.set_setting("ritual_announced_at", _today().strftime("%d.%m.%Y"))
    await callback.message.edit_text(
        f"Анонс отправлен: {sent} чел." + (f", не дошло: {failed}." if failed else "."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[RITUAL_ADMIN_BACK]),
    )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    db.init_db()

    # догоняющая резервная копия при старте — обнаружено 2026-09-03, что
    # суточная задача в 03:00 (ниже) почти никогда не попадает в своё окно при
    # частых перезапусках бота (обычное дело во время активной разработки) и
    # молча пропускается планировщиком; копий не было НИ РАЗУ за две недели,
    # кроме ручных. Здесь — подстраховка независимо от расписания: если самой
    # свежей копии больше суток (или их вообще ещё не было), создаём прямо сейчас
    last_backup_age = db.get_latest_backup_age_hours()
    if last_backup_age is None or last_backup_age > 24:
        try:
            path = db.backup_database()
            logging.info("Догоняющая резервная копия при старте бота: %s", path)
        except Exception:
            logging.exception("Не удалось создать догоняющую резервную копию при старте")

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=fsm_storage)
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(check_sanctum_reminders, "cron", hour=config.SANCTUM_REMINDER_HOUR, args=[bot])
    scheduler.add_job(check_reengagement, "cron", hour=config.SANCTUM_REMINDER_HOUR, minute=30, args=[bot])
    scheduler.add_job(check_webinar_reminders, "interval", minutes=15, args=[bot])
    scheduler.add_job(check_intention_reminders, "cron", day="8,22", hour=11, args=[bot])
    scheduler.add_job(cleanup_feed_posts, "cron", hour=config.SANCTUM_REMINDER_HOUR, minute=45)
    # календарь ритуалов: 1-го числа в 11:11 по Киеву - календарь на месяц; каждое
    # утро в 09:00 - напоминания тем, кто включил "Напоминать в день"
    scheduler.add_job(send_ritual_monthly, "cron", day=1, hour=11, minute=11, args=[bot],
                      misfire_grace_time=6 * 3600)
    scheduler.add_job(send_ritual_daily, "cron", hour=9, minute=0, args=[bot], misfire_grace_time=3 * 3600)
    # misfire_grace_time увеличен (по умолчанию у APScheduler он мал) — если
    # окно 03:00 всё-таки пропущено, задача ещё догонит себя сама в течение
    # нескольких часов, а не будет молча пропущена планировщиком совсем
    scheduler.add_job(backup_database_job, "cron", hour=3, misfire_grace_time=6 * 3600)
    scheduler.start()

    logging.info("Bot started!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
