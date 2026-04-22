import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
import telebot
from telebot import apihelper
from telebot import types
from requests.exceptions import ConnectTimeout, ReadTimeout
from requests.exceptions import ConnectionError as RequestsConnectionError

from current_api import CurrencyApiError, convert_currency
import database as db

load_dotenv()

# Таймауты HTTP к api.telegram.org (по умолчанию в pyTelegramBotAPI часто мало для медленных сетей / прокси).
apihelper.CONNECT_TIMEOUT = int(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "45"))
apihelper.READ_TIMEOUT = int(os.getenv("TELEGRAM_READ_TIMEOUT", "120"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

bot = telebot.TeleBot(BOT_TOKEN)
db.init_db()


COUNTRY_TO_CURRENCY = {
    "russia": "RUB",
    "россия": "RUB",
    "italy": "EUR",
    "италия": "EUR",
    "china": "CNY",
    "китай": "CNY",
    "usa": "USD",
    "сша": "USD",
    "japan": "JPY",
    "япония": "JPY",
    "uk": "GBP",
    "great britain": "GBP",
    "великобритания": "GBP",
}


@dataclass
class DraftTrip:
    from_country: Optional[str] = None
    to_country: Optional[str] = None
    home_currency: Optional[str] = None
    travel_currency: Optional[str] = None
    rate: Optional[float] = None


drafts: dict[int, DraftTrip] = {}
pending_expense: dict[int, float] = {}
pending_rate: set[int] = set()
last_menu_message_id_by_chat: dict[int, int] = {}


def main_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Создать новое путешествие", callback_data="newtrip"),
        types.InlineKeyboardButton("Мои путешествия", callback_data="mytrips"),
        types.InlineKeyboardButton("Баланс", callback_data="balance"),
        types.InlineKeyboardButton("Добавить расход", callback_data="addexpense"),
        types.InlineKeyboardButton("История расходов", callback_data="history"),
        types.InlineKeyboardButton("Изменить курс", callback_data="setrate"),
    )
    return kb


def parse_currency(country: str) -> Optional[str]:
    return COUNTRY_TO_CURRENCY.get(country.strip().lower())


def format_balance(trip: dict) -> str:
    return (
        f"Остаток: {trip['balance_travel']:.2f} {trip['travel_currency']} = "
        f"{trip['balance_home']:.2f} {trip['home_currency']}"
    )


def get_active_trip_dict(user_id: int) -> Optional[dict]:
    return db.row_to_dict(db.get_active_trip(user_id))


def clear_previous_menu(chat_id: int):
    last_message_id = last_menu_message_id_by_chat.get(chat_id)
    if not last_message_id:
        return
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=last_message_id, reply_markup=None)
    except Exception:
        # Сообщение могло быть удалено/изменено или уже без клавиатуры — пропускаем.
        pass


def send_menu_message(chat_id: int, text: str):
    clear_previous_menu(chat_id)
    sent = bot.send_message(chat_id, text, reply_markup=main_menu())
    last_menu_message_id_by_chat[chat_id] = sent.message_id


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    send_menu_message(
        message.chat.id,
        "Я финансовый помощник для путешествий.\n"
        "Создаю кошельки, считаю курс и веду историю трат.",
    )


@bot.message_handler(commands=["newtrip"])
def command_newtrip(message: types.Message):
    start_trip_flow(message.chat.id, message.from_user.id)


def start_trip_flow(chat_id: int, user_id: int):
    clear_previous_menu(chat_id)
    drafts[user_id] = DraftTrip()
    msg = bot.send_message(chat_id, "Введите страну отправления (например: Россия):")
    bot.register_next_step_handler(msg, ask_to_country, user_id)


