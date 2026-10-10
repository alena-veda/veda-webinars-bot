import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, INITIAL_ADMIN_IDS

# Заглушки для незаполненных полей. Вынесены в константы, чтобы main.py мог
# сверяться с ними и НЕ показывать эти технические тексты обычным пользователям
# (только вам, пока вы не заполнили реальные данные в админ-панели).
PLACEHOLDER_SANCTUM_INTRO = "Текст-приглашение пока не заполнен. Измените его в админ-панели."
PLACEHOLDER_SANCTUM_LAWS = "Текст с законами храма пока не заполнен. Измените его в админ-панели."
PLACEHOLDER_PRICE = "0"
PLACEHOLDER_PAYMENT_REQUISITES = "Реквизиты для оплаты пока не заполнены. Измените их в админ-панели."
PLACEHOLDER_PAYMENT_PURPOSE = "Назначение платежа пока не заполнено. Измените его в админ-панели."


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- постоянное хранилище состояний диалога (FSM) ----------
# Обнаружено 2026-09-06: стандартное MemoryStorage у aiogram хранит состояние
# "на середине диалога" (пишет намерение, редактирует текст в панели,
# оформляет оплату) только в оперативной памяти процесса - при КАЖДОМ
# перезапуске бота (а во время активной разработки это происходит часто)
# состояние всех людей одновременно стиралось. Следующее сообщение человека
# после такого перезапуска не попадало ни в один обработчик и терялось молча
# (реальный случай: попытка изменить намерение). Здесь то же самое, но
# сохраняется в той же базе данных - переживает любой перезапуск бота.

def fsm_get_state(storage_key):
    conn = get_conn()
    row = conn.execute("SELECT state FROM fsm_storage WHERE storage_key = ?", (storage_key,)).fetchone()
    conn.close()
    return row["state"] if row else None


def fsm_set_state(storage_key, state):
    conn = get_conn()
    conn.execute(
        "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, ?, '{}') "
        "ON CONFLICT(storage_key) DO UPDATE SET state = excluded.state",
        (storage_key, state),
    )
    conn.commit()
    conn.close()


def fsm_get_data(storage_key):
    conn = get_conn()
    row = conn.execute("SELECT data FROM fsm_storage WHERE storage_key = ?", (storage_key,)).fetchone()
    conn.close()
    if not row or not row["data"]:
        return {}
    return json.loads(row["data"])


