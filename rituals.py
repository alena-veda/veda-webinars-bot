"""Календарь ритуалов VEDA SANCTUM: данные, работа с базой и сборка текстов.

Даты НЕ считаются "на лету": они лежат в таблице ritual_events, которую можно
править в админ-панели. Первичная таблица (SEED_EVENTS) посчитана 2026-09-21
по эфемеридам NASA JPL DE440 для Киева (50.4501 N, 30.5234 E) и сверена с
Drik Panchang (Киев). Экадаши показаны по смарта-традиции (как у Drik), если
у вайшнавов день другой, это указано в поле "уточнение".
"""
from datetime import datetime, timedelta

import database as db

KINDS = {
    "amavasya": ("🌑", "Амавасья"),
    "purnima": ("🌕", "Пурнима"),
    "ekadashi": ("🕯", "Экадаши"),
    "shivaratri": ("🔱", "Маха-Шиваратри"),
    "navaratri": ("🪷", "Навратри"),
    "equinox": ("⚖️", "Равноденствие"),
    "solstice": ("☀️", "Солнцестояние"),
}

RU_MONTHS_LOWER = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
RU_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# тексты и их подписи для админ-панели; значения по умолчанию - мои черновики,
# Алёна правит их сама (HTML-разметка сохраняется)
TEXT_DEFAULTS = {
    "ritual_teaser_text": (
        "🌙 <b>Календарь ритуалов</b>\n\n"
        "Для участников VEDA SANCTUM есть календарь ритуальных дат: Амавасья и Пурнима "
        "(новолуние и полнолуние), Экадаши, Маха-Шиваратри, Навратри, равноденствия и "
        "солнцестояния.\n\n"
        "Даты выверены по ведическому календарю для Киева. Раз в месяц они приходят "
        "личным сообщением, а по желанию можно включить напоминание в нужный день."
    ),
    "ritual_monthly_intro": "🌙 <b>Календарь ритуалов на {месяц}</b>\n\n{имя}, вот даты, которые стоит отметить:",
    "ritual_monthly_footer": (
        "Время везде киевское. Кнопка «О практиках месяца» коротко объяснит смысл каждого дня."
    ),
    "ritual_day_text": "🌙 {имя}, сегодня в календаре ритуалов:",
    "ritual_announce_text": (
        "🌙 <b>В VEDA SANCTUM появился календарь ритуалов</b>\n\n"
        "{имя}, теперь здесь собраны даты Амавасьи и Пурнимы, Экадаши, Маха-Шиваратри, "
        "Навратри, равноденствий и солнцестояний, выверенные для Киева.\n\n"
        "1-го числа каждого месяца в 11:11 я буду присылать календарь на месяц. "
        "Если хотите, чтобы бот напоминал в сам день, включите «🔔 Напоминать в день». "
        "Календарь всегда можно открыть кнопкой ниже."
    ),
    "ritual_paid_line": (
        "🌙 Для участников есть календарь ритуалов: даты новолуний, полнолуний, экадаши и больших "
        "праздников года. Откройте его кнопкой ниже."
    ),
    "ritual_sanctum_line": (
        "🌙 Внутри Вас ждёт календарь ритуалов: даты новолуний, полнолуний, экадаши и больших "
        "праздников года, с напоминаниями."
    ),
    "ritual_meaning_amavasya": (
        "Новолуние. Время тишины, завершения и очищения: отпустить лишнее, задать себе внутренний "
        "вопрос, посеять новое намерение."
    ),
    "ritual_meaning_purnima": (
        "Полнолуние. Пик лунной энергии: время благодарности, завершения цикла и проявления того, "
        "что созревало."
    ),
    "ritual_meaning_ekadashi": (
        "Одиннадцатый лунный день. День облегчения тела и ума: лёгкая пища или пост по самочувствию, "
        "больше тишины, мантры и молитвы."
    ),
    "ritual_meaning_shivaratri": (
        "Великая ночь Шивы. Ночь бодрствования, медитации и мантры, время внутреннего пробуждения."
    ),
    "ritual_meaning_navaratri": (
        "Девять ночей поклонения Божественной Матери. Время очищения, дисциплины и внутренней силы."
    ),
    "ritual_meaning_equinox": (
        "День и ночь равны. Точка равновесия года: хорошее время выровнять внутренние ритмы."
    ),
    "ritual_meaning_solstice": (
        "Поворот солнца. Самая длинная или самая короткая ночь года, время подвести итоги "
        "и задать направление на следующий цикл."
    ),
    # практика по умолчанию пуста - показывается в сообщениях, только если Алёна её заполнит;
    # действует сразу на ВСЕ даты этого типа (не нужно вносить в каждый месяц отдельно),
    # а для отдельной даты всегда можно задать свою практику поверх общей (см. practice_of)
    "ritual_practice_amavasya": "",
    "ritual_practice_purnima": "",
    "ritual_practice_ekadashi": "",
    "ritual_practice_shivaratri": "",
    "ritual_practice_navaratri": "",
    "ritual_practice_equinox": "",
    "ritual_practice_solstice": "",
    # экран календаря на 2 месяца вперёд (кнопка "🌙" из профиля/Инфо) - аудит 2026-09-28
    "ritual_calendar_header_text": "🌙 <b>Календарь ритуалов</b>",
    "ritual_month_empty_text": "даты скоро появятся",
    "ritual_kyiv_time_text": "Время везде киевское.",
    "ritual_members_only_text": "Этот раздел для участников VEDA SANCTUM",
    "ritual_month_no_dates_text": "На этот месяц пока нет дат",
    "ritual_reminder_on_text": "Напоминания включены: в день события утром придёт сообщение 🔔",
    "ritual_reminder_off_text": "Напоминания выключены 🔕",
    "ritual_optout_on_text": (
        "Хорошо, календарь 1-го числа больше не пришлю. Открыть его всегда можно в «Инфо» и в профиле."
    ),
    "ritual_optout_off_text": "Календарь снова будет приходить 1-го числа каждого месяца ✅",
    "ritual_mantra_amavasya": "",
    "ritual_mantra_purnima": "",
    "ritual_mantra_ekadashi": "",
    "ritual_mantra_shivaratri": "",
    "ritual_mantra_navaratri": "",
    "ritual_mantra_equinox": "",
    "ritual_mantra_solstice": "",
}