def ask_to_country(message: types.Message, user_id: int):
    home_currency = parse_currency(message.text or "")
    if not home_currency:
        msg = bot.send_message(message.chat.id, "Не знаю валюту этой страны. Попробуйте снова:")
        bot.register_next_step_handler(msg, ask_to_country, user_id)
        return
    draft = drafts.setdefault(user_id, DraftTrip())
    draft.from_country = message.text.strip()
    draft.home_currency = home_currency
    msg = bot.send_message(message.chat.id, "Введите страну назначения (например: Китай):")
    bot.register_next_step_handler(msg, ask_initial_amount, user_id)


def ask_initial_amount(message: types.Message, user_id: int):
    travel_currency = parse_currency(message.text or "")
    if not travel_currency:
        msg = bot.send_message(message.chat.id, "Не знаю валюту этой страны. Попробуйте снова:")
        bot.register_next_step_handler(msg, ask_initial_amount, user_id)
        return

    draft = drafts[user_id]
    draft.to_country = message.text.strip()
    draft.travel_currency = travel_currency

    try:
        api_data = convert_currency(1, draft.home_currency, draft.travel_currency)
        draft.rate = float(api_data["result"])
    except (CurrencyApiError, KeyError, ValueError):
        msg = bot.send_message(
            message.chat.id,
            "Не удалось получить курс из API. Введите курс вручную (сколько домашней валюты за 1 единицу валюты поездки):",
        )
        pending_rate.add(user_id)
        bot.register_next_step_handler(msg, manual_rate_then_amount, user_id)
        return

    bot.send_message(
        message.chat.id,
        f"Текущий курс: 1 {draft.travel_currency} = {draft.rate:.4f} {draft.home_currency}\n"
        "Если хотите изменить курс, отправьте /setrate после создания путешествия.",
    )
    msg = bot.send_message(message.chat.id, f"Введите стартовую сумму в {draft.home_currency}:")
    bot.register_next_step_handler(msg, finish_trip_creation, user_id)


def manual_rate_then_amount(message: types.Message, user_id: int):
    text = (message.text or "").replace(",", ".")
    try:
        rate = float(text)
        if rate <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "Курс должен быть положительным числом. Введите снова:")
        bot.register_next_step_handler(msg, manual_rate_then_amount, user_id)
        return
    draft = drafts[user_id]
    draft.rate = rate
    pending_rate.discard(user_id)
    msg = bot.send_message(message.chat.id, f"Введите стартовую сумму в {draft.home_currency}:")
    bot.register_next_step_handler(msg, finish_trip_creation, user_id)


def finish_trip_creation(message: types.Message, user_id: int):
    text = (message.text or "").replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "Сумма должна быть положительным числом. Введите снова:")
        bot.register_next_step_handler(msg, finish_trip_creation, user_id)
        return

    draft = drafts[user_id]
    trip_id = db.create_trip(
        user_id=user_id,
        name=f"{draft.from_country} -> {draft.to_country}",
        from_country=draft.from_country or "",
        to_country=draft.to_country or "",
        home_currency=draft.home_currency or "USD",
        travel_currency=draft.travel_currency or "EUR",
        rate=float(draft.rate or 1),
        initial_home_amount=amount,
    )
    trip = db.row_to_dict(db.get_trip_by_id(trip_id, user_id))
    send_menu_message(message.chat.id, f"Путешествие создано: {trip['name']}\n{format_balance(trip)}")
    drafts.pop(user_id, None)


@bot.message_handler(commands=["switch"])
def command_switch(message: types.Message):
    trips = db.get_trips(message.from_user.id)
    if not trips:
        send_menu_message(message.chat.id, "Путешествий пока нет.")
        return
    kb = types.InlineKeyboardMarkup()
    for trip in trips:
        kb.add(types.InlineKeyboardButton(trip["name"], callback_data=f"switch:{trip['id']}"))
    bot.send_message(message.chat.id, "Выберите активное путешествие:", reply_markup=kb)


@bot.message_handler(commands=["balance"])
def command_balance(message: types.Message):
    send_balance(chat_id=message.chat.id, user_id=message.from_user.id)