def fsm_set_data(storage_key, data):
    conn = get_conn()
    payload = json.dumps(data, ensure_ascii=False)
    conn.execute(
        "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, NULL, ?) "
        "ON CONFLICT(storage_key) DO UPDATE SET data = excluded.data",
        (storage_key, payload),
    )
    conn.commit()
    conn.close()


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT,
            blocked INTEGER DEFAULT 0,
            preferred_name TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN preferred_name TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # отмечает, что человеку уже один раз отправили напоминание "пришёл и
        # пропал" — чтобы не слать его повторно каждый день
        c.execute("ALTER TABLE users ADD COLUMN reengage_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # когда человек в последний раз открывал экран VEDA SANCTUM, ещё ни разу
        # не начав оформление (см. show_sanctum/sanctum_apply, main.py) — основа
        # для "поведенческого" напоминания check_reengagement: если посмотрел,
        # но не нажал "Инициировать шаг" за настроенный срок. Сбрасывается в
        # NULL, как только человек реально начинает оформление (sanctum_apply) —
        # напоминание в этот момент уже не нужно.
        c.execute("ALTER TABLE users ADD COLUMN sanctum_intro_viewed_at TEXT")
    except Exception:
        pass
    try:
        # отмечает, что "поведенческое" напоминание уже отправлено один раз —
        # больше не отправляем и не считаем такого человека "полностью тихим"
        # для общего напоминания "пришёл и пропал" (получил нацеленное вместо
        # общего, а не оба сразу)
        c.execute("ALTER TABLE users ADD COLUMN sanctum_nudge_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # кто пригласил этого человека (реферальная ссылка вида ?start=ref_<id>) —
        # заполняется один раз, только при самом первом /start, никогда не
        # переписывается later (INSERT OR IGNORE в add_user это гарантирует)
        c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    except Exception:
        pass
    try:
        # последнее действие человека в боте (человеческим языком) и когда оно
        # было - для уведомлений о новичках и профиля (2026-10-10)
        c.execute("ALTER TABLE users ADD COLUMN last_action TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_action_at TEXT")
    except Exception:
        pass
    try:
        # когда человеку отправили вводное сообщение «Понятно. Продолжить» и отправляли ли
        # ему напоминание, если он не нажал (2026-10-10)
        c.execute("ALTER TABLE users ADD COLUMN intro_sent_at TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN intro_nudge_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # когда человек впервые открыл экран VEDA SANCTUM (не сбрасывается, нужен для отчёта «Путь новичков»)
        c.execute("ALTER TABLE users ADD COLUMN sanctum_first_opened_at TEXT")
    except Exception:
        pass
    try:
        # сколько людей, приглашённых ЭТИМ человеком, реально вошли в VEDA SANCTUM
        # (оплатили первый раз) — основа для Ордена Люминаров, см. main.py
        c.execute("ALTER TABLE users ADD COLUMN luminar_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # отметка "купил VEDA HEALING FLOW" — этот бот не видит оплаты в том,
        # отдельном боте, поэтому это будущая РУЧНАЯ отметка (столбец создан
        # заранее, кнопка в админ-панели пока не построена — ждём её решения)
        c.execute("ALTER TABLE users ADD COLUMN bought_meditation_bot INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # человек сам заблокировал бота или вышел из чата (Telegram сообщает об
        # этом отдельным событием my_chat_member) — отдельно от "blocked", которым
        # помечаются ТОЛЬКО те, кого заблокировали вы сами; сбрасывается обратно
        # в 0 автоматически, если человек снова напишет боту
        c.execute("ALTER TABLE users ADD COLUMN self_departed INTEGER DEFAULT 0")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            admin_id INTEGER PRIMARY KEY,
            added_at TEXT,
            is_owner INTEGER DEFAULT 0
        )
    """)
    try:
        # владелец (Вы) видит и может всё без ограничений; остальные администраторы
        # (помощники) — только то, что им явно разрешено через admin_permissions
        c.execute("ALTER TABLE admins ADD COLUMN is_owner INTEGER DEFAULT 0")
    except Exception:
        pass
    # начальные админы из config.py (первый запуск бота) — всегда владельцы,
    # никогда не ограничиваются правами, даже если строка уже существовала
    # до этой миграции (тогда is_owner мог остаться 0 по умолчанию)
    for _oid in INITIAL_ADMIN_IDS:
        c.execute("UPDATE admins SET is_owner = 1 WHERE admin_id = ?", (_oid,))

    c.execute("""
        CREATE TABLE IF NOT EXISTS admin_permissions (
            admin_id INTEGER,
            permission_key TEXT,
            PRIMARY KEY (admin_id, permission_key)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS webinars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            date_text TEXT,
            price TEXT,
            invite_link TEXT,
            photo TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE webinars ADD COLUMN photo TEXT")
    except Exception:
        pass
    try:
        # разрешён ли под этим вебинаром вопрос от людей — включается отдельно
        # для каждого вебинара, а не глобально
        c.execute("ALTER TABLE webinars ADD COLUMN allow_questions INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # тип записи — вебинар/практика/расстановка, нужен для правильного слова
        # в автоматических напоминаниях ("открывается {тип} «{название}»")
        c.execute("ALTER TABLE webinars ADD COLUMN event_type TEXT DEFAULT 'webinar'")
    except Exception:
        pass
    try:
        # настоящие дата+время (в отличие от date_text, который только текст для
        # показа) — на них считаются автоматические напоминания за 5 дней/24 часа/
        # 1 час; заполняется и пересчитывается вместе с date_text одной функцией
        # (update_webinar_datetime), чтобы они никогда не расходились между собой
        c.execute("ALTER TABLE webinars ADD COLUMN event_dt TEXT")
    except Exception:
        pass
    try:
        # ссылка на запись (YouTube и т.п.) — отдельно от invite_link (та ссылка
        # для подключения ДО события, теряет смысл после); заполняется отдельно,
        # обычно не сразу, а когда видео будет готово и выложено. Показывается
        # в "Прошедшие" тем, кто оплатил именно этот вебинар, и всем действующим
        # участникам VEDA SANCTUM (её решение 2026-09-23: доступ к записям —
        # реальная выгода VEDA SANCTUM, а не просто уведомление)
        c.execute("ALTER TABLE webinars ADD COLUMN video_link TEXT")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS webinar_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webinar_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT
        )
    """)
    try:
        # отзыв картинкой (скриншот переписки и т.п.) — file_id фото в Telegram;
        # text при этом может быть пустой строкой (подписи нет), но не NULL —
        # так же, как в feed_posts, столбец остаётся NOT NULL
        c.execute("ALTER TABLE webinar_reviews ADD COLUMN photo TEXT")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS sanctum (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            price TEXT,
            invite_link TEXT,
            intro_text TEXT,
            laws_text TEXT,
            intro_photo TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE sanctum ADD COLUMN intro_text TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum ADD COLUMN laws_text TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum ADD COLUMN intro_photo TEXT")
    except Exception:
        pass
    try:
        # старое поле из первой версии экрана Санктума, давно заменено
        # intro_text/laws_text и нигде в коде больше не читается
        c.execute("ALTER TABLE sanctum DROP COLUMN description")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_type TEXT,
            product_id INTEGER,
            product_title TEXT,
            price TEXT,
            status TEXT,
            receipt_file_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    try:
        # отмечает, что человеку уже отправили напоминание "не забудьте прислать
        # чек" по этой заявке — чтобы не слать его повторно каждый день
        c.execute("ALTER TABLE registrations ADD COLUMN stall_reminder_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    for _col in ("reminder_5d_sent", "reminder_24h_sent", "reminder_1h_sent"):
        try:
            # отмечает, что этому конкретному подтверждённому участнику уже
            # отправили именно этот уровень напоминания о вебинаре — чтобы не
            # присылать один и тот же уровень повторно при каждом проходе планировщика
            c.execute(f"ALTER TABLE registrations ADD COLUMN {_col} INTEGER DEFAULT 0")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS feed_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT,
            text TEXT,
            file_id TEXT,
            file_ids_json TEXT,
            created_at TEXT,
            expires_at TEXT,
            allow_questions INTEGER DEFAULT 0
        )
    """)
    try:
        c.execute("ALTER TABLE feed_posts ADD COLUMN allow_questions INTEGER DEFAULT 0")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_type TEXT,
            ref_id INTEGER,
            ref_title TEXT,
            user_id INTEGER,
            question_text TEXT,
            answer_text TEXT,
            is_public INTEGER DEFAULT 0,
            created_at TEXT,
            answered_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS fsm_storage (
            storage_key TEXT PRIMARY KEY,
            state TEXT,
            data TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS faq_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            created_at TEXT,
            reviewed INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sanctum_membership (
            user_id INTEGER PRIMARY KEY,
            valid_until TEXT,
            price TEXT,
            status TEXT DEFAULT 'active',
            promise_date TEXT,
            promise_reminder_sent_for TEXT,
            reminder_3d_sent_for TEXT,
            reminder_0d_sent_for TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN price TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN status TEXT DEFAULT 'active'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN promise_date TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN promise_reminder_sent_for TEXT")
    except Exception:
        pass
    try:
        # ритуал-намерение при переходе на 2-ю ступень ("Искра") — текст,
        # который человек сам написал в ответ на приглашение, день месяца,
        # когда он это сделал (для ежемесячного напоминания в тот же день),
        # и дата последнего такого напоминания (дедуп — не чаще раза в месяц)
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN intention_text TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN intention_day INTEGER")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN intention_last_reminded TEXT")
    except Exception:
        pass
    try:
        # ставится вручную ею через "🕯 Намерения участников" после того, как
        # она лично дала человеку разбор его намерения — напоминание при этом
        # продолжает приходить (это самостоятельная практика, не "ожидание
        # ответа от неё"), но кнопка "написать лично" из него убирается, раз
        # разбор уже дан. Сбрасывается обратно в 0 при любом редактировании
        # намерения (см. set_sanctum_intention) — новый текст ещё не разбирали
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN intention_reviewed INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # суммарные дни присутствия в VEDA SANCTUM с самого первого входа —
        # растёт при каждом продлении (см. upsert_sanctum_membership), НЕ
        # обнуляется при перерыве (уже накопленное не теряется), основа для
        # ступеней "Путь Восхождения" (main.py, compute_ascension_level)
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN accumulated_days INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # возвратная схема для тех, кто был в Sanctum и не продлил (main.py,
        # check_reengagement) — чтобы не слать напоминание повторно одному и
        # тому же человеку за один и тот же случай истечения; сбрасывается
        # при новой оплате (см. upsert_sanctum_membership), чтобы при СЛЕДУЮЩЕМ
        # истечении можно было напомнить снова
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN winback_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # дата, когда её вручную убрали из Sanctum (от неё, а не от valid_until,
        # считаются срок сохранения цены и срок обнуления времени в поле у
        # тех, кого убрали вручную); снимается при новой оплате
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN removed_at TEXT")
        # для тех, кого убрали ДО появления этого поля - точная дата неизвестна,
        # считаем от дня обновления (честнее, чем от valid_until, который может
        # оказаться в будущем)
        c.execute(
            "UPDATE sanctum_membership SET removed_at = date('now') WHERE status = 'removed' AND removed_at IS NULL"
        )
    except Exception:
        pass
    try:
        # уже отправлено напоминание "последний шанс сохранить цену" за этот
        # случай ухода; сбрасывается при новой оплате
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN lastchance_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # время в поле уже обнулено за этот случай долгого отсутствия;
        # сбрасывается при новой оплате
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN stage_reset_done INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        # пожизненный бесплатный доступ (дар Люминара III, её решение 2026-09-25) -
        # только пометка ДЛЯ ОТОБРАЖЕНИЯ ("пожизненно" вместо даты); само "никогда
        # не истекает" обеспечивает valid_until в далёком будущем (см.
        # grant_lifetime_sanctum) - так все существующие проверки "активен ли
        # сейчас" (напоминания, календарь ритуалов, доступ к записям вебинаров)
        # срабатывают верно сами, без правки в каждом месте
        c.execute("ALTER TABLE sanctum_membership ADD COLUMN lifetime_free INTEGER DEFAULT 0")
    except Exception:
        pass

    for admin_id in INITIAL_ADMIN_IDS:
        c.execute(
            "INSERT OR IGNORE INTO admins (admin_id, added_at, is_owner) VALUES (?, ?, 1)",
            (admin_id, _now()),
        )

    c.execute("SELECT id FROM sanctum WHERE id = 1")
    if not c.fetchone():
        c.execute(
            "INSERT INTO sanctum (id, price, invite_link, intro_text, laws_text) "
            "VALUES (1, ?, ?, ?, ?)",
            (PLACEHOLDER_PRICE, "", PLACEHOLDER_SANCTUM_INTRO, PLACEHOLDER_SANCTUM_LAWS),
        )
    else:
        c.execute(
            "UPDATE sanctum SET intro_text = COALESCE(intro_text, ?), laws_text = COALESCE(laws_text, ?) WHERE id = 1",
            (PLACEHOLDER_SANCTUM_INTRO, PLACEHOLDER_SANCTUM_LAWS),
        )

    c.execute("SELECT value FROM settings WHERE key = 'payment_requisites'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('payment_requisites', ?)",
            (PLACEHOLDER_PAYMENT_REQUISITES,),
        )

    c.execute("SELECT value FROM settings WHERE key = 'payment_purpose_webinar'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('payment_purpose_webinar', ?)",
            (PLACEHOLDER_PAYMENT_PURPOSE,),
        )

    c.execute("SELECT value FROM settings WHERE key = 'payment_purpose_sanctum'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('payment_purpose_sanctum', ?)",
            (PLACEHOLDER_PAYMENT_PURPOSE,),
        )

    # ---------- автовозврат "потерянных" людей ----------
    # "1"/"0" вместо булева типа — так же, как везде в settings, значения только текстовые
    _default_settings = {
        "stall_enabled": "1",
        "stall_hours": "6",
        "stall_text": (
            "{имя}, Вы начали оформление «{product}», но чек об оплате пока не прислали мне.\n"
            "Если уже оплатили - пришлите, пожалуйста, скриншот сюда в пространство, и я подтвержу участие.\n"
            "Если раздумываете или что-то не получается - нажмите «💌 Личное обращение» ниже и напишите мне, "
            "я отвечу Вам прямо здесь же."
        ),
        "reengage_enabled": "1",
        "reengage_days_silent": "3",
        "reengage_text": (
            "{имя}, не тороплю - если пока не получилось заглянуть глубже, значит, ещё не время, "
            "и это нормально.\n"
            "Когда захочется отойти от суеты внешнего мира, выдохнуть и углубиться в себя с любовью "
            "к себе - в VEDA HEALING FLOW есть медитации именно для такого контакта.\n"
            "А когда почувствуете зов пойти глубже - Врата в святилище VEDA SANCTUM откроются Вам, "
            "когда будете готовы совершить эту инициацию."
        ),
        "winback_enabled": "1",
        "winback_days_after_expiry": "7",
        "winback_text": (
            "{имя}, Ваш доступ в VEDA SANCTUM | CODEofGOD закончился {дата}.\n\n"
            "Дверь остаётся открытой: до {дата_до} за Вами сохраняется Ваша прежняя цена - {цена}. "
            "После этой даты вход будет по новой стоимости, как для новых участников.\n\n"
            "Если Вам нужно немного времени - нажмите «Оплачу позже» и назовите дату оплаты "
            "(это важно отметить именно здесь, в боте, а не писать мне лично: так я буду знать, "
            "что Вы возвращаетесь).\n\n"
            "Буду рада видеть Вас снова! С уважением, Алёна ☀️"
        ),
        "price_lock_days": "30",
        "stage_reset_days": "90",
        "lastchance_days_before": "5",
        "sanctum_removed_text": (
            "{имя}, Ваш доступ в VEDA SANCTUM | CODEofGOD закончился {дата}.\n\n"
            "Дверь остаётся открытой: до {дата_до} за Вами сохраняется Ваша прежняя цена - {цена}. "
            "После этой даты вход будет по новой стоимости, как для новых участников.\n\n"
            "Буду рада видеть Вас снова! С уважением, Алёна ☀️"
        ),
        "sanctum_lastchance_text": (
            "{имя}, напоминаю: до {дата_до} Вы ещё можете вернуться в VEDA SANCTUM | CODEofGOD "
            "по своей прежней цене - {цена}. После этой даты вход будет по новой стоимости, "
            "как для новых участников.\n\n"
            "Если Вам нужно немного времени - нажмите «Оплачу позже» и назовите дату оплаты "
            "(это важно отметить именно здесь, в боте, а не писать мне лично: так я буду знать, "
            "что Вы возвращаетесь).\n\n"
            "С уважением, Алёна ☀️"
        ),
        "sanctum_reset_text": (
            "{имя}, Вас давно не было в поле VEDA SANCTUM, поэтому отсчёт времени в поле "
            "для ступеней Пути Восхождения начинается заново.\n"
            "{сохранено}\n"
            "Дверь в Sanctum открыта, когда Вы будете готовы. С уважением, Алёна ☀️"
        ),
        "sanctum_reset_luminar_line": "Ваш ранг Люминара остаётся с Вами ✨",
        "sanctum_reset_meditation_line": "Возможность продолжить работу с VEDA HEALING FLOW тоже остаётся с Вами.",
        "sanctum_nudge_enabled": "1",
        "sanctum_nudge_hours": "5",
        "sanctum_nudge_text": (
            "{имя}, Вы заглядывали в VEDA SANCTUM - хочу мягко напомнить о шаге дальше, "
            "если он всё ещё откликается.\n\n"
            "Врата открыты, когда будете готовы ✨"
        ),
    }
    # старая версия возвратного письма (до правил "цена сохраняется 30 дней") -
    # заменяем на новую, но только если она не была отредактирована вручную
    c.execute("SELECT value FROM settings WHERE key = 'winback_text'")
    _row = c.fetchone()
    if _row and _row[0] and "цена для Вас закреплена прежней" in _row[0]:
        c.execute("UPDATE settings SET value = ? WHERE key = 'winback_text'", (_default_settings["winback_text"],))
    for key, value in _default_settings.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    c.execute("SELECT value FROM settings WHERE key = 'sanctum_reminder_text_early'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('sanctum_reminder_text_early', ?)",
            (
                "⚜️ Ваш доступ в <b>VEDA SANCTUM | CODEofGOD</b> действует до {date}. "
                "Продлите заранее, дабы оставаться и двигаться в поле потоково.\n\n"
                "Ваш прайс подписки {price} остаётся с Вами - Закон Баланса хранит её, пока Вы в потоке.",
            ),
        )

    c.execute("SELECT value FROM settings WHERE key = 'sanctum_reminder_text_due'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('sanctum_reminder_text_due', ?)",
            (
                "⚜️ Сегодня заканчивается Ваш доступ в <b>VEDA SANCTUM | CODEofGOD.</b>\n"
                "Продлите, дабы оставаться в сакральном поле.\n\n"
                "Ваш прайс подписки {price} остаётся с Вами - Закон Баланса хранит её, пока Вы в потоке.",
            ),
        )

    c.execute("SELECT value FROM settings WHERE key = 'sanctum_promise_reminder_text'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('sanctum_promise_reminder_text', ?)",
            (
                "⏰ Напоминаем: Вы планировали оплатить {price} за VEDA SANCTUM завтра, {date}. "
                "Не забудьте 🙏",
            ),
        )

    c.execute("SELECT value FROM settings WHERE key = 'about_text'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('about_text', ?)",
            ("Текст «Обо мне» пока не заполнен. Измените его в админ-панели.",),
        )

    c.execute("SELECT value FROM settings WHERE key = 'welcome_text'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('welcome_text', ?)",
            (
                "💎Приветствую Вас в моём пространстве!\n\n"
                "Это место создано как точка входа к новым настройкам сознания, глубокому "
                "контакту с собой и своей истинной силой, к знаниям Источника.\n\n"
                "Что открывается для Вас внутри:\n\n"
                "✨ Анонсы вебинаров и практик - актуальное расписание и быстрая регистрация.\n"
                "✨ Тематические разборы - глубокие смыслы, ответы на запросы и работа с состоянием.\n"
                "✨ VEDA SANCTUM | CODEofGOD - доступ в мой закрытый сакральный канал.\n"
                "✨ VEDA HEALING FLOW - медитативный помощник с исцеляющими высокими частотами "
                "+ техниками от монахов, на каждый день с расширенной версией.\n"
                "✨ Философия Alena Veda - суть подхода, соединяющего Дух и Материю.\n"
                "✨ Путь Восхождения и Близкий круг - Ваша система проявления в моём поле. "
                "Проявляя активность, участвуя в пути и делясь этим пространством с окружающими, "
                "Вы повышаете свой уровень светимости в боте, открываете ценные дары от меня "
                "и приближаетесь к самому близкому закрытому кругу моего поля.\n\n"
                "~~~~~~~~~~~~~~~~~~\n"
                "Выбирайте интересующие Вас разделы в меню ниже ⤵️",
            ),
        )

    c.execute("SELECT value FROM settings WHERE key = 'meditation_text'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('meditation_text', ?)",
            (
                "🧘🏽‍♀️ <b>VEDA HEALING FLOW</b>\n\n"
                "Мой медитативный помощник с высокочастотными настройками и исцеляющими "
                "медитациями - для тела, ума и души.",
            ),
        )

    c.execute("SELECT value FROM settings WHERE key = 'meditation_coming_soon_text'")
    if not c.fetchone():
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('meditation_coming_soon_text', ?)",
            (
                "🌙 Портал VEDA HEALING FLOW настраивается прямо сейчас - совсем скоро здесь "
                "откроется доступ к Вашему квантовому помощнику. Загляните снова совсем скоро 🙏",
            ),
        )

    # ---------- напоминания о вебинарах/практиках/расстановках ----------
    _webinar_reminder_defaults = {
        "webinar_reminder_5d_text": (
            "{имя}, через 5 дней открывается {тип} «{название}» ✨\n\n"
            "Если чувствуете, что эта тема кому-то из Вашего окружения будет полезна к "
            "познанию, получению расширяющего знания от Алёны - нажмите кнопку ниже, "
            "откроется карточка с полной информацией, ею и удобно поделиться/переслать ☀️"
        ),
        "webinar_reminder_24h_text": (
            "{имя}, напоминаю Вам: уже завтра, {дата и время}, состоится {тип} «{название}». "
            "Если ещё не успели поделиться с теми, кому это может быть интересно - нажмите "
            "кнопку ниже, откроется карточка с полной информацией, её и удобно переслать ☀️"
        ),
        "webinar_reminder_1h_text": (
            "{имя}, начинаем через час - «{название}».\n\n"
            "{ссылка}"
            "До встречи! 💎"
        ),
        # уходит один раз, когда впервые заполняется ссылка на запись (video_link)
        # у конкретного вебинара - и тем, кто оплатил именно его, и всем действующим
        # участникам VEDA SANCTUM (см. adm_wb_video_notify_go, main.py)
        "webinar_video_ready_text": (
            "🎬 Готова запись «{тема}»:\n{ссылка}\n\nПриятного просмотра 🙏"
        ),
    }
    for key, value in _webinar_reminder_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- Путь Восхождения + Орден Люминаров: тексты (2026-08-31) ----------
    _ascension_defaults = {
        "ascension_level1_text": (
            "{имя}, приветствую Вас!\n"
            "Вы у Первого Касания - на пороге, у Врат самоисследования и сакральных открытий.\n"
            "Здесь Вы начинаете познавать себя глубже - многогранно и целостно.\n"
            "Осмотритесь.\n"
            "Я - Alena Veda. Архитектор Сознания.\n"
            "И моё пространство открыто перед Вами:\n"
            "⚜️ VEDA SANCTUM | CODEofGOD - мой закрытый канал живого знания\n"
            "📅 Вебинары и практики - глубокие встречи с мной.\n"
            "🧘 Медитации - квантовый помощник в работе с телом, сознанием и Душой, моим голосом и частотой\n"
            "Ваш шаг: войдите в Veda Sanctum. Пробыв в поле два месяца, Вы взойдёте на следующую ступень - "
            "и Вам откроются её дары.\n"
            "Впереди - ступень Искра."
        ),
        "ascension_level2_text": (
            "{имя}, Ваше Сердце отозвалось.\n"
            "Вы сделали первый шаг в святилище. Это уже не любопытство ума - это первое соприкосновение "
            "с моим полем.\n"
            "Оно начало свою тихую работу в Вас, даже если ум ещё не улавливает как.\n\n"
            "Ваш ритуал входа: напишите своё намерение - с чем Вы идёте дальше в этом поле.\n"
            "Не для меня, а для себя и квантового поля.\n"
            "В моём поле намерение от сердца обретает силу - поле его слышит.\n"
            "Мой дар Вам - глубокий разбор Вашего личного запроса в приоритете, согласно намерению.\n"
            "Напишите запрос мне лично.\n\n"
            "Ваш шаг дальше - выбором путей:\n"
            "• Пребывать в поле VEDA SANCTUM шесть месяцев.\n"
            "+ Пройти глубже через пространство медитаций VEDA HEALING FLOW\n"
            "ИЛИ\n"
            "+ Разделить этот свет и привести своих к Вратам - ключом Люминар I\n\n"
            "Впереди - ступень Исследователь Глубины."
        ),
        "ascension_level3_text": (
            "{имя}, Вы прошли достаточно, чтобы называться Исследователем Глубины.\n"
            "Вы больше не гость. Вы идёте, и глубина уже держит Вас.\n\n"
            "Мой дар Вам на этом пороге:\n"
            "Закрытая медитация-инициация, которой нет нигде. Она откроет в Вас новый уровень сознания - "
            "тот, с которого начинается путь на новую ступень - в Круг Силы.\n\n"
            "Что ещё открыто:\n"
            "• Особая цена на бота медитаций VEDA HEALING FLOW - на один месяц.\n"
            "• Доступ на вебинары и расстановки как участнику VEDA SANCTUM.\n"
            "• Доступ к архиву святилища - прошлым практикам, знаниям, ченнелингам.\n\n"
            "Ваш шаг дальше: отсюда не восходят временем. Дальше я вижу, считываю, провожу сама - "
            "различаю, как происходит пробуждение. Идите вглубь по-настоящему, приходите с живыми "
            "вопросами, будьте активны в поле. Остальное я увижу.\n"
            "Впереди - ступень Круг Силы."
        ),
        "ascension_intention_invite_text": (
            "🕯 Намерение-инициация входа\n"
            "Вы вступаете в Путь. Прежде чем идти дальше - остановитесь и почувствуйте.\n"
            "С чем Вы входите в это поле? К чему намереваетесь? Каким будет Ваш шаг в новый мир?\n"
            "Напишите от сердца, кратко. Я не оцениваю слова - поле слышит суть.\n"
            "Раз в месяц я буду возвращать Вас к этому намерению - чтобы Вы видели, как оно оживает."
        ),
        "ascension_intention_confirmation_text": (
            "Благодарю 🙏 {имя}, Ваше намерение сохранено - я буду возвращать Вас к нему раз в месяц.\n\n"
            "Напоминаю: на ступени Искра для Вас открыт мой дар - глубинный личный разбор именно "
            "этого намерения, в приоритете. Напишите мне о нём лично, когда будете готовы."
        ),
        "ascension_intention_recall_text": (
            "🕯 Ваше намерение, записанное при входе в поле:\n\n«{намерение}»\n\n"
            "Посмотрите - как оно живёт в Вас сейчас? 🙏"
        ),
        "luminar_intro_text": (
            "✨ Созвездие Люминаров\n"
            "Есть те, кого касание моего поля переполняет, и они приводят своих - близких по духу - "
            "к этим Вратам святилища.\n"
            "Не по просьбе. Из желания поделиться светом.\n"
            "Это отдельный путь чести, идущий рядом с Вашим восхождением.\n"
            "Тот, через кого другие приходят в моё пространство, становится Люминаром - вдохновением, "
            "от которого зажигаются другие.\n"
            "Приглашение засчитывается, когда пришедшие через Вас входят в VEDA SANCTUM|CODEofGOD "
            "по-настоящему.\n"
            "Важен не тот, кто узнал, а тот, кто присоединился.\n"
            "Это Закон Баланса: отданное от полноты возвращается к Вам."
        ),
        "ascension_level1_brief_text": (
            "🎁 Дано: доступ к пространству - вебинарам, практикам, VEDA HEALING FLOW, философии.\n\n"
            "👉🏼 Войдите в VEDA SANCTUM (выберите в menu этого бота). Пробыв в поле два месяца, Вы "
            "автоматически взойдёте на ступень «Искра» - и Вам откроются её дары."
        ),
        "ascension_level2_brief_text": (
            "🎁 Дано: первый шаг в святилище - поле начало тихую работу, открыт ритуал намерения. "
            "Мой дар: глубинный личный разбор Вашего запроса в приоритете, согласно написанному "
            "намерению - напишите его мне лично.\n\n"
            "👉🏼 Пребудьте в поле VEDA SANCTUM ещё 6 месяцев (итого 8), и откройте один из двух "
            "ключей: медитации VEDA HEALING FLOW или Люминар I - тогда взойдёте на ступень "
            "«Исследователь Глубины»."
        ),
        "ascension_level3_brief_text": (
            "🎁 Дано: закрытая медитация-инициация, особая цена на VEDA HEALING FLOW, доступ к "
            "архиву и вебинарам как участнику Санктума.\n\n"
            "Дальше не по времени - только моим личным ведением, диагностикой, чтением поля."
        ),
        "luminar_intro_short_text": (
            "✨ Созвездие Люминаров - путь чести, если пожелаете делиться светом. "
            "Подробнее в «Как устроен Путь»."
        ),
        "profile_greeting_text": "✨ {имя}, вот Ваш профиль в VEDAME SPACE",
        "profile_meditation_reminder_text": (
            "🌙 <b>VEDA HEALING FLOW</b>\nНе забывайте заглядывать в медитативный помощник VEDA HEALING FLOW."
        ),
        "profile_join_date_text": "С нами с {дата}, уже {дней}.",
        "profile_sanctum_header_text": "⚜️ <b>{название}</b>",
        "profile_sanctum_active_text": "Доступ активен до {дата}.",
        "profile_sanctum_expired_text": "Доступ закончился {дата}.",
        "profile_sanctum_promise_text": "⏰ Вы планировали оплатить {дата}.",
        "profile_sanctum_never_joined_text": "Узнайте больше - нажмите на кнопку ниже 👇",
        "profile_webinars_header_text": "📅 <b>Вебинары</b>",
        "profile_webinars_empty_text": "Пока не было.",
        "profile_path_step_label_text": "👑 Ступень: {ступень}",
        "profile_luminar_rank_label_text": "✨ {ранг}",
        "profile_referral_intro_text": "Ваша ссылка, чтобы приглашать других:\n{ссылка}",
        "profile_btn_renew_text": "Продлить",
        "profile_btn_read_level_text": "📖 Читать послание ступени",
        "profile_btn_luminar_intro_text": "✨ Подробнее о Созвездии Люминаров",
        "profile_btn_path_overview_text": "ℹ️ Как устроен Путь?",
        "profile_luminar_progress_zero_text": (
            "Пока никто не пришёл через Вашу ссылку на Veda Sanctum - но это может начаться в любой момент. "
            "Воспользуйтесь возможностью, дабы получить свой первый сакральный ключ."
        ),
        "profile_luminar_progress_text": (
            "Вы стали проводником в святилище Veda Sanctum для {число} {человек}. "
            "До ключа Люминар I осталось - {осталось}."
        ),
        "profile_luminar_progress_next_text": "Путь к {следующий_ранг}: {бар} - {число} из {порог}",
        "admin_personal_chat_link": "https://t.me/Alena_Devi_Veda",
        "faq_suggestion_invite_text": (
            "Какой вопрос добавить в «Частые вопросы»? Опишите его - я обязательно его учту 🙏"
        ),
        "faq_suggestion_confirm_text": "Спасибо! Ваш вопрос передан - я включу его в «Частые вопросы» 🙏",
        "faq_text": (
            "❓ <b>Частые вопросы</b>\n\n"
            "<b>Как оплатить участие или подписку?</b>\n"
            "Выберите нужный раздел в меню - бот покажет реквизиты и назначение платежа. После оплаты "
            "пришлите сюда скриншот чека, я подтвержу участие.\n\n"
            "<b>Что будет, если я не продлю VEDA SANCTUM вовремя?</b>\n"
            "Ничего страшного - доступ просто закончится до следующей оплаты. Бот заранее напомнит о "
            "продлении.\n\n"
            "<b>Можно ли изменить намерение, которое я написала на ступени Искра?</b>\n"
            "Да, в любой момент - кнопка «Изменить намерение» есть и в профиле, и в напоминаниях о нём.\n\n"
            "<b>Остались вопросы?</b>\n"
            "Нажмите «💌 Личное обращение» и напишите мне - я отвечу здесь же."
        ),
        "rules_text": (
            "📜 <b>Правила пространства</b>\n\n"
            "Это пространство создано для тех, кто выбирает искренность, глубину и уважение - к себе и "
            "к другим.\n\n"
            "Материалы, тексты и медитации в этом пространстве - авторские, принадлежат Alena Veda. "
            "Прошу не копировать и не распространять их без разрешения.\n\n"
            "Формальное обращение на Вы - для всех, всегда, это часть уважения, принятого в этом "
            "пространстве.\n\n"
            "Если у Вас есть вопрос или сомнение - лучше написать лично, чем оставаться в неясности."
        ),
        "bot_guide_text": (
            "🧭 <b>Как пользоваться ботом, коротко</b>\n\n"
            "⚜️ VEDA SANCTUM - закрытый канал, оформить или продлить подписку.\n"
            "🧘 VEDA HEALING FLOW - медитации, отдельный бот.\n"
            "✨ Мой профиль - Ваш путь, ранг, реферальная ссылка, календарь ритуалов.\n"
            "📅 Вебинары - расписание, регистрация, архив прошедших (с записями, если Вы в Sanctum "
            "или оплатили именно этот вебинар).\n"
            "💠 Философия Alena Veda - о подходе.\n"
            "📖 Архив публикаций - прошлые материалы по месяцам.\n"
            "❓ Инфо/Кодекс пространства - этот же раздел: частые вопросы и правила пространства.\n\n"
            "🌙 <b>Календарь ритуалов</b> - находится здесь же, в профиле и в Sanctum: даты новолуний, "
            "экадаши и других дней, с возможностью включить напоминания.\n\n"
            "✨ <b>Приглашайте друзей</b> - Ваша личная ссылка есть в профиле. Когда приглашённые "
            "входят в Sanctum, Вам открываются дары Люминаров.\n\n"
            "<b>Как оплатить:</b> выберите вебинар или Sanctum, нажмите кнопку оплаты, переведите по "
            "реквизитам, пришлите сюда скриншот чека - я подтвержу и открою доступ. Если ещё не готовы "
            "оплатить сейчас - можно нажать «Оплачу позже» и назвать дату, я подожду.\n\n"
            "Что-то непонятно - «💌 Личное обращение», отвечу лично."
        ),
        "luminar_referral_ping_text_1": (
            "Здорово! Вы провели в моё поле ещё одного человека. Продолжайте светить в мир, делитесь "
            "благОМ - это прекрасно!"
        ),
        "luminar_referral_ping_text_2": (
            "✨ Ещё один свет вошёл в это пространство через Вас. Так рождается Созвездие Люминаров - "
            "Вашими руками."
        ),
        "luminar_referral_ping_text_3": (
            "🌟 Через Вас снова открылась дверь в поле для кого-то нового. Продолжайте быть проводником света."
        ),
        "referral_welcome_text": (
            "✨ Вас выбрали и пригласили войти.\n"
            "Я вижу, что Вы прибыли сюда от прекрасного человека - {пригласивший} - значит, в Вас увидели "
            "того, кому это поле может быть созвучно в моём канале {название}. О нём подробнее далее."
        ),
        "ascension_overview_text": (
            "{имя}, вот как устроены ветви пространства Восхождения.\n"
            "Здесь два связанных, но разных пути.\n\n"
            "👑 Путь Восхождения - про Вашу личную глубину и присутствие в моём поле в VEDA SANCTUM. "
            "Ступени выглядят так:\n"
            "1 · Первое Касание - Вы в начале, у порога.\n"
            "2 · Искра - Вы вошли в Санктум, поле начало тихую работу.\n"
            "3 · Исследователь Глубины - Вы прошли шесть месяцев в поле (вместе с первыми двумя - "
            "восемь), и открыли один из двух ключей: медитации VEDA HEALING FLOW или ключ Люминар I.\n"
            "Далее ступени - «Круг Силы», и только после - ступень «Сфера Созидания», но туда не "
            "восходят временем - только моим личным проведением, диагностикой, чтением поля.\n\n"
            "✨ Созвездие Люминаров - отдельный путь ключей свечения в проводимости. Он про свет, "
            "которым Вы делитесь с другими - когда приглашённые Вами реально входят в VEDA SANCTUM "
            "и остаются в этом пути с нами. Итак, Сакральные Ключи:\n"
            "Люминар I - 5 вошедших от Вас.\n"
            "Люминар II - 10 вошедших.\n"
            "Люминар III - 30 вошедших.\n"
            "У каждого ключа - свой особый дар Вам лично от меня.\n\n"
            "Путь Восхождения открыт для каждого, кто остаётся в моём поле Veda Sanctum. "
            "Созвездие Люминаров - Ваш собственный выбор."
        ),
        "luminar_1_text": (
            "{имя}, Ваш свет притянул других к Истинному Знанию.\n"
            "Вы стали Люминаром I - тем, кто от полноты приводит своих к Вратам.\n"
            "Это не заслуга ума.\n"
            "Это перелив: Вас коснулось - и через Вас коснулось других.\n"
            "Ваш дар: {дар}.\n"
            "И этот свет открывает Вам ключ на ступень Исследователь Глубины."
        ),
        "luminar_2_text": (
            "{имя}, Ваш свет разгорается.\n"
            "Через Вас к полю пришли уже десятеро.\n"
            "Вы стали Люминаром II.\n"
            "Ваш дар: {дар}."
        ),
        "luminar_3_text": (
            "{имя}, Вы стали Люминаром III - светилом, подсвечивающим путь в круг близких по духу.\n"
            "Тридцать Душ обрели это место через Вас.\n"
            "Отныне Ваш доступ в VEDA SANCTUM - пожизненный и бесплатный.\n"
            "Ваш дар: {дар}."
        ),
        "webinars_intro_text": "Ближайшие вебинары:",
        "webinars_empty_text": (
            "Пока нет по графику предстоящих или открытых вебинаров.\n"
            "Наблюдайте за информацией в этом пространстве или загляните позже в раздел "
            "«Вебинары, практики, расстановки»🌿"
        ),
    }
    for key, value in _ascension_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- аудит текстов 2026-09-28: экраны VEDA SANCTUM ----------
    _sanctum_screen_defaults = {
        "not_ready_text": "Извините, регистрация сюда пока недоступна. Загляните чуть позже 🙏",
        "sanctum_placeholder_text": (
            "✨ Информация о канале VEDA SANCTUM | CODEofGOD скоро появится здесь. Загляните позже 🙏"
        ),
        "sanctum_lifetime_text": (
            "⚜️ VEDA SANCTUM | CODEofGOD\n\n"
            "🏆 Ваш доступ пожизненный и бесплатный - дар за ключ Люминар III."
        ),
        "sanctum_active_text": (
            "⚜️ VEDA SANCTUM | CODEofGOD\n\n"
            "Ваш доступ активен до {дата}.\n\n"
            "Хотите продлить заранее на следующий месяц? Стоимость: {цена} "
            "(закреплена за Вами, как за опытным участником Sanctum)."
        ),
        "sanctum_expired_text": (
            "⚜️ VEDA SANCTUM | CODEofGOD\n\n"
            "Ваш доступ закончился{дата_часть}.\n\n"
            "Хотите возобновить?\n{строка_цены}"
        ),
        "sanctum_price_locked_line": (
            "Стоимость подписки в месяц: {цена} - Ваша прежняя цена сохраняется до {дата_до}."
        ),
        "sanctum_price_current_line": "Стоимость подписки в месяц: {цена}.",
        "sanctum_already_lifetime_text": "У Вас уже пожизненный доступ в VEDA SANCTUM 🏆 Платить не нужно.",
        "sanctum_pending_request_text": (
            "У Вас уже есть заявка в VEDA SANCTUM | CODEofGOD - она ожидает проверки, "
            "я подтвержу её в ближайшее время 🙏"
        ),
        "sanctum_payment_instructions_text": (
            "Для вступления в VEDA SANCTUM | CODEofGOD переведите {цена}.\n\n"
            "{реквизиты}\n\n"
            "После оплаты пришлите сюда, в VEDAME SPACE, скриншот Вашего чека 📸\n\n"
            "Благодарю!"
        ),
        "sanctum_promise_prompt_text": (
            "На какую дату Вы планируете совершение оплаты?\n"
            "Пришлите в формате ДД.ММ.ГГГГ (например: 15.09.2026),\n"
            "я напомню Вам за день до неё.\n\n"
            "Это важно отметить именно здесь, в боте, а не писать мне лично: только отметка в боте "
            "показывает, что Вы возвращаетесь, - и пока дата не наступила, я не буду Вас беспокоить."
            "{ограничение}"
        ),
        "sanctum_promise_invalid_date_text": (
            "Не получилось распознать дату.\nПришлите в формате ДД.ММ.ГГГГ, например: 15.09.2026."
        ),
        "sanctum_promise_past_date_text": "Дата должна быть в будущем.\nПришлите, пожалуйста, другую дату.",
        "sanctum_promise_too_late_text": (
            "Эту дату я, к сожалению, принять не могу: прежняя цена сохраняется до {дата}.\n"
            "Пришлите, пожалуйста, дату не позже {дата}."
        ),
        "sanctum_promise_confirm_text": "Хорошо 🙏\nЯ напомню Вам {дата_напоминания}, за день до {дата}.",
        "sanctum_lifetime_profile_line": "🏆 Доступ пожизненный и бесплатный - дар за ключ Люминар III.",
        "sanctum_manual_grant_text": (
            "✨ Вам открыт доступ в VEDA SANCTUM | CODEofGOD до {дата} по цене {цена}."
        ),
    }
    for key, value in _sanctum_screen_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- аудит текстов 2026-09-28: оплата и чек ----------
    _payment_flow_defaults = {
        "receipt_thanks_text": "Спасибо! Чек отправлен на проверку, я сообщу Вам о результате 🙏",
        "receipt_no_active_request_text": "Не нашла активную заявку. Попробуйте зарегистрироваться заново.",
        "receipt_wrong_type_text": "Пришлите, пожалуйста, именно скриншот (фото) чека 📸",
        "payment_confirmed_text": "✅ Оплата за «{название}» подтверждена!",
        "payment_confirmed_sanctum_line": (
            "\n\nПодписка активна до {дата} по цене {цена}. "
            "Я напомню заранее, когда придёт время продлевать подписку."
        ),
        "payment_declined_text": (
            "❌ Оплату за «{название}» не удалось подтвердить.\n\n"
            "Если хотите прислать чек ещё раз - нажмите «📸 Отправить чек».\n"
            "Если остались вопросы - нажмите «💌 Личное обращение», напишите сообщение, "
            "и я отвечу Вам здесь же 🙏"
        ),
        "resend_receipt_not_found_text": "Эта заявка больше не найдена.",
        "resend_receipt_already_confirmed_text": "Эта оплата уже подтверждена ✅",
        "resend_receipt_already_sent_text": "Чек уже отправлен и ожидает проверки 🙏",
        "resend_receipt_prompt_text": "Пришлите, пожалуйста, скриншот чека по «{название}» 📸",
        "payment_screenshot_text": (
            "📸 После оплаты по реквизитам Алёны пришлите скриншот Вашего чека (просто картинкой) "
            "прямо сюда, под этим сообщением.\n"
            "Ничего писать в строке ввода текста не нужно, только картинка Вашего скриншота чека об оплате."
        ),
    }
    for key, value in _payment_flow_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- аудит текстов 2026-09-28: остальные экраны «Вебинары» ----------
    _webinar_screen_defaults = {
        "webinars_past_empty_text": "Прошедших пока нет.",
        "webinars_past_header_text": "📜 Прошедшие вебинары, практики, расстановки:",
        "webinar_unavailable_past_text": "Эта запись больше недоступна.",
        "webinar_video_locked_line": (
            "\n\n🎬 Запись доступна участникам VEDA SANCTUM (это часть того, что даёт Sanctum) "
            "или тем, кто оплатил именно этот вебинар."
        ),
        "webinar_unavailable_text": "Этот вебинар больше недоступен",
        "webinar_pending_request_text": (
            "У Вас уже есть заявка на «{название}» - она ожидает проверки, "
            "я подтвержу её в ближайшее время 🙏"
        ),
        "webinar_payment_instructions_text": (
            "Отлично! Для участия в «{название}» переведите {цена}.\n\n"
            "{реквизиты}\n\n"
            "После оплаты пришлите сюда, в VEDAME SPACE, скриншот Вашего чека 📸"
        ),
        "webinar_reviews_empty_text": "Отзывов пока нет",
        "webinar_reviews_header_text": "⭐ Отзывы о «{название}»",
    }
    for key, value in _webinar_screen_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- аудит текстов 2026-09-28: общие, архив, вопросы, намерение, инфо ----------
    _general_texts_defaults = {
        "unknown_user_text": "Чтобы начать (или начать заново), напишите /start 🙏",
        "require_text_text": "Пришлите, пожалуйста, обычным текстом 🙏",
        "name_question_text": "Как я могу к Вам обращаться? Представьтесь, пожалуйста. ✨",
        "name_thanks_text": "Благодарю, {имя}! 🙏",
        "cancel_text": "Отменено.",
        "feed_intro_text": (
            "Здесь собраны все публикации, упорядоченные по месяцам.\n\n"
            "Выберите месяц ниже - откроется самая свежая публикация из него, а дальше "
            "листайте кнопками «Раньше» / «Позже» под ней, одну за другой."
        ),
        "feed_empty_text": "Пока в архиве пусто.\nЗагляните позже 🌿",
        "feed_month_empty_text": "В этом месяце публикаций больше нет.",
        "feed_post_not_found_text": "Публикация не найдена - возможно, её удалили.",
        "contact_admin_prompt_text": (
            "Напишите Ваше сообщение (можно текстом, фото, голосовое) - я его передам и Вам ответят здесь же 🙏"
        ),
        "contact_admin_thanks_text": "Благодарю, я передала Ваше сообщение 🙏\nОтвет придёт здесь же, в этом чате.",
        "contact_admin_failed_text": "Не получилось передать сообщение, попробуйте ещё раз чуть позже 🙏",
        "question_unavailable_text": "Вопросы сейчас недоступны.",
        "question_prompt_text": "Напишите Ваш вопрос, я передам его Алёне 🙏",
        "question_thanks_text": "Спасибо, я передала Ваш вопрос 🙏",
        "question_failed_text": "Не получилось передать вопрос, попробуйте ещё раз чуть позже 🙏",
        "question_answer_prefix_text": "💬 Ответ на Ваш вопрос:\n\n{ответ}",
        "qa_public_empty_text": "Пока нет опубликованных вопросов.",
        "qa_public_header_text": "💬 Вопросы и ответы",
        "intention_missing_text": "У Вас пока нет записанного намерения.",
        "intention_edit_prompt_text": (
            "Сейчас записано:\n«{текст}»\n\nПришлите новый текст намерения полностью - он заменит прежний."
        ),
        "info_menu_text": "Выберите, что интересует:",
    }
    for key, value in _general_texts_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- аудит текстов 2026-09-29: названия ступеней Пути и рангов Люминаров ----------
    _naming_defaults = {
        "ascension_level1_name": "Первое Касание",
        "ascension_level2_name": "Искра",
        "ascension_level3_name": "Исследователь Глубины",
        "luminar_label_1": "Орден: Люминар I",
        "luminar_label_2": "Орден: Люминар II",
        "luminar_label_3": "Орден: Люминар III",
        "luminar_rank_name_1": "Люминар I",
        "luminar_rank_name_2": "Люминар II",
        "luminar_rank_name_3": "Люминар III",
        "luminar_gift_1": "2 месяца в VEDA SANCTUM",
        "luminar_gift_2": "скидка 30% на личную сессию-терапию или распаковку личности",
        "luminar_gift_3": "2 часа личной глубинной сессии со мной - распаковка личности или энерготерапия, на Ваш выбор",
    }
    for key, value in _naming_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- 2026-10-08: вводное сообщение после имени и пауза перед «Первым Касанием» ----------
    _intro_defaults = {
        "intro_msg_enabled": "1",
        "intro_msg_text": (
            "{имя}, благодарю Вас 🙏\n\n"
            "У меня к Вам ВАЖНАЯ ПРОСЬБА: читайте каждое сообщение внимательно и до конца. "
            "Всё, что нужно делать - уже написано в каждом сообщении и в кнопках под ними.\n"
            "Не спешите, и путь станет понятным."
        ),
        "intro_msg_button": "Понятно. Продолжить ➡️",
        "intro_msg_photo": "",
        "welcome_pause_seconds": "30",
        "newcomer_notify_enabled": "1",
        "intro_nudge_enabled": "1",
        "intro_nudge_hours": "24",
        "intro_nudge_text": (
            "{имя}, Вы остановились на первом шаге, и это совсем не страшно 🌿\n\n"
            "Выше в чате лежит моё сообщение с важной просьбой читать всё внимательно. "
            "Чтобы идти дальше, нужно только нажать кнопку «{кнопка}». Писать ничего не нужно.\n\n"
            "Такая же кнопка есть и под этим сообщением, можно нажать её здесь."
        ),
    }
    for key, value in _intro_defaults.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not c.fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

    # переименование кнопки (2026-10-08): «Инициировать шаг оплаты» -> «Хочу оплатить подписку».
    # Заменяем только точное название кнопки внутри её уже написанных текстов, один раз.
    c.execute("SELECT value FROM settings WHERE key = 'migr_pay_button_rename'")
    if not c.fetchone():
        for _k, _v in c.execute("SELECT key, value FROM settings WHERE value LIKE '%Инициировать шаг оплаты%'").fetchall():
            c.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (_v.replace("Инициировать шаг оплаты", "Хочу оплатить подписку"), _k),
            )
        c.execute(
            "UPDATE sanctum SET laws_text = REPLACE(laws_text, 'Инициировать шаг оплаты', 'Хочу оплатить подписку') "
            "WHERE laws_text LIKE '%Инициировать шаг оплаты%'"
        )
        c.execute("INSERT INTO settings (key, value) VALUES ('migr_pay_button_rename', '1')")

    # аудит 2026-09-29: поздравления Люминаров раньше писали дар прямо в тексте,
    # {дар} нигде реально не подставлялся - теперь дар вынесен в отдельную
    # редактируемую настройку (luminar_gift_N), тексты переведены на {дар}.
    # Трогаем только нетронутые тексты - если она уже сама что-то переписала,
    # не перезаписываем.
    _pre_dar_luminar_texts = {
        "luminar_1_text": (
            "{имя}, Ваш свет притянул других к Истинному Знанию.\n"
            "Вы стали Люминаром I - тем, кто от полноты приводит своих к Вратам.\n"
            "Это не заслуга ума.\n"
            "Это перелив: Вас коснулось - и через Вас коснулось других.\n"
            "Ваш дар: 2 месяца в Veda Sanctum.\n"
            "И этот свет открывает Вам ключ на ступень Исследователь Глубины."
        ),
        "luminar_2_text": (
            "{имя}, Ваш свет разгорается.\n"
            "Через Вас к полю пришли уже десятеро.\n"
            "Вы стали Люминаром II.\n"
            "Ваш дар: 30% на личную сессию-терапию или распаковку личности."
        ),
        "luminar_3_text": (
            "{имя}, Вы стали Люминаром III - светилом, подсвечивающим путь в круг близких по духу.\n"
            "Тридцать Душ обрели это место через Вас.\n"
            "Отныне Ваш доступ в VEDA SANCTUM - пожизненный и бесплатный.\n"
            "Ваш дар: 2 часа личной глубинной сессии со мной - распаковка личности или энерготерапия, "
            "на Ваш выбор."
        ),
    }
    for _key, _old_value in _pre_dar_luminar_texts.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (_key,))
        _row = c.fetchone()
        if _row and _row[0] == _old_value:
            c.execute("UPDATE settings SET value = ? WHERE key = ?", (_ascension_defaults[_key], _key))

    _pre_dar_gifts = {
        "luminar_gift_1": "2 месяца в VEDA SANCTUM в дар",
        "luminar_gift_3": "2 часа личной глубинной сессии в дар - распаковка личности или энерготерапия, на выбор",
    }
    for _key, _old_value in _pre_dar_gifts.items():
        c.execute("SELECT value FROM settings WHERE key = ?", (_key,))
        _row = c.fetchone()
        if _row and _row[0] == _old_value:
            c.execute("UPDATE settings SET value = ? WHERE key = ?", (_naming_defaults[_key], _key))

    # дар за Люминар I увеличен с 1 до 2 месяцев (её решение 2026-09-24) - если
    # текст ещё не был правлен вручную (значение точно как в старом дефолте),
    # обновляем его на новую формулировку; если она уже сама переписала текст,
    # не трогаем
    _old_luminar_1_text = (
        "{имя}, Ваш свет притянул других к Истинному Знанию.\n"
        "Вы стали Люминаром I - тем, кто от полноты приводит своих к Вратам.\n"
        "Это не заслуга ума.\n"
        "Это перелив: Вас коснулось - и через Вас коснулось других.\n"
        "Ваш дар: месяц в Veda Sanctum.\n"
        "И этот свет открывает Вам ключ на ступень Исследователь Глубины."
    )
    c.execute("SELECT value FROM settings WHERE key = 'luminar_1_text'")
    _row = c.fetchone()
    if _row and _row[0] == _old_luminar_1_text:
        c.execute(
            "UPDATE settings SET value = ? WHERE key = 'luminar_1_text'",
            (_ascension_defaults["luminar_1_text"],),
        )

    # дар за Люминар III дополнен пожизненным бесплатным доступом в VEDA SANCTUM
    # (её решение 2026-09-25), тот же принцип - трогаем только нетронутый текст
    _old_luminar_3_text = (
        "{имя}, Вы стали Люминаром III - светилом, подсвечивающим путь в круг близких по духу.\n"
        "Тридцать Душ обрели это место через Вас.\n"
        "Ваш дар: 2 часа личной глубинной сессии со мной - распаковка личности или энерготерапия, "
        "на Ваш выбор."
    )
    c.execute("SELECT value FROM settings WHERE key = 'luminar_3_text'")
    _row = c.fetchone()
    if _row and _row[0] == _old_luminar_3_text:
        c.execute(
            "UPDATE settings SET value = ? WHERE key = 'luminar_3_text'",
            (_ascension_defaults["luminar_3_text"],),
        )

    conn.commit()
    conn.close()

    # календарь ритуалов (таблица дат, колонки у users, тексты) - отдельный модуль;
    # импорт здесь, а не наверху файла, чтобы не было круговой зависимости
    import rituals
    rituals.init_rituals()


# ---------- users ----------

def add_user(user_id, username, first_name, referred_by=None):
    """Возвращает True, если это НОВЫЙ человек (только что добавлен), и False,
    если он уже был в базе — нужно, чтобы спрашивать "как к вам обращаться"
    только один раз, при самом первом /start. referred_by (если передан) —
    Telegram ID пригласившего, попадает в базу только при реальной вставке
    (INSERT OR IGNORE не даёт переписать его задним числом у уже известного
    человека по случайно перешедшей старой реферальной ссылке)."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at, referred_by) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, first_name, _now(), referred_by),
    )
    conn.commit()
    is_new = cur.rowcount > 0
    conn.close()
    return is_new


def set_preferred_name(user_id, name):
    conn = get_conn()
    conn.execute("UPDATE users SET preferred_name = ? WHERE user_id = ?", (name, user_id))
    conn.commit()
    conn.close()


def set_last_action(user_id, label):
    """Запоминает последнее действие человека (для профиля и уведомлений о новичках)."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET last_action = ?, last_action_at = ? WHERE user_id = ?",
        (label, _now(), user_id),
    )
    conn.commit()
    conn.close()


def mark_intro_sent(user_id):
    conn = get_conn()
    conn.execute("UPDATE users SET intro_sent_at = ?, intro_nudge_sent = 0 WHERE user_id = ?", (_now(), user_id))
    conn.commit()
    conn.close()


def mark_intro_nudge_sent(user_id):
    conn = get_conn()
    conn.execute("UPDATE users SET intro_nudge_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_intro_nudge_due(cutoff_datetime_str):
    """Люди, которым давно отправили вводное сообщение, а они не нажали кнопку и
    после этого вообще ничего не делали в боте; напоминание им ещё не отправляли."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.* FROM users u
        WHERE u.intro_sent_at IS NOT NULL AND u.intro_sent_at <= ?
        AND (u.intro_nudge_sent IS NULL OR u.intro_nudge_sent = 0)
        AND u.ritual_intro_shown_at IS NULL
        AND (u.last_action_at IS NULL OR u.last_action_at <= u.intro_sent_at)
        AND (u.blocked IS NULL OR u.blocked = 0)
        AND (u.self_departed IS NULL OR u.self_departed = 0)
        AND u.preferred_name IS NOT NULL AND TRIM(u.preferred_name) != ''
        AND u.user_id NOT IN (SELECT admin_id FROM admins)
    """, (cutoff_datetime_str,)).fetchall()
    conn.close()
    return rows


def get_newcomer_funnel(since_str=None):
    """Воронка новичков за период (people = нажавшие «Старт» не раньше since_str, без админов).
    Возвращает словарь с числами по шагам и список последних действий тех, кто не оплатил."""
    conn = get_conn()
    where = "u.user_id NOT IN (SELECT admin_id FROM admins)"
    params = []
    if since_str:
        where += " AND u.created_at >= ?"
        params.append(since_str)
    people = conn.execute(f"SELECT u.* FROM users u WHERE {where}", params).fetchall()
    regs = conn.execute("SELECT user_id, product_type, status FROM registrations").fetchall()
    members = {r["user_id"] for r in conn.execute("SELECT user_id FROM sanctum_membership").fetchall()}
    conn.close()
    reg_users = {}
    for r in regs:
        reg_users.setdefault(r["user_id"], []).append(r)
    out = {
        "came": 0, "named": 0, "intro_ok": 0, "sanctum_opened": 0, "pay_started": 0,
        "receipt_sent": 0, "paid": 0, "stuck": {},
    }
    for u in people:
        uid = u["user_id"]
        rs = reg_users.get(uid, [])
        named = bool((u["preferred_name"] or "").strip())
        out["came"] += 1
        out["named"] += named
        out["intro_ok"] += bool(u["ritual_intro_shown_at"])
        opened = bool(u["sanctum_first_opened_at"]) or any(r["product_type"] == "sanctum" for r in rs) or uid in members
        out["sanctum_opened"] += opened
        out["pay_started"] += bool(rs)
        out["receipt_sent"] += any(r["status"] != "awaiting_receipt" for r in rs)
        paid = any(r["status"] == "confirmed" for r in rs)
        out["paid"] += paid
        if not paid and uid not in members:
            label = u["last_action"] or ("Нажал «Старт» (имя не назвал)" if not named else "нет данных (был в боте до установки)")
            out["stuck"][label] = out["stuck"].get(label, 0) + 1
    return out


def get_unnamed_users():
    """Люди, которые нажали «Старт», но не назвали имя (новые сверху)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE preferred_name IS NULL OR TRIM(preferred_name) = '' "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_all_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_all_users_full():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def is_user_blocked(user_id):
    conn = get_conn()
    row = conn.execute("SELECT blocked FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(row["blocked"]) if row else False


def set_user_blocked(user_id, blocked):
    conn = get_conn()
    cur = conn.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def set_user_self_departed(user_id, departed):
    """Отмечает, что человек сам заблокировал бота/вышел из чата (departed=True)
    или вернулся, написав снова (departed=False) — событие приходит от Telegram
    само, независимо от того, есть ли этот user_id уже в таблице (ничего не
    делает, если человека там ещё нет — до /start ему нечего отмечать)."""
    conn = get_conn()
    conn.execute("UPDATE users SET self_departed = ? WHERE user_id = ?", (1 if departed else 0, user_id))
    conn.commit()
    conn.close()


def get_self_departed_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE self_departed = 1").fetchone()["n"]
    conn.close()
    return n


def get_self_departed_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users WHERE self_departed = 1").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_blocked_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users WHERE blocked = 1").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def reset_user_onboarding(user_id):
    """Даёт человеку пройти знакомство заново (бот снова спросит имя, повторит
    приветствие и «Первое Касание») - и больше ничего. Раньше здесь строка
    человека стиралась целиком, а вместе с ней пропадали ранг Люминара и отметка
    о покупке VEDA HEALING FLOW (они хранятся в этой же строке), хотя это
    постоянные достижения. Знакомство в cmd_start определяется только по
    сохранённому имени (preferred_name), поэтому сбрасываем ровно его -
    ступень, Sanctum, цена, заявки, Люминар, покупки и пригласивший не трогаются."""
    conn = get_conn()
    conn.execute("UPDATE users SET preferred_name = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_users_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    conn.close()
    return n


def get_new_users_count(since_datetime_str):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (since_datetime_str,)
    ).fetchone()["n"]
    conn.close()
    return n


def get_users_count_in_range(start_datetime_str, end_datetime_str):
    """Сколько людей зарегистрировалось в промежутке [start, end) — для сравнения
    "эта неделя vs прошлая" и подобной динамики."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE created_at >= ? AND created_at < ?",
        (start_datetime_str, end_datetime_str),
    ).fetchone()["n"]
    conn.close()
    return n