TEXT_LABELS = {
    "ritual_teaser_text": "текст для тех, кто НЕ в Sanctum - вместо самого календаря. Приходит в двух случаях: сам открыл «Инфо» → «Календарь ритуалов», или одноразовое автонапоминание, если видел кнопку календаря, но ни разу не нажал. Кнопки под ним: «⚜️ Что такое VEDA SANCTUM» и «Оформить подписку»",
    "ritual_monthly_intro": "начало ежемесячного сообщения - приходит САМО 1-го числа каждого месяца в 11:11 всем участникам (метки {имя}, {месяц}). Кнопки под всем сообщением (общие с «концом» ниже): «📖 О практиках месяца», «🔔/🔕 Напоминать в день», «🔔/🔕 Присылать календарь»",
    "ritual_monthly_footer": "конец того же ежемесячного сообщения, сразу после списка дат - кнопки те же, что у начала выше. Само сообщение: начало, список дат с личной фразой каждого праздника и этот конец (практик в нём нет, они приходят утром в день события и по кнопке «📖 О практиках месяца»)",
    "ritual_day_text": "начало напоминания В ДЕНЬ события - отдельная, более короткая рассылка, приходит только тем, кто включил 🔔 (метка {имя}). Кнопка под ним: «🌙 Календарь ритуалов»",
    "ritual_announce_text": "разовый анонс календаря участникам - отдельный, одноразовый текст, не часть ежемесячной цепочки выше (метка {имя}). Кнопка под ним: «🌙 Открыть календарь ритуалов»",
    "ritual_paid_line": "строка в сообщении «оплата подтверждена» для новых участников - добавляется только при первой оплате Sanctum; вместе с ней появляется кнопка «🌙 Календарь ритуалов» (которой иначе в этом сообщении не было бы)",
    "ritual_sanctum_line": "строка про календарь в экране «Войти в глубину»",
    "ritual_meaning_amavasya": "смысл Амавасьи (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_purnima": "смысл Пурнимы (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_ekadashi": "общий смысл Экадаши (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_shivaratri": "смысл Маха-Шиваратри (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_navaratri": "общий смысл Навратри (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_equinox": "смысл равноденствия (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_meaning_solstice": "смысл солнцестояния (на все даты сразу). Показывается под личной фразой дня; «-» очищает",
    "ritual_practice_amavasya": "практика на Амавасью (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_purnima": "практика на Пурниму (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_ekadashi": "практика на Экадаши (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_shivaratri": "практика на Маха-Шиваратри (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_navaratri": "практика на Навратри (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_equinox": "практика на равноденствие (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_practice_solstice": "практика на солнцестояние (на все даты сразу). Если у даты своей практики нет, показывается эта; «-» очищает",
    "ritual_calendar_header_text": "заголовок экрана календаря на 2 месяца вперёд - под этим экраном (вместе с «месяц пуст» и «часовой пояс» ниже) кнопки: «📖 О практиках месяца», «🔔/🔕 Напоминать в день», «🔔/🔕 Присылать календарь»",
    "ritual_month_empty_text": "если в одном из двух месяцев пока нет дат",
    "ritual_kyiv_time_text": "подпись под двухмесячным календарём про часовой пояс",
    "ritual_members_only_text": "если не участник Sanctum пытается открыть детали/переключатели",
    "ritual_month_no_dates_text": "«О практиках месяца» - если в этом месяце дат нет",
    "ritual_reminder_on_text": "подсказка при включении напоминания на день события",
    "ritual_reminder_off_text": "подсказка при выключении напоминания на день события",
    "ritual_optout_on_text": "подсказка при отключении ежемесячной рассылки календаря",
    "ritual_optout_off_text": "подсказка при включении ежемесячной рассылки календаря обратно",
    "ritual_mantra_amavasya": "ссылка на мантру для Амавасьи (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_purnima": "ссылка на мантру для Пурнимы (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_ekadashi": "ссылка на мантру для Экадаши (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_shivaratri": "ссылка на мантру для Маха-Шиваратри (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_navaratri": "ссылка на мантру для Навратри (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_equinox": "ссылка на мантру для равноденствия (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
    "ritual_mantra_solstice": "ссылка на мантру для солнцестояния (на все даты сразу). Ссылка с https://, например с YouTube; «-» убирает",
}