def send_balance(chat_id: int, user_id: int):
    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(chat_id, "Нет активного путешествия. Создайте его через меню.")
        return
    send_menu_message(chat_id, format_balance(trip))


@bot.message_handler(commands=["history"])
def command_history(message: types.Message):
    send_history(chat_id=message.chat.id, user_id=message.from_user.id)


def send_history(chat_id: int, user_id: int):
    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(chat_id, "Нет активного путешествия.")
        return
    expenses = db.get_expenses(trip["id"], limit=10)
    if not expenses:
        send_menu_message(chat_id, "История расходов пуста.")
        return
    lines = ["Последние расходы:"]
    for exp in expenses:
        lines.append(
            f"- {exp['amount_travel']:.2f} {trip['travel_currency']} = "
            f"{exp['amount_home']:.2f} {trip['home_currency']} ({exp['created_at']})"
        )
    send_menu_message(chat_id, "\n".join(lines))


@bot.message_handler(commands=["setrate"])
def command_setrate(message: types.Message):
    request_setrate(chat_id=message.chat.id, user_id=message.from_user.id)


def request_setrate(chat_id: int, user_id: int):
    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(chat_id, "Нет активного путешествия.")
        return
    clear_previous_menu(chat_id)
    pending_rate.add(user_id)
    msg = bot.send_message(
        chat_id,
        f"Текущий курс: 1 {trip['travel_currency']} = {trip['rate']:.4f} {trip['home_currency']}\n"
        "Введите новый курс (домашней валюты за 1 единицу валюты поездки):",
    )
    bot.register_next_step_handler(msg, update_rate_handler, user_id, trip["id"])


def request_expense(chat_id: int, user_id: int):
    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(chat_id, "Нет активного путешествия.")
        return
    clear_previous_menu(chat_id)
    msg = bot.send_message(chat_id, f"Введите сумму расхода в {trip['travel_currency']}:")
    bot.register_next_step_handler(msg, process_expense_input, user_id)


def update_rate_handler(message: types.Message, user_id: int, trip_id: int):
    text = (message.text or "").replace(",", ".")
    try:
        rate = float(text)
        if rate <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "Нужно положительное число. Введите снова:")
        bot.register_next_step_handler(msg, update_rate_handler, user_id, trip_id)
        return
    db.update_trip_rate(trip_id, rate)
    pending_rate.discard(user_id)
    send_menu_message(message.chat.id, "Курс обновлен.")


def show_expense_confirmation(chat_id: int, user_id: int, amount_travel: float):
    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(chat_id, "Нет активного путешествия.")
        return

    amount_home = amount_travel * trip["rate"]
    pending_expense[user_id] = amount_travel

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Учесть как расход? ✅ Да", callback_data="expense:yes"),
        types.InlineKeyboardButton("❌ Нет", callback_data="expense:no"),
    )
    bot.send_message(
        chat_id,
        f"{amount_travel:.2f} {trip['travel_currency']} = {amount_home:.2f} {trip['home_currency']}",
        reply_markup=kb,
    )


def process_expense_input(message: types.Message, user_id: int):
    text = (message.text or "").strip().replace(",", ".")
    try:
        amount_travel = float(text)
        if amount_travel <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "Сумма должна быть положительным числом. Введите снова:")
        bot.register_next_step_handler(msg, process_expense_input, user_id)
        return
    show_expense_confirmation(message.chat.id, user_id, amount_travel)