def get_new_users_by_day(days: int):
    """Новые пользователи по дням за последние `days` дней (включая сегодня) —
    список (дата ГГГГ-ММ-ДД, количество), только дни с хотя бы одним человеком."""
    conn = get_conn()
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM users "
        "WHERE created_at >= ? GROUP BY d ORDER BY d",
        (since,),
    ).fetchall()
    conn.close()
    return [(r["d"], r["n"]) for r in rows]


def get_confirmed_count_in_range(start_datetime_str, end_datetime_str, product_type=None):
    """Сколько оплат подтверждено в промежутке [start, end) — по updated_at
    (момент, когда статус стал 'confirmed'). product_type=None — оба типа сразу."""
    conn = get_conn()
    query = "SELECT COUNT(*) AS n FROM registrations WHERE status = 'confirmed' AND updated_at >= ? AND updated_at < ?"
    params = [start_datetime_str, end_datetime_str]
    if product_type:
        query += " AND product_type = ?"
        params.append(product_type)
    n = conn.execute(query, params).fetchone()["n"]
    conn.close()
    return n


def get_confirmed_by_day(days: int, product_type=None):
    """Подтверждённые оплаты по дням за последние `days` дней — список
    (дата, количество), только дни с хотя бы одной оплатой."""
    conn = get_conn()
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    query = (
        "SELECT substr(updated_at, 1, 10) AS d, COUNT(*) AS n FROM registrations "
        "WHERE status = 'confirmed' AND updated_at >= ?"
    )
    params = [since]
    if product_type:
        query += " AND product_type = ?"
        params.append(product_type)
    query += " GROUP BY d ORDER BY d"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [(r["d"], r["n"]) for r in rows]