# одноразовое напоминание тем, кто увидел кнопку календаря (например, на
# "Первом Касании"), но ни разу её не открыл - её решение 2026-09-22: не
# рассчитывать только на то, что человек сам зайдёт в "Инфо" или в профиль
NUDGE_DEFAULTS = {
    "ritual_nudge_enabled": "1",
    "ritual_nudge_days": "3",
}

# ---------- начальная таблица (одобрена Алёной 2026-09-21) ----------

_EK = {
    "Индира": "Экадаши в память о предках: помогает облегчить тяжесть рода.",
    "Папанкуша": "«Крюк для грехов»: день очищения от накопленных ошибок и тяжёлых следов.",
    "Рама": "Экадаши месяца Картика, связан с почитанием Лакшми. Приносит умиротворение и очищение.",
    "Девутхана": "Пробуждение Вишну после четырёх месяцев космического сна. Начало благоприятного времени.",
    "Утпанна": "День явления самой Экадаши. С него традиционно начинают практику экадаши-поста.",
    "Мокшада": "«Дарующая освобождение». Совпадает с днём, когда была явлена Бхагавад-гита.",
    "Сафала": "«Приносящая успех»: день, чтобы поставить намерение на удачное завершение начатого.",
    "Пауша Путрада": "Экадаши месяца Пауша, в традиции связан с благополучием потомков и рода.",
    "Шаттила": "Связан с практикой дарения (особенно чёрного кунжута) и очищением.",
    "Джая": "«Победа»: экадаши о преодолении внутренних препятствий.",
    "Виджая": "«Победа»: поддержка в преодолении трудностей и важных начинаниях.",
    "Амалаки": "Связан с почитанием дерева амла (индийский крыжовник), символом здоровья и очищения.",
    "Папамочани": "«Освобождающая от ошибок»: последний экадаши перед началом нового лунного года.",
    "Камада": "«Исполняющая желания»: первый экадаши нового лунного года.",
    "Варутхини": "«Защищающая»: экадаши о защите и благополучии, ценится практика дарения.",
    "Мохини": "Связан с образом Мохини, в котором Вишну разрушает иллюзии.",
    "Апара": "«Безграничная»: очищает и приумножает накопленные добрые заслуги.",
    "Нирджала": "Самый строгий экадаши (без воды), по традиции равен всем остальным. Практика только по здоровью и самочувствию.",
    "Йогини": "«Исцеляющая»: экадаши очищения тела и восстановления.",
    "Девшаяни": "Начало Чатурмасьи: Вишну уходит в космический сон на четыре месяца.",
    "Камика": "Экадаши месяца Шравана, время поклонения и чистых намерений.",
    "Шравана Путрада": "Связан с благополучием детей и продолжением рода.",
    "Аджа": "«Нерождённая»: освобождает от последствий прошлых ошибок.",
    "Парсва": "Вишну во сне поворачивается на другой бок: середина Чатурмасьи, время внутреннего поворота.",
}