@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data

    if data == "newtrip":
        start_trip_flow(call.message.chat.id, user_id)
    elif data == "mytrips":
        trips = db.get_trips(user_id)
        if not trips:
            send_menu_message(call.message.chat.id, "Путешествий пока нет.")
        else:
            kb = types.InlineKeyboardMarkup()
            for trip in trips:
                marker = " (активно)" if trip["is_active"] else ""
                kb.add(types.InlineKeyboardButton(trip["name"] + marker, callback_data=f"switch:{trip['id']}"))
            bot.send_message(call.message.chat.id, "Ваши путешествия:", reply_markup=kb)
    elif data == "balance":
        send_balance(call.message.chat.id, user_id)
    elif data == "history":
        send_history(call.message.chat.id, user_id)
    elif data == "setrate":
        request_setrate(call.message.chat.id, user_id)
    elif data == "addexpense":
        request_expense(call.message.chat.id, user_id)
    elif data.startswith("switch:"):
        trip_id = int(data.split(":", 1)[1])
        db.set_active_trip(user_id, trip_id)
        trip = get_active_trip_dict(user_id)
        send_menu_message(call.message.chat.id, f"Активировано: {trip['name']}\n{format_balance(trip)}")
    elif data == "expense:yes":
        trip = get_active_trip_dict(user_id)
        amount_travel = pending_expense.get(user_id)
        if trip and amount_travel is not None:
            amount_home = amount_travel * trip["rate"]
            db.add_expense(trip["id"], amount_travel, amount_home)
            updated = get_active_trip_dict(user_id)
            send_menu_message(call.message.chat.id, f"Расход учтен.\n{format_balance(updated)}")
        pending_expense.pop(user_id, None)
    elif data == "expense:no":
        pending_expense.pop(user_id, None)
        send_menu_message(call.message.chat.id, "Ок, не учитываю.")

    bot.answer_callback_query(call.id)


@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message):
    user_id = message.from_user.id
    if user_id in pending_rate:
        return

    trip = get_active_trip_dict(user_id)
    if not trip:
        send_menu_message(message.chat.id, "Сначала создайте путешествие через меню.")
        return

    text = (message.text or "").strip().replace(",", ".")
    try:
        amount_travel = float(text)
        if amount_travel <= 0:
            raise ValueError
    except ValueError:
        send_menu_message(
            message.chat.id,
            "Отправьте число (например: 125.5) - это будет расход в валюте страны назначения.",
        )
        return

    show_expense_confirmation(message.chat.id, user_id, amount_travel)


def discard_pending_updates(
    bot_instance: telebot.TeleBot,
    attempts: int = 8,
    base_delay_sec: float = 2.0,
) -> None:
    """
    Сбрасывает очередь необработанных update до текущего момента (аналог skip_pending).

    В pyTelegramBotAPI вызов skip_pending внутри infinity_polling выполняется вне цикла
    с повторными попытками: при таймауте к api.telegram.org процесс сразу завершается.
    """
    log = logging.getLogger(__name__)
    last_exc: BaseException | None = None
    for n in range(1, attempts + 1):
        try:
            bot_instance.get_updates(offset=-1, limit=1, timeout=60, long_polling_timeout=1)
            return
        except (ReadTimeout, ConnectTimeout, RequestsConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            delay = min(30.0, base_delay_sec * (2 ** (n - 1)))
            log.warning("Telegram getUpdates (сброс очереди): попытка %s/%s — %s; пауза %.1f с", n, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError(
        "Не удалось установить соединение с api.telegram.org (таймаут или обрыв сети). "
        "Проверьте интернет, VPN, файрвол и переменные HTTP_PROXY/HTTPS_PROXY; при проблемном корпоративном прокси "
        "добавьте api.telegram.org в NO_PROXY или отключите прокси для этого запуска."
    ) from last_exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Бот запущен")
    if os.getenv("TELEGRAM_CLEAR_PENDING", "1").strip().lower() not in ("0", "false", "no"):
        discard_pending_updates(bot)
    else:
        logging.getLogger(__name__).info("Пропуск сброса очереди (TELEGRAM_CLEAR_PENDING=0)")
    bot.infinity_polling(
        skip_pending=False,
        timeout=60,
        long_polling_timeout=55,
        logger_level=logging.INFO,
    )