def get_registration_funnel(product_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM registrations WHERE product_type = ? GROUP BY status",
        (product_type,),
    ).fetchall()
    conn.close()
    counts = {"awaiting_receipt": 0, "awaiting_confirmation": 0, "confirmed": 0, "declined": 0}
    for r in rows:
        counts[r["status"]] = r["n"]
    return counts


def get_webinar_registration_counts():
    conn = get_conn()
    rows = conn.execute("""
        SELECT product_title, status, COUNT(*) AS n
        FROM registrations WHERE product_type = 'webinar'
        GROUP BY product_title, status
    """).fetchall()
    conn.close()
    return rows


# ---------- сегменты для рассылки ----------

def get_sanctum_active_user_ids(today_iso):
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id FROM sanctum_membership WHERE valid_until >= ? AND (status IS NULL OR status != 'removed')",
        (today_iso,),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_sanctum_expired_user_ids(today_iso):
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id FROM sanctum_membership WHERE valid_until < ? AND (status IS NULL OR status != 'removed')",
        (today_iso,),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_sanctum_needs_attention(today_iso):
    """Кто просрочил оплату VEDA SANCTUM, не убран вручную, и при этом НЕ
    назвал дату оплаты (либо назвал, но эта дата уже прошла без оплаты) -
    её решение 2026-09-27: отдельный список, чтобы не пропустить таких людей
    в общем списке подписчиков по мере роста базы. Полные строки users +
    membership - для имени, ID, цены и даты."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sm.user_id, sm.valid_until, sm.price, sm.promise_date,
               u.username, u.first_name, u.preferred_name
        FROM sanctum_membership sm
        JOIN users u ON u.user_id = sm.user_id
        WHERE sm.valid_until < ?
        AND (sm.status IS NULL OR sm.status != 'removed')
        AND (sm.promise_date IS NULL OR sm.promise_date < ?)
        ORDER BY sm.valid_until ASC
    """, (today_iso, today_iso)).fetchall()
    conn.close()
    return rows