_NAV = {
    "Чайтра": "Весенние девять ночей Божественной Матери в начале нового лунного года. Завершаются Рама Навами.",
    "Шарад": "Осенние девять ночей, самые почитаемые. Завершаются Виджаядашами, днём победы света.",
}

# (дата, тип, название, уточнение, смысл-исключение)
_RAW = [
    ("2026-09-22", "ekadashi", "Парсва", ""),
    ("2026-09-23", "equinox", "Осеннее равноденствие", "в 03:05 по Киеву"),
    ("2026-09-26", "purnima", "", "полнолуние в 19:49"),
    ("2026-10-06", "ekadashi", "Индира", ""),
    ("2026-10-10", "amavasya", "", "новолуние в 18:50"),
    ("2026-10-11", "navaratri", "Шарад", "11.10 - 19.10, Виджаядашами 20.10"),
    ("2026-10-22", "ekadashi", "Папанкуша", ""),
    ("2026-10-25", "purnima", "", "полнолуние 26.10 в 06:11"),
    ("2026-11-05", "ekadashi", "Рама", ""),
    ("2026-11-08", "amavasya", "", "новолуние 09.11 в 09:02"),
    ("2026-11-20", "ekadashi", "Девутхана", ""),
    ("2026-11-24", "purnima", "", "полнолуние в 16:53"),
    ("2026-12-04", "ekadashi", "Утпанна", ""),
    ("2026-12-08", "amavasya", "", "новолуние 09.12 в 02:51"),
    ("2026-12-20", "ekadashi", "Мокшада", ""),
    ("2026-12-21", "solstice", "Зимнее солнцестояние", "в 22:50 по Киеву"),
    ("2026-12-23", "purnima", "", "полнолуние 24.12 в 03:28"),
    ("2027-01-03", "ekadashi", "Сафала", ""),
    ("2027-01-07", "amavasya", "", "новолуние в 22:24"),
    ("2027-01-18", "ekadashi", "Пауша Путрада", "у вайшнавов: 19.01"),
    ("2027-01-22", "purnima", "", "полнолуние в 14:17"),
    ("2027-02-02", "ekadashi", "Шаттила", ""),
    ("2027-02-06", "amavasya", "", "новолуние в 17:56"),
    ("2027-02-17", "ekadashi", "Джая", ""),
    ("2027-02-20", "purnima", "", "полнолуние 21.02 в 01:23"),
    ("2027-03-03", "ekadashi", "Виджая", ""),
    ("2027-03-06", "shivaratri", "", ""),
    ("2027-03-07", "amavasya", "", "новолуние 08.03 в 11:29"),
    ("2027-03-18", "ekadashi", "Амалаки", ""),
    ("2027-03-20", "equinox", "Весеннее равноденствие", "в 22:24 по Киеву"),
    ("2027-03-22", "purnima", "", "полнолуние в 12:43"),
    ("2027-04-02", "ekadashi", "Папамочани", ""),
    ("2027-04-06", "amavasya", "", "новолуние 07.04 в 02:51"),
    ("2027-04-07", "navaratri", "Чайтра", "07.04 - 15.04, Рама Навами 15.04"),
    ("2027-04-16", "ekadashi", "Камада", "у вайшнавов: 17.04"),
    ("2027-04-20", "purnima", "", "полнолуние 21.04 в 01:27"),
    ("2027-05-02", "ekadashi", "Варутхини", ""),
    ("2027-05-06", "amavasya", "", "новолуние в 13:58"),
    ("2027-05-16", "ekadashi", "Мохини", ""),
    ("2027-05-20", "purnima", "", "полнолуние в 13:59"),
    ("2027-06-01", "ekadashi", "Апара", ""),
    ("2027-06-04", "amavasya", "", "новолуние в 22:40"),
    ("2027-06-14", "ekadashi", "Нирджала", ""),
    ("2027-06-18", "purnima", "", "полнолуние 19.06 в 03:44"),
    ("2027-06-21", "solstice", "Летнее солнцестояние", "в 17:10 по Киеву"),
    ("2027-06-30", "ekadashi", "Йогини", ""),
    ("2027-07-03", "amavasya", "", "новолуние 04.07 в 06:02"),
    ("2027-07-14", "ekadashi", "Девшаяни", ""),
    ("2027-07-18", "purnima", "", "полнолуние в 18:44"),
    ("2027-07-29", "ekadashi", "Камика", "у вайшнавов: 30.07"),
    ("2027-08-02", "amavasya", "", "новолуние в 13:05"),
    ("2027-08-12", "ekadashi", "Шравана Путрада", ""),
    ("2027-08-16", "purnima", "", "полнолуние 17.08 в 10:28"),
    ("2027-08-28", "ekadashi", "Аджа", ""),
    ("2027-08-31", "amavasya", "", "новолуние в 20:41"),
    ("2027-09-11", "ekadashi", "Парсва", ""),
    ("2027-09-15", "purnima", "", "полнолуние 16.09 в 02:03"),
    ("2027-09-23", "equinox", "Осеннее равноденствие", "в 09:01 по Киеву"),
    ("2027-09-26", "ekadashi", "Индира", ""),
    ("2027-09-29", "amavasya", "", "новолуние 30.09 в 05:36"),
    ("2027-09-30", "navaratri", "Шарад", "30.09 - 08.10, Виджаядашами 09.10"),
    ("2027-10-11", "ekadashi", "Папанкуша", ""),
    ("2027-10-15", "purnima", "", "полнолуние в 16:47"),
    ("2027-10-25", "ekadashi", "Рама", ""),
    ("2027-10-29", "amavasya", "", "новолуние в 16:36"),
    ("2027-11-09", "ekadashi", "Девутхана", ""),
    ("2027-11-13", "purnima", "", "полнолуние 14.11 в 05:25"),
    ("2027-11-23", "ekadashi", "Утпанна", "у вайшнавов: 24.11"),
    ("2027-11-27", "amavasya", "", "новолуние 28.11 в 05:24"),
    ("2027-12-09", "ekadashi", "Мокшада", ""),
    ("2027-12-13", "purnima", "", "полнолуние в 18:08"),
    ("2027-12-22", "solstice", "Зимнее солнцестояние", "в 04:42 по Киеву"),
    ("2027-12-23", "ekadashi", "Сафала", ""),
    ("2027-12-27", "amavasya", "", "новолуние в 22:12"),
]


