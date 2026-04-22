import os
from typing import Iterable

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("CURRENCY_API_KEY")
BASE_URL = "http://api.exchangerate.host"


class CurrencyApiError(Exception):
    pass


def _ensure_api_key() -> str:
    if not API_KEY:
        raise CurrencyApiError("CURRENCY_API_KEY is not set in .env")
    return API_KEY


def get_current_rate(default: str = "USD", currencies: Iterable[str] = ("EUR", "GBP", "JPY")) -> dict:
    api_key = _ensure_api_key()
    url = f"{BASE_URL}/live"
    params = {
        "access_key": api_key,
        "source": default.upper(),
        "currencies": ",".join(c.upper() for c in currencies),
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise CurrencyApiError(str(data.get("error", "Unknown API error")))
    return data


def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    api_key = _ensure_api_key()
    url = f"{BASE_URL}/convert"
    params = {
        "access_key": api_key,
        "from": from_currency.upper(),
        "to": to_currency.upper(),
        "amount": amount,
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise CurrencyApiError(str(data.get("error", "Unknown API error")))
    return data


if __name__ == "__main__":
    rates = get_current_rate(default="RUB", currencies=("USD", "EUR", "GBP", "JPY", "CNY"))
    print(rates.get("quotes", {}))
    converted = convert_currency(100, "USD", "EUR")
    print(converted)