def get_sanctum_removed_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM sanctum_membership WHERE status = 'removed'").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_sanctum_never_user_ids():
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.user_id FROM users u
        LEFT JOIN sanctum_membership sm ON sm.user_id = u.user_id
        WHERE sm.user_id IS NULL
    """).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_webinar_attendee_user_ids():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM registrations WHERE product_type = 'webinar'"
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_never_purchased_user_ids():
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.user_id FROM users u
        WHERE u.user_id NOT IN (SELECT user_id FROM registrations)
        AND u.user_id NOT IN (SELECT user_id FROM sanctum_membership)
    """).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_unfinished_payment_user_ids():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM registrations WHERE status IN ('awaiting_receipt', 'awaiting_confirmation')"
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ---------- автовозврат "потерянных" людей ----------

def get_all_awaiting_receipt():
    """ВСЕ заявки (и вебинары, и VEDA SANCTUM), которые сейчас застряли на этапе
    "чек ещё не прислали" — в отличие от get_stalled_registrations, без фильтра
    по времени и без учёта stall_reminder_sent: это для ручного просмотра
    администратором ("кто сейчас в процессе"), а не для автоматических напоминаний."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM registrations WHERE status = 'awaiting_receipt' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return rows


def get_stalled_registrations(cutoff_datetime_str, today_iso=None):
    """Заявки, которые всё ещё ждут чек (человек не прислал скриншот оплаты)
    дольше настроенного срока — и которым ещё не отправляли напоминание.
    Заявки на Sanctum тех, кто сам назвал дату оплаты («Оплачу позже»), которая
    ещё не прошла, не трогаем: им придёт своё напоминание за день до даты."""
    conn = get_conn()
    sql = (
        "SELECT * FROM registrations WHERE status = 'awaiting_receipt' "
        "AND created_at <= ? AND (stall_reminder_sent IS NULL OR stall_reminder_sent = 0)"
    )
    params = [cutoff_datetime_str]
    if today_iso:
        sql += (
            " AND NOT (product_type = 'sanctum' AND user_id IN ("
            "SELECT user_id FROM sanctum_membership WHERE promise_date IS NOT NULL AND promise_date >= ?))"
        )
        params.append(today_iso)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def mark_stall_reminder_sent(reg_id):
    conn = get_conn()
    conn.execute("UPDATE registrations SET stall_reminder_sent = 1 WHERE id = ?", (reg_id,))
    conn.commit()
    conn.close()


def get_silent_never_purchased_user_ids(cutoff_datetime_str):
    """Люди, которые пришли достаточно давно (created_at раньше cutoff), никогда
    ничего не покупали и ни разу не были в VEDA SANCTUM, и которым ещё не
    отправляли напоминание "пришёл и пропал". Возвращает полные строки users
    (не только id) — нужны для персонализации текста по имени.

    Отдельно исключены те, кто уже смотрел VEDA SANCTUM (sanctum_intro_viewed_at
    задан) или кому уже отправили нацеленное "поведенческое" напоминание
    (см. get_sanctum_intro_viewers_due) — им это, более точное по сути,
    напоминание идёт ВМЕСТО общего, а не вдобавок к нему."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.* FROM users u
        WHERE u.created_at <= ?
        AND (u.reengage_sent IS NULL OR u.reengage_sent = 0)
        AND u.sanctum_intro_viewed_at IS NULL
        AND (u.sanctum_nudge_sent IS NULL OR u.sanctum_nudge_sent = 0)
        AND u.user_id NOT IN (SELECT user_id FROM registrations)
        AND u.user_id NOT IN (SELECT user_id FROM sanctum_membership)
    """, (cutoff_datetime_str,)).fetchall()
    conn.close()
    return rows


def mark_sanctum_intro_viewed(user_id):
    """Обновляет момент последнего просмотра VEDA SANCTUM - но только пока
    человеку ещё не отправлено "поведенческое" напоминание (см.
    get_sanctum_intro_viewers_due) - после отправки больше не отслеживаем,
    чтобы не запустить бесконечный цикл повторных напоминаний."""
    conn = get_conn()
    # самый первый просмотр запоминаем навсегда (для отчёта «Путь новичков»)
    conn.execute(
        "UPDATE users SET sanctum_first_opened_at = ? WHERE user_id = ? AND sanctum_first_opened_at IS NULL",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    conn.execute(
        "UPDATE users SET sanctum_intro_viewed_at = ? "
        "WHERE user_id = ? AND (sanctum_nudge_sent IS NULL OR sanctum_nudge_sent = 0)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    conn.commit()
    conn.close()


def clear_sanctum_intro_viewed(user_id):
    """Человек реально начал оформление (sanctum_apply) - "поведенческое"
    напоминание больше не нужно, обычные напоминания об оплате (stall) уже
    подхватят его дальше."""
    conn = get_conn()
    conn.execute("UPDATE users SET sanctum_intro_viewed_at = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_sanctum_intro_viewers_due(cutoff_datetime_str):
    """Люди, которые смотрели информацию о VEDA SANCTUM (см.
    mark_sanctum_intro_viewed), но не дошли до "Инициировать шаг" дольше
    настроенного срока, и которым это конкретное напоминание ещё не
    отправляли.

    Отдельно исключены те, у кого уже есть заявка на Sanctum в ожидании (чек
    или проверка) - обнаружен и закрыт реальный пограничный случай 2026-09-18:
    человек нажимает "Инициировать шаг" (заявка создаётся, отметка просмотра
    снимается), затем кнопкой "⬅️ Назад" возвращается на экран Sanctum - тот
    заново отмечает "просмотрено", хотя оформление уже идёт. Без этого
    исключения такой человек получил бы сразу два разных напоминания -
    "завис на оплате" И "посмотрел, не начал" - про один и тот же шаг."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM users
        WHERE sanctum_intro_viewed_at IS NOT NULL
        AND sanctum_intro_viewed_at <= ?
        AND (sanctum_nudge_sent IS NULL OR sanctum_nudge_sent = 0)
        AND user_id NOT IN (
            SELECT user_id FROM registrations
            WHERE product_type = 'sanctum' AND status IN ('awaiting_receipt', 'awaiting_confirmation')
        )
    """, (cutoff_datetime_str,)).fetchall()
    conn.close()
    return rows


def mark_sanctum_nudge_sent(user_id):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET sanctum_nudge_sent = 1, sanctum_intro_viewed_at = NULL WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def mark_reengage_sent(user_id):
    conn = get_conn()
    conn.execute("UPDATE users SET reengage_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_lapsed_sanctum_user_ids(cutoff_date_str):
    """Люди, у которых доступ в VEDA SANCTUM истёк как минимум cutoff_date_str
    (valid_until раньше этой даты), которых она НЕ убирала вручную (status
    'removed' - её осознанное решение, автоматика туда не лезет), и которым
    ещё не отправляли возвратное напоминание за ЭТО истечение (winback_sent
    сбрасывается в 0 при каждой новой оплате, см. upsert_sanctum_membership -
    значит если человек продлит, а потом снова забудет, напомнить можно будет
    ещё раз). Возвращает полные строки users, объединённые с ценой/датой
    подписки - нужны и для персонализации, и для текста напоминания."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.*, sm.valid_until, sm.price AS sanctum_price, sm.promise_date
        FROM sanctum_membership sm
        JOIN users u ON u.user_id = sm.user_id
        WHERE sm.status != 'removed'
        AND sm.valid_until < ?
        AND (sm.winback_sent IS NULL OR sm.winback_sent = 0)
    """, (cutoff_date_str,)).fetchall()
    conn.close()
    return rows


def get_memberships_for_rules():
    """Все подписки вместе с данными человека - основа для ежедневных проверок
    правил ухода (main.py, check_reengagement): напоминание "последний шанс
    сохранить цену" и обнуление времени в поле после долгого отсутствия. Только
    те, кому бот вообще может написать (есть карточка в users)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sm.user_id, sm.valid_until, sm.price, sm.status, sm.promise_date, sm.accumulated_days,
               sm.removed_at, sm.lastchance_sent, sm.stage_reset_done,
               u.preferred_name, u.first_name, u.username, u.luminar_count, u.bought_meditation_bot,
               u.blocked
        FROM sanctum_membership sm
        JOIN users u ON u.user_id = sm.user_id
    """).fetchall()
    conn.close()
    return rows


def mark_lastchance_sent(user_id):
    conn = get_conn()
    conn.execute("UPDATE sanctum_membership SET lastchance_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def mark_stage_reset_done(user_id):
    """Обнуляет накопленное время в поле (accumulated_days) - НЕ трогает
    ранг Люминара и отметку покупки VEDA HEALING FLOW (они в users, а не тут)."""
    conn = get_conn()
    conn.execute(
        "UPDATE sanctum_membership SET accumulated_days = 0, stage_reset_done = 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def mark_winback_sent(user_id):
    conn = get_conn()
    conn.execute("UPDATE sanctum_membership SET winback_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- admins ----------

def is_admin(user_id):
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM admins WHERE admin_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def add_admin(admin_id, is_owner=False):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO admins (admin_id, added_at, is_owner) VALUES (?, ?, ?)",
        (admin_id, _now(), 1 if is_owner else 0),
    )
    conn.commit()
    conn.close()


def remove_admin(admin_id):
    conn = get_conn()
    conn.execute("DELETE FROM admins WHERE admin_id = ?", (admin_id,))
    conn.execute("DELETE FROM admin_permissions WHERE admin_id = ?", (admin_id,))
    conn.commit()
    conn.close()


def get_all_admin_ids():
    conn = get_conn()
    rows = conn.execute("SELECT admin_id FROM admins").fetchall()
    conn.close()
    return [r["admin_id"] for r in rows]


def is_owner(user_id):
    conn = get_conn()
    row = conn.execute("SELECT is_owner FROM admins WHERE admin_id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(row["is_owner"]) if row else False


def has_permission(user_id, permission_key):
    """Владелец (Вы) — всегда True. Обычный администратор — только если этот
    конкретный раздел ему явно разрешён (см. admin_permissions/adm_admins)."""
    if is_owner(user_id):
        return True
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM admin_permissions WHERE admin_id = ? AND permission_key = ?",
        (user_id, permission_key),
    ).fetchone()
    conn.close()
    return row is not None


def get_admin_permissions(admin_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT permission_key FROM admin_permissions WHERE admin_id = ?", (admin_id,)
    ).fetchall()
    conn.close()
    return {r["permission_key"] for r in rows}


def set_admin_permissions(admin_id, permission_keys):
    conn = get_conn()
    conn.execute("DELETE FROM admin_permissions WHERE admin_id = ?", (admin_id,))
    conn.executemany(
        "INSERT INTO admin_permissions (admin_id, permission_key) VALUES (?, ?)",
        [(admin_id, key) for key in permission_keys],
    )
    conn.commit()
    conn.close()


# ---------- лента (архив публикаций) ----------

def add_feed_post(content_type, text, file_id, file_ids_json, expires_at, allow_questions=False):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO feed_posts (content_type, text, file_id, file_ids_json, created_at, expires_at, allow_questions)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content_type, text, file_id, file_ids_json, _now(), expires_at, 1 if allow_questions else 0),
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def get_feed_months(today_iso):
    """Список месяцев (в формате ГГГГ-ММ), в которых есть хотя бы одна ещё не
    истёкшая публикация — новые месяцы сверху, для экрана выбора месяца."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT substr(created_at, 1, 7) AS ym FROM feed_posts "
        "WHERE expires_at IS NULL OR expires_at >= ? ORDER BY ym DESC",
        (today_iso,),
    ).fetchall()
    conn.close()
    return [r["ym"] for r in rows]