def _seed_rows():
    rows = []
    for d, kind, name, detail in _RAW:
        meaning = ""
        if kind == "ekadashi":
            title = f"Экадаши {name}"
            meaning = _EK[name]
        elif kind == "navaratri":
            title = f"{name} Навратри"
            meaning = _NAV[name]
        elif name:
            title = name
        else:
            title = KINDS[kind][1]
        rows.append((d, kind, title, detail, meaning))
    return rows


def init_rituals():
    """Создаёт таблицу, колонки у users, тексты по умолчанию и (один раз)
    начальные даты. Безопасно вызывать при каждом старте."""
    conn = db.get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ritual_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            meaning TEXT DEFAULT '',
            practice TEXT DEFAULT ''
        )
    """)
    try:
        c.execute("ALTER TABLE ritual_events ADD COLUMN mantra TEXT DEFAULT ''")
    except Exception:
        pass
    for col in ("ritual_optout INTEGER DEFAULT 0", "ritual_reminders INTEGER DEFAULT 0",
                "ritual_last_month TEXT", "ritual_last_day TEXT",
                # для одноразового напоминания тем, кто увидел кнопку календаря
                # (на "Первом Касании" и т.д.), но ни разу её не открыл (см.
                # mark_ritual_intro_shown/mark_ritual_opened, check_reengagement в main.py)
                "ritual_intro_shown_at TEXT", "ritual_opened_at TEXT",
                "ritual_nudge_sent INTEGER DEFAULT 0"):
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except Exception:
            pass
    for key, value in TEXT_DEFAULTS.items():
        if not c.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    for key, value in NUDGE_DEFAULTS.items():
        if not c.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    if not c.execute("SELECT 1 FROM settings WHERE key = 'ritual_seeded'").fetchone():
        c.executemany(
            "INSERT INTO ritual_events (event_date, kind, title, detail, meaning) VALUES (?, ?, ?, ?, ?)",
            _seed_rows(),
        )
        c.execute("INSERT INTO settings (key, value) VALUES ('ritual_seeded', '1')")
    conn.commit()
    conn.close()


# ---------- события ----------

def events_between(start_iso, end_iso):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM ritual_events WHERE event_date >= ? AND event_date <= ? ORDER BY event_date, id",
        (start_iso, end_iso),
    ).fetchall()
    conn.close()
    return rows


def events_of_month(ym):
    return events_between(f"{ym}-01", f"{ym}-31")


def events_on(day_iso):
    return events_between(day_iso, day_iso)


def get_event(event_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM ritual_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return row


def months_with_counts():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT substr(event_date, 1, 7) AS ym, COUNT(*) AS n FROM ritual_events GROUP BY ym ORDER BY ym"
    ).fetchall()
    conn.close()
    return rows


def add_event(event_date, kind, title, detail="", meaning="", practice=""):
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO ritual_events (event_date, kind, title, detail, meaning, practice) VALUES (?, ?, ?, ?, ?, ?)",
        (event_date, kind, title, detail, meaning, practice),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


EVENT_FIELDS = {"event_date", "title", "detail", "meaning", "practice", "mantra"}


def update_event(event_id, field, value):
    if field not in EVENT_FIELDS:
        raise ValueError(field)
    conn = db.get_conn()
    conn.execute(f"UPDATE ritual_events SET {field} = ? WHERE id = ?", (value, event_id))
    conn.commit()
    conn.close()


def same_title_dates(event_id):
    """Даты ДРУГИХ праздников с точно таким же названием (например, «Экадаши Индира» в разные годы)."""
    e = get_event(event_id)
    if not e:
        return []
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT event_date FROM ritual_events WHERE title = ? AND id != ? ORDER BY event_date", (e["title"], event_id)
    ).fetchall()
    conn.close()
    return [r["event_date"] for r in rows]


def copy_to_same_title(event_id):
    """Переносит личную фразу, практику и мантру этой даты на все другие даты с тем же названием."""
    e = get_event(event_id)
    if not e:
        return 0
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE ritual_events SET meaning = ?, practice = ?, mantra = ? WHERE title = ? AND id != ?",
        (e["meaning"] or "", e["practice"] or "", e["mantra"] or "", e["title"], event_id),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def delete_event(event_id):
    conn = db.get_conn()
    conn.execute("DELETE FROM ritual_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


# ---------- люди ----------

def set_user_flag(user_id, field, value):
    if field not in ("ritual_optout", "ritual_reminders", "ritual_last_month", "ritual_last_day"):
        raise ValueError(field)
    conn = db.get_conn()
    conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()


def mark_ritual_intro_shown(user_id):
    """Момент, когда человеку впервые показали кнопку календаря (например, на
    "Первом Касании") - основа для одноразового напоминания ниже. Не
    перезаписывается повторно, если вдруг уже был проставлен раньше."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE users SET ritual_intro_shown_at = ? WHERE user_id = ? AND ritual_intro_shown_at IS NULL",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    conn.commit()
    conn.close()


