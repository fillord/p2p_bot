import os
import httpx
from decimal import Decimal

USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

async def generate_new_wallet():
    API_KEY = os.getenv("NOW_PAYMENTS_API_KEY") 
    API_URL = "https://api.nowpayments.io/v1/payment"

    if not API_KEY:
        print("Ошибка: API ключ для NowPayments не найден.")
        return None
    headers = {'x-api-key': API_KEY}
    payload = {
        "price_amount": 20,
        "price_currency": "usd",
        "pay_currency": "usdttrc20",
        "ipn_callback_url": "https://nowpayments.io"
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get('pay_address')
    except httpx.HTTPStatusError as e:
        print(f"Ошибка API при генерации кошелька: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        print(f"Произошла непредвиденная ошибка: {e}")
        return None

async def check_new_transactions(wallet_address: str):
    """Проверяет новые входящие транзакции USDT для указанного кошелька."""
    api_url = f"https://api.trongrid.io/v1/accounts/{wallet_address}/transactions/trc20"
    params = {
        "limit": 50,
        "only_to": "true",
        "contract_address": USDT_CONTRACT_ADDRESS,
    }
    new_transactions = []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, params=params)
            if response.status_code == 200:
                data = response.json()
                if data.get("success") and data.get("data"):
                    for tx in data["data"]:
                        amount = Decimal(tx.get("value", "0")) / Decimal("1000000")
                        tx_info = {
                            "txid": tx.get("transaction_id"),
                            "amount": amount,
                            "from": tx.get("from"),
                        }
                        new_transactions.append(tx_info)
    except Exception as e:
        print(f"Ошибка при проверке транзакций для {wallet_address}: {e}")
    
    return new_transactions


async def create_payout(address: str, amount: Decimal):
    """Создает выплату на указанный адрес через API NowPayments."""
    PAYOUT_API_URL = "https://api.nowpayments.io/v1/payout"
    API_KEY = os.getenv("NOW_PAYMENTS_API_KEY")
    if not API_KEY:
        print("Ошибка: API ключ для NowPayments не найден.")
        return False, "API ключ не настроен"

    headers = {
        'x-api-key': API_KEY
    }
    payload = {
        "payouts": [
            {
                "address": address,
                "currency": "USDTTRC20",
                "amount": str(amount)
            }
        ]
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(PAYOUT_API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("payouts") and data["payouts"][0].get("batch_id"):
                return True, data["payouts"][0]["batch_id"]
            else:
                return False, data.get("message", "Неизвестная ошибка API")
    except httpx.HTTPStatusError as e:
        error_message = e.response.json().get("message", f"HTTP {e.response.status_code}")
        print(f"Ошибка API при создании выплаты: {error_message}")
        return False, error_message
    except Exception as e:
        print(f"Произошла непредвиденная ошибка при выплате: {e}")
        return False, str(e)