def get_feed_posts_in_month(year_month, today_iso):
    """Ещё не истёкшие публикации конкретного месяца (ГГГГ-ММ), новые сверху —
    готовый список для пролистывания вперёд/назад внутри месяца."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM feed_posts WHERE substr(created_at, 1, 7) = ? "
        "AND (expires_at IS NULL OR expires_at >= ?) ORDER BY id DESC",
        (year_month, today_iso),
    ).fetchall()
    conn.close()
    return rows


def get_feed_post(post_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM feed_posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return row


def update_feed_post_text(post_id, text):
    conn = get_conn()
    conn.execute("UPDATE feed_posts SET text = ? WHERE id = ?", (text, post_id))
    conn.commit()
    conn.close()


def delete_feed_post(post_id):
    conn = get_conn()
    conn.execute("DELETE FROM feed_posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def delete_expired_feed_posts(today_iso):
    """Тихая ежедневная уборка — посты с истёкшим сроком хранения удаляются
    насовсем, чтобы не копились мёртвые записи."""
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM feed_posts WHERE expires_at IS NOT NULL AND expires_at < ?", (today_iso,)
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def set_feed_post_allow_questions(post_id, allow):
    conn = get_conn()
    conn.execute("UPDATE feed_posts SET allow_questions = ? WHERE id = ?", (1 if allow else 0, post_id))
    conn.commit()
    conn.close()


# ---------- вопросы (под вебинарами и публикациями в архиве) ----------

def add_question(ref_type, ref_id, ref_title, user_id, question_text):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO questions (ref_type, ref_id, ref_title, user_id, question_text, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ref_type, ref_id, ref_title, user_id, question_text, _now()),
    )
    conn.commit()
    q_id = cur.lastrowid
    conn.close()
    return q_id


def get_question(question_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    conn.close()
    return row


def set_question_answer(question_id, answer_text, is_public):
    conn = get_conn()
    conn.execute(
        "UPDATE questions SET answer_text = ?, is_public = ?, answered_at = ? WHERE id = ?",
        (answer_text, 1 if is_public else 0, _now(), question_id),
    )
    conn.commit()
    conn.close()


def get_public_qa(ref_type, ref_id):
    """Только отвеченные И опубликованные вопросы — то, что видно всем под
    конкретным вебинаром/публикацией. Неотвеченные и приватные сюда не попадают."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM questions WHERE ref_type = ? AND ref_id = ? AND is_public = 1 "
        "AND answer_text IS NOT NULL ORDER BY id ASC",
        (ref_type, ref_id),
    ).fetchall()
    conn.close()
    return rows