def mark_ritual_opened(user_id):
    """Человек сам открыл календарь (с любого места) - напоминание про "не
    открыл ни разу" ему больше не нужно."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE users SET ritual_opened_at = ? WHERE user_id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    conn.commit()
    conn.close()


def mark_ritual_nudge_sent(user_id):
    conn = db.get_conn()
    conn.execute("UPDATE users SET ritual_nudge_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_users_due_for_ritual_nudge(days):
    """Кому показали кнопку календаря давно (>= days дней назад), но человек
    её ни разу не открыл, и напоминание ему ещё не отправлялось."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE ritual_intro_shown_at IS NOT NULL AND ritual_intro_shown_at <= ? "
        "AND ritual_opened_at IS NULL AND (ritual_nudge_sent IS NULL OR ritual_nudge_sent = 0) "
        "AND (blocked IS NULL OR blocked = 0) AND (self_departed IS NULL OR self_departed = 0)",
        (cutoff,),
    ).fetchall()
    conn.close()
    return rows


def active_members():
    """Все действующие участники Санктума (не удалённые, срок не истёк) с их
    настройками календаря."""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT u.* FROM users u JOIN sanctum_membership m ON m.user_id = u.user_id "
        "WHERE m.valid_until >= ? AND (m.status IS NULL OR m.status != 'removed') "
        "AND (u.blocked IS NULL OR u.blocked = 0)",
        (today_iso,),
    ).fetchall()
    conn.close()
    return rows