def count_public_qa(ref_type, ref_id):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE ref_type = ? AND ref_id = ? "
        "AND is_public = 1 AND answer_text IS NOT NULL",
        (ref_type, ref_id),
    ).fetchone()["n"]
    conn.close()
    return n


# ---------- webinars ----------

def add_webinar(title, description, date_text, price, invite_link, event_type="webinar", event_dt=None):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO webinars
           (title, description, date_text, price, invite_link, is_active, created_at, event_type, event_dt)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (title, description, date_text, price, invite_link, _now(), event_type, event_dt),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_active_webinars():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM webinars WHERE is_active = 1 ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def update_webinar_datetime(webinar_id, event_dt_iso, date_text):
    """Обновляет настоящие дату+время (event_dt, для расчёта напоминаний) и
    красивый текст для показа (date_text) ОДНИМ вызовом — они всегда пересчитываются
    вместе из одного источника, поэтому разъехаться между собой не могут."""
    conn = get_conn()
    conn.execute(
        "UPDATE webinars SET event_dt = ?, date_text = ? WHERE id = ?",
        (event_dt_iso, date_text, webinar_id),
    )
    conn.commit()
    conn.close()


def update_webinar_type(webinar_id, event_type):
    assert event_type in ("webinar", "practice", "constellation")
    conn = get_conn()
    conn.execute("UPDATE webinars SET event_type = ? WHERE id = ?", (event_type, webinar_id))
    conn.commit()
    conn.close()


def get_webinars_with_upcoming_event(cutoff_iso):
    """Записи с заполненной настоящей датой (event_dt), которая не раньше cutoff —
    источник данных для системы автоматических напоминаний. is_active сознательно
    не проверяется: уже подтверждённым участникам напоминание должно прийти, даже
    если регистрация на событие тем временем закрыта."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM webinars WHERE event_dt IS NOT NULL AND event_dt >= ?", (cutoff_iso,)
    ).fetchall()
    conn.close()
    return rows


def get_confirmed_registrations_unsent(webinar_id, reminder_column):
    assert reminder_column in ("reminder_5d_sent", "reminder_24h_sent", "reminder_1h_sent")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM registrations WHERE product_type = 'webinar' AND product_id = ? "
        f"AND status = 'confirmed' AND ({reminder_column} IS NULL OR {reminder_column} = 0)",
        (webinar_id,),
    ).fetchall()
    conn.close()
    return rows


def mark_webinar_reminder_sent(reg_id, reminder_column):
    assert reminder_column in ("reminder_5d_sent", "reminder_24h_sent", "reminder_1h_sent")
    conn = get_conn()
    conn.execute(f"UPDATE registrations SET {reminder_column} = 1 WHERE id = ?", (reg_id,))
    conn.commit()
    conn.close()


def get_past_webinars():
    """Выключенные вебинары/практики/расстановки с заполненным названием —
    показываются подписчикам в разделе "Прошедшие" (без цены и регистрации,
    см. main._show_past_webinar), в отличие от get_active_webinars цена тут
    не обязательна, потому что в прошедшем виде она всё равно не показывается."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM webinars WHERE is_active = 0 AND title IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def get_all_webinars():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM webinars ORDER BY id DESC").fetchall()
    conn.close()
    return rows


def get_webinar(webinar_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    conn.close()
    return row


def update_webinar_field(webinar_id, field, value):
    assert field in ("title", "description", "date_text", "price", "invite_link", "photo", "video_link")
    conn = get_conn()
    conn.execute(f"UPDATE webinars SET {field} = ? WHERE id = ?", (value, webinar_id))
    conn.commit()
    conn.close()


def toggle_webinar_active(webinar_id):
    conn = get_conn()
    row = conn.execute("SELECT is_active FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    new_val = 0 if row["is_active"] else 1
    conn.execute("UPDATE webinars SET is_active = ? WHERE id = ?", (new_val, webinar_id))
    conn.commit()
    conn.close()
    return new_val


def toggle_webinar_questions(webinar_id):
    conn = get_conn()
    row = conn.execute("SELECT allow_questions FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    new_val = 0 if row["allow_questions"] else 1
    conn.execute("UPDATE webinars SET allow_questions = ? WHERE id = ?", (new_val, webinar_id))
    conn.commit()
    conn.close()
    return new_val


def delete_webinar(webinar_id):
    conn = get_conn()
    conn.execute("DELETE FROM webinars WHERE id = ?", (webinar_id,))
    conn.commit()
    conn.close()


# ---------- sanctum ----------

def get_sanctum():
    conn = get_conn()
    row = conn.execute("SELECT * FROM sanctum WHERE id = 1").fetchone()
    conn.close()
    return row


def update_sanctum_field(field, value):
    assert field in ("price", "invite_link", "intro_text", "laws_text", "intro_photo")
    conn = get_conn()
    conn.execute(f"UPDATE sanctum SET {field} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()


# ---------- settings ----------

def get_setting(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---------- registrations ----------

def create_registration(user_id, product_type, product_id, product_title, price):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO registrations
           (user_id, product_type, product_id, product_title, price, status, receipt_file_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'awaiting_receipt', NULL, ?, ?)""",
        (user_id, product_type, product_id, product_title, price, _now(), _now()),
    )
    conn.commit()
    reg_id = cur.lastrowid
    conn.close()
    return reg_id


def get_pending_registration(user_id, product_type, product_id):
    """Уже существующая незавершённая заявка этого человека на именно этот
    продукт (ждёт чек или ждёт проверки) — чтобы при повторном нажатии
    "Зарегистрироваться"/"Подать заявку" не плодить дубли и не путать потом
    напоминания. product_id=None — для Sanctum (у него нет отдельного id)."""
    conn = get_conn()
    if product_id is None:
        row = conn.execute(
            "SELECT * FROM registrations WHERE user_id = ? AND product_type = ? AND product_id IS NULL "
            "AND status IN ('awaiting_receipt', 'awaiting_confirmation') ORDER BY id DESC LIMIT 1",
            (user_id, product_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM registrations WHERE user_id = ? AND product_type = ? AND product_id = ? "
            "AND status IN ('awaiting_receipt', 'awaiting_confirmation') ORDER BY id DESC LIMIT 1",
            (user_id, product_type, product_id),
        ).fetchone()
    conn.close()
    return row


def get_awaiting_receipt_for_user(user_id):
    """Незавершённая заявка этого человека, реально ждущая чек (любой
    продукт) — подстраховка для photo_fallback (main.py): если состояние
    ожидания чека было потеряно (например, человек успел ещё раз нажать
    /start между оформлением заявки и отправкой скриншота — /start всегда
    очищает состояние), заявка в базе всё равно показывает, что чек нужен,
    и фото не теряется молча. Реальный случай, поймавший это: 2026-09-06."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM registrations WHERE user_id = ? AND status = 'awaiting_receipt' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def attach_receipt(reg_id, file_id):
    conn = get_conn()
    conn.execute(
        "UPDATE registrations SET receipt_file_id = ?, status = 'awaiting_confirmation', updated_at = ? WHERE id = ?",
        (file_id, _now(), reg_id),
    )
    conn.commit()
    conn.close()


def set_registration_status(reg_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE registrations SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), reg_id),
    )
    conn.commit()
    conn.close()


def get_registration(reg_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
    conn.close()
    return row


def get_user_registrations(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM registrations WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_pending_registrations():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM registrations WHERE status = 'awaiting_confirmation' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return rows


def count_registrations_by_status(status):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM registrations WHERE status = ?", (status,)).fetchone()["c"]
    conn.close()
    return n


def count_stalled_registrations(cutoff_datetime_str):
    """Сколько заявок ждут чек дольше настроенного срока - в отличие от
    get_stalled_registrations (main.py, автонапоминание) считает ВСЕ такие
    заявки, а не только те, кому ещё не отправляли напоминание - для честного
    снимка "сколько сейчас реально висит", а не "кому ещё нужно написать"."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) c FROM registrations WHERE status = 'awaiting_receipt' AND created_at <= ?",
        (cutoff_datetime_str,),
    ).fetchone()["c"]
    conn.close()
    return n


def count_unreviewed_faq_suggestions():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM faq_suggestions WHERE reviewed = 0").fetchone()["c"]
    conn.close()
    return n


def add_webinar_review(webinar_id, text, photo=None):
    """Отзыв, привязанный к конкретному вебинару/практике/расстановке -
    добавляется в любой момент, независимо от того, когда он реально пришёл
    (её решение 2026-09-27: отзывы часто приходят через день-два-три после
    события, а не сразу). Текстом, фото со подписью или просто фото
    (её решение 2026-09-28: много отзывов приходят ей скриншотами)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO webinar_reviews (webinar_id, text, photo, created_at) VALUES (?, ?, ?, ?)",
        (webinar_id, text or "", photo, _now()),
    )
    conn.commit()
    conn.close()


def get_webinar_reviews(webinar_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM webinar_reviews WHERE webinar_id = ? ORDER BY id ASC", (webinar_id,)
    ).fetchall()
    conn.close()
    return rows


def count_webinar_reviews(webinar_id):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM webinar_reviews WHERE webinar_id = ?", (webinar_id,)).fetchone()["c"]
    conn.close()
    return n


def delete_webinar_review(review_id):
    conn = get_conn()
    conn.execute("DELETE FROM webinar_reviews WHERE id = ?", (review_id,))
    conn.commit()
    conn.close()


def get_confirmed_webinar_registrant_ids(webinar_id):
    """Кто реально оплатил именно этот вебинар/практику/расстановку - для
    уведомления о готовой записи (см. adm_wb_video_notify_go, main.py)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM registrations WHERE product_type = 'webinar' "
        "AND product_id = ? AND status = 'confirmed'",
        (webinar_id,),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ---------- sanctum membership (подписка) ----------

def get_sanctum_membership(user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM sanctum_membership WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def set_sanctum_intention(user_id, text):
    """Сохраняет намерение — и при первом написании, и при любом последующем
    редактировании/дополнении (человек может менять текст сколько угодно
    раз). Напоминание больше не завязано на личный день месяца — приходит
    всем в одни и те же даты (см. check_intention_reminders), поэтому здесь
    больше не запоминается intention_day. intention_reviewed сбрасывается в 0
    при каждом сохранении — если она уже отмечала разбор для предыдущего
    текста, новый текст считается ещё не разобранным."""
    conn = get_conn()
    conn.execute(
        "UPDATE sanctum_membership SET intention_text = ?, intention_reviewed = 0 WHERE user_id = ?",
        (text, user_id),
    )
    conn.commit()
    conn.close()


def get_active_intentions():
    """Все намерения, которым сегодня нужно напомнить — сама привязка к датам
    (8 и 22 число) сделана на уровне расписания задачи (main.py), здесь только
    защита от повторной отправки, если задача вдруг сработает дважды за один
    день (intention_last_reminded != сегодняшняя дата)."""
    conn = get_conn()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM sanctum_membership WHERE intention_text IS NOT NULL "
        "AND (status IS NULL OR status != 'removed') "
        "AND (intention_last_reminded IS NULL OR intention_last_reminded != ?)",
        (today_iso,),
    ).fetchall()
    conn.close()
    return rows


def mark_intention_reminded(user_id, date_iso):
    conn = get_conn()
    conn.execute("UPDATE sanctum_membership SET intention_last_reminded = ? WHERE user_id = ?", (date_iso, user_id))
    conn.commit()
    conn.close()


def set_intention_reviewed(user_id, reviewed: bool):
    """Ставится вручную ею через "🕯 Намерения участников", когда она лично
    дала человеку разбор его намерения — убирает кнопку "написать лично" из
    последующих ежемесячных напоминаний (само напоминание продолжает идти)."""
    conn = get_conn()
    conn.execute(
        "UPDATE sanctum_membership SET intention_reviewed = ? WHERE user_id = ?", (1 if reviewed else 0, user_id)
    )
    conn.commit()
    conn.close()


def get_all_intentions_for_admin():
    """Список всех записанных намерений вместе с именем человека — для экрана
    "🕯 Намерения участников", где она видит полный текст и может отметить,
    что разбор дан."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT sm.user_id, sm.intention_text, sm.intention_reviewed, u.preferred_name, u.first_name, u.username
        FROM sanctum_membership sm
        JOIN users u ON u.user_id = sm.user_id
        WHERE sm.intention_text IS NOT NULL AND (sm.status IS NULL OR sm.status != 'removed')
        ORDER BY sm.intention_reviewed ASC, u.preferred_name, u.first_name
        """
    ).fetchall()
    conn.close()
    return rows


def add_faq_suggestion(user_id, question_text):
    """Человек предложил вопрос для раздела «Частые вопросы» - складываем в
    очередь на рассмотрение, НЕ пересылая администраторам лично (её явное
    решение: она сама заглядывает в список, когда удобно, а не получает
    сообщение в чат на каждый присланный вопрос)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO faq_suggestions (user_id, question_text, created_at) VALUES (?, ?, ?)",
        (user_id, question_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_pending_faq_suggestions():
    conn = get_conn()
    rows = conn.execute(
        "SELECT fs.id, fs.user_id, fs.question_text, fs.created_at, "
        "u.username, u.preferred_name, u.first_name "
        "FROM faq_suggestions fs LEFT JOIN users u ON u.user_id = fs.user_id "
        "WHERE fs.reviewed = 0 ORDER BY fs.created_at ASC"
    ).fetchall()
    conn.close()
    return rows


def count_pending_faq_suggestions():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM faq_suggestions WHERE reviewed = 0").fetchone()
    conn.close()
    return row["c"] if row else 0


def mark_faq_suggestion_reviewed(suggestion_id):
    conn = get_conn()
    conn.execute("UPDATE faq_suggestions SET reviewed = 1 WHERE id = ?", (suggestion_id,))
    conn.commit()
    conn.close()


def set_bought_meditation_bot(user_id, value: bool):
    """Отметка "купил(а) VEDA HEALING FLOW" — ставится вручную из панели
    администратора (см. adm_user_meditation_toggle, main.py), так как это
    отдельный, никак технически не связанный бот, и бот-информатор не может
    узнать об оплате там сам. Используется в compute_ascension_level как
    один из двух ускорителей 3-й ступени."""
    conn = get_conn()
    conn.execute("UPDATE users SET bought_meditation_bot = ? WHERE user_id = ?", (1 if value else 0, user_id))
    conn.commit()
    conn.close()


def set_accumulated_days(user_id, days: int):
    """Ручная перезапись накопленного стажа в VEDA SANCTUM (main.py,
    compute_ascension_level) — для случая, когда человека вносят в бота не
    впервые, а он уже реально состоит в Sanctum какое-то время в обход бота
    (например, платил Вам напрямую до того, как бот начал это отслеживать).
    В отличие от upsert_sanctum_membership, НЕ прибавляет, а заменяет
    значение целиком — вызывать только когда администратор сам явно назвал
    итоговое число месяцев/дней, а не при обычном продлении."""
    conn = get_conn()
    conn.execute("UPDATE sanctum_membership SET accumulated_days = ? WHERE user_id = ?", (days, user_id))
    conn.commit()
    conn.close()


def set_luminar_count(user_id, count):
    """Установить число приглашённых-оплативших напрямую - для ручной
    корректировки ранга Люминара администратором (см. adm_luminar_manual,
    main.py), а не через реальные рефералы. count не может быть отрицательным."""
    conn = get_conn()
    conn.execute("UPDATE users SET luminar_count = ? WHERE user_id = ?", (max(0, count), user_id))
    conn.commit()
    conn.close()


def increment_luminar_count(user_id):
    """Засчитывает ещё одного реально вошедшего и оплатившего человека
    пригласившему — возвращает новый счётчик (нужен вызывающей стороне,
    чтобы понять, пересёк ли человек порог ранга Люминара)."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET luminar_count = COALESCE(luminar_count, 0) + 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    row = conn.execute("SELECT luminar_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["luminar_count"] if row else 0


def upsert_sanctum_membership(user_id, valid_until, price):
    # Успешная оплата или ручная выдача всегда означает: подписка активна
    # (снимает статус "удалён", если он был), и любое ранее данное "обещание
    # оплатить" выполнено — сбрасываем его, чтобы не напомнить зря.
    #
    # accumulated_days ("Путь Восхождения", main.py): считаем, сколько НОВЫХ
    # дней добавляет именно этот период, и прибавляем к уже накопленному —
    # если период ещё активен, период начинается на следующий день после
    # старого valid_until (продление, без задвоения уже учтённых дней); если
    # истёк или подписки не было — период начинается сегодня (пропуск/перерыв
    # просто не засчитывается, но раньше накопленное не стирается).
    conn = get_conn()
    existing = conn.execute(
        "SELECT valid_until, accumulated_days FROM sanctum_membership WHERE user_id = ?", (user_id,)
    ).fetchone()
    today = datetime.now().date()
    new_valid = datetime.strptime(valid_until, "%Y-%m-%d").date()
    if existing and existing["valid_until"]:
        old_valid = datetime.strptime(existing["valid_until"], "%Y-%m-%d").date()
        period_start = old_valid + timedelta(days=1) if old_valid >= today else today
    else:
        period_start = today
    added_days = max(0, (new_valid - period_start).days + 1)
    prior_days = (existing["accumulated_days"] if existing and existing["accumulated_days"] else 0)
    total_days = prior_days + added_days

    conn.execute(
        "INSERT INTO sanctum_membership "
        "(user_id, valid_until, price, status, promise_date, promise_reminder_sent_for, accumulated_days, winback_sent, "
        "removed_at, lastchance_sent, stage_reset_done) "
        "VALUES (?, ?, ?, 'active', NULL, NULL, ?, 0, NULL, 0, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET valid_until = excluded.valid_until, price = excluded.price, "
        "status = 'active', promise_date = NULL, promise_reminder_sent_for = NULL, "
        "accumulated_days = excluded.accumulated_days, winback_sent = 0, "
        "removed_at = NULL, lastchance_sent = 0, stage_reset_done = 0, lifetime_free = 0",
        (user_id, valid_until, price, total_days),
    )
    conn.commit()
    conn.close()


LIFETIME_VALID_UNTIL = "9999-12-31"


def grant_lifetime_sanctum(user_id, price):
    """Пожизненный бесплатный доступ (дар Люминара III) - valid_until в далёком
    будущем, lifetime_free=1 только для отображения ("пожизненно" вместо даты).
    accumulated_days НАРОЧНО не пересчитывается через обычную арифметику
    "добавленных дней" (как в upsert_sanctum_membership) - иначе разница между
    сегодня и 9999 годом дала бы астрономическое число дней; просто оставляем
    как было. Снимает статус "убран" и любое обещание оплатить позже, как и
    обычная оплата."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT accumulated_days FROM sanctum_membership WHERE user_id = ?", (user_id,)
    ).fetchone()
    accumulated = existing["accumulated_days"] if existing and existing["accumulated_days"] else 0
    conn.execute(
        "INSERT INTO sanctum_membership "
        "(user_id, valid_until, price, status, promise_date, promise_reminder_sent_for, accumulated_days, "
        "winback_sent, removed_at, lastchance_sent, stage_reset_done, lifetime_free) "
        "VALUES (?, ?, ?, 'active', NULL, NULL, ?, 0, NULL, 0, 0, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET valid_until = excluded.valid_until, price = excluded.price, "
        "status = 'active', promise_date = NULL, promise_reminder_sent_for = NULL, winback_sent = 0, "
        "removed_at = NULL, lastchance_sent = 0, stage_reset_done = 0, lifetime_free = 1",
        (user_id, LIFETIME_VALID_UNTIL, price, accumulated),
    )
    conn.commit()
    conn.close()


def set_sanctum_status(user_id, status):
    assert status in ("active", "removed")
    conn = get_conn()
    if status == "removed":
        # запоминаем дату ухода и заново открываем цепочку "последний шанс" /
        # "обнуление" именно для этого случая
        today_str = datetime.now().strftime("%Y-%m-%d")
        # реальный найденный случай (2026-09-27): у пожизненного доступа (дар
        # Люминара III) valid_until стоит в 9999 году "просто чтобы был активен
        # всегда" - при ручном удалении эта дата иначе так и осталась бы висеть
        # в прошлом статусе "убран", и при следующей оплате арифметика дат
        # (текущая дата + 1 день от 9999-12-31) вызывала бы переполнение и
        # падение бота. Поэтому убрать вручную - значит убрать по-настоящему:
        # valid_until сбрасывается на сегодня, lifetime_free снимается
        row = conn.execute(
            "SELECT lifetime_free FROM sanctum_membership WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and row["lifetime_free"]:
            cur = conn.execute(
                "UPDATE sanctum_membership SET status = 'removed', removed_at = ?, lastchance_sent = 0, "
                "stage_reset_done = 0, valid_until = ?, lifetime_free = 0 WHERE user_id = ?",
                (today_str, today_str, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE sanctum_membership SET status = 'removed', removed_at = ?, lastchance_sent = 0, "
                "stage_reset_done = 0 WHERE user_id = ?",
                (today_str, user_id),
            )
    else:
        cur = conn.execute(
            "UPDATE sanctum_membership SET status = 'active', removed_at = NULL WHERE user_id = ?", (user_id,)
        )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def set_promise_date(user_id, promise_date_iso):
    conn = get_conn()
    conn.execute(
        "INSERT INTO sanctum_membership (user_id, promise_date, promise_reminder_sent_for) VALUES (?, ?, NULL) "
        "ON CONFLICT(user_id) DO UPDATE SET promise_date = excluded.promise_date, promise_reminder_sent_for = NULL",
        (user_id, promise_date_iso),
    )
    conn.commit()
    conn.close()


def get_promises_due_on(target_date):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sanctum_membership WHERE promise_date = ? "
        "AND (status IS NULL OR status != 'removed') "
        "AND ((promise_reminder_sent_for IS NULL) OR (promise_reminder_sent_for != promise_date))",
        (target_date,),
    ).fetchall()
    conn.close()
    return rows


def mark_promise_reminder_sent(user_id, promise_date):
    conn = get_conn()
    conn.execute(
        "UPDATE sanctum_membership SET promise_reminder_sent_for = ? WHERE user_id = ?",
        (promise_date, user_id),
    )
    conn.commit()
    conn.close()


def get_memberships_expiring_on(target_date, reminder_column):
    assert reminder_column in ("reminder_3d_sent_for", "reminder_0d_sent_for")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM sanctum_membership WHERE valid_until = ? "
        f"AND (status IS NULL OR status != 'removed') "
        f"AND (({reminder_column} IS NULL) OR ({reminder_column} != valid_until))",
        (target_date,),
    ).fetchall()
    conn.close()
    return rows


def get_all_sanctum_memberships():
    conn = get_conn()
    rows = conn.execute("""
        SELECT sm.user_id, sm.valid_until, sm.price, sm.status, sm.promise_date, sm.lifetime_free,
               u.username, u.first_name, u.preferred_name
        FROM sanctum_membership sm
        LEFT JOIN users u ON u.user_id = sm.user_id
        ORDER BY sm.valid_until ASC
    """).fetchall()
    conn.close()
    return rows


def mark_reminder_sent(user_id, reminder_column, valid_until):
    assert reminder_column in ("reminder_3d_sent_for", "reminder_0d_sent_for")
    conn = get_conn()
    conn.execute(
        f"UPDATE sanctum_membership SET {reminder_column} = ? WHERE user_id = ?",
        (valid_until, user_id),
    )
    conn.commit()
    conn.close()


# ---------- резервное копирование базы ----------

BACKUP_DIR = "backups"
BACKUP_KEEP_DAYS = 14


def get_latest_backup_age_hours():
    """Возраст (в часах) самой свежей резервной копии — None, если копий ещё
    не было вообще. Используется при старте бота, чтобы досоздать копию,
    если суточное расписание (03:00) её пропустило — типичная причина при
    частых перезапусках бота: обнаружено 2026-09-03, что за две недели
    ежедневная задача НИ РАЗУ не отработала сама, только ручные вызовы."""
    files = glob.glob(os.path.join(BACKUP_DIR, "webinars_*.db"))
    if not files:
        return None
    newest = max(os.path.getmtime(f) for f in files)
    return (datetime.now().timestamp() - newest) / 3600


def backup_database():
    """Создаёт безопасную резервную копию базы через встроенный backup API
    sqlite3 (корректно работает, даже пока бот использует базу — в отличие
    от простого копирования файла) и удаляет копии старше BACKUP_KEEP_DAYS
    дней, чтобы место на диске не забивалось бесконечно. Возвращает путь
    к новому файлу."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    dest_path = os.path.join(BACKUP_DIR, f"webinars_{timestamp}.db")

    src_conn = sqlite3.connect(DB_PATH)
    dest_conn = sqlite3.connect(dest_path)
    with dest_conn:
        src_conn.backup(dest_conn)
    src_conn.close()
    dest_conn.close()

    cutoff = datetime.now() - timedelta(days=BACKUP_KEEP_DAYS)
    for path in glob.glob(os.path.join(BACKUP_DIR, "webinars_*.db")):
        if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            os.remove(path)

    return dest_path