def stats():
    members = active_members()
    return {
        "members": len(members),
        "reminders": sum(1 for u in members if u["ritual_reminders"]),
        "optout": sum(1 for u in members if u["ritual_optout"]),
    }


# ---------- сборка текстов ----------

def fmt_line(ev, with_year=False):
    d = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
    emoji = KINDS.get(ev["kind"], ("•", ""))[0]
    date_s = d.strftime("%d.%m.%Y" if with_year else "%d.%m")
    line = f"{emoji} {date_s} ({RU_WEEKDAYS[d.weekday()]}) - {ev['title']}"
    if ev["detail"]:
        line += f" ({ev['detail']})"
    return line


def month_title(ym):
    y, m = ym.split("-")
    return f"{RU_MONTHS_LOWER[int(m) - 1]} {y}"


def personal_phrase(ev):
    """Личная фраза дня без служебного знака «!»."""
    raw = (ev["meaning"] or "").strip()
    return raw[1:].lstrip() if raw.startswith("!") else raw


def meaning_of(ev):
    """Смысл дня: сверху личная фраза именно этой даты, под ней общий текст типа события.
    Если личная фраза начинается с «!», показывается только она (без общего текста)."""
    raw = (ev["meaning"] or "").strip()
    exclusive = raw.startswith("!")
    personal = personal_phrase(ev)
    general = "" if exclusive else (db.get_setting(f"ritual_meaning_{ev['kind']}") or "").strip()
    return "\n\n".join(p for p in (personal, general) if p)


def mantra_of(ev):
    """Ссылка на мантру: своя у даты, а если её нет, общая для типа события."""
    own = (ev["mantra"] or "").strip() if "mantra" in ev.keys() else ""
    return own or (db.get_setting(f"ritual_mantra_{ev['kind']}") or "").strip()


def practice_of(ev):
    """Практика для конкретной даты - если для НЕЁ САМОЙ ничего не задано, берёт общую
    практику для этого типа события (одна и та же для всех Экадаши, всех Амавасий и т.д.) -
    заполнять на каждый месяц заново не нужно, только если для какой-то даты нужна особая."""
    return (ev["practice"] or "").strip() or (db.get_setting(f"ritual_practice_{ev['kind']}") or "").strip()


def month_text(ym, name=None):
    """Ежемесячное сообщение: вступление, список дат с личной фразой каждого
    праздника, подпись. Практики в него не входят: они приходят утром в день
    события и по кнопке «О практиках месяца». None, если на месяц нет дат."""
    evs = events_of_month(ym)
    if not evs:
        return None
    intro = (db.get_setting("ritual_monthly_intro") or "").replace("{месяц}", month_title(ym))
    intro = intro.replace("{имя}", name or "друг")
    blocks = []
    for e in evs:
        line = fmt_line(e)
        phrase = personal_phrase(e)
        blocks.append(f"{line}\n{phrase}" if phrase else line)
    parts = [intro, "\n\n".join(blocks)]
    footer = db.get_setting("ritual_monthly_footer")
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def about_blocks(ym):
    """По одному блоку на каждый праздник месяца: название и дата, смысл (личная
    фраза и общий текст), практика. Для кнопки «О практиках месяца»."""
    blocks = []
    for e in events_of_month(ym):
        d = datetime.strptime(e["event_date"], "%Y-%m-%d").strftime("%d.%m")
        emoji = KINDS.get(e["kind"], ("•", ""))[0]
        title = e["title"] + (f" ({e['detail']})" if e["detail"] else "")
        text = f"{emoji} <b>{d} - {title}</b>"
        meaning = meaning_of(e)
        if meaning:
            text += f"\n\n{meaning}"
        practice = practice_of(e)
        if practice:
            text += f"\n\n{practice}"
        blocks.append({"text": text, "mantra": mantra_of(e)})
    return blocks


def about_text(ym):
    """Все блоки «О практиках месяца» одним текстом (для проверок)."""
    blocks = about_blocks(ym)
    if not blocks:
        return None
    return "\n\n".join(b["text"] for b in blocks)
