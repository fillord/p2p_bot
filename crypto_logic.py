# import os
# import httpx
# from decimal import Decimal

# # Константа адреса контракта USDT в сети Tron
# USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# async def generate_new_wallet():
#     # ... (эта функция остается без изменений)
#     API_KEY = os.getenv("NOW_PAYMENTS_API_KEY") 
#     API_URL = "https://api.nowpayments.io/v1/payment"

#     if not API_KEY:
#         print("Ошибка: API ключ для NowPayments не найден.")
#         return None
#     headers = {'x-api-key': API_KEY}
#     payload = {
#         "price_amount": 20,
#         "price_currency": "usd",
#         "pay_currency": "usdttrc20",
#         "ipn_callback_url": "https://nowpayments.io"
#     }
#     try:
#         async with httpx.AsyncClient() as client:
#             response = await client.post(API_URL, headers=headers, json=payload)
#             response.raise_for_status()
#             data = response.json()
#             return data.get('pay_address')
#     except httpx.HTTPStatusError as e:
#         print(f"Ошибка API при генерации кошелька: {e.response.status_code} - {e.response.text}")
#         return None
#     except Exception as e:
#         print(f"Произошла непредвиденная ошибка: {e}")
#         return None

# async def check_new_transactions(wallet_address: str):
#     """Проверяет новые входящие транзакции USDT для указанного кошелька."""
#     api_url = f"https://api.trongrid.io/v1/accounts/{wallet_address}/transactions/trc20"
#     params = {
#         "limit": 50,  # Получаем последние 50 транзакций
#         "only_to": "true", # Только входящие
#         "contract_address": USDT_CONTRACT_ADDRESS,
#     }
#     new_transactions = []
#     try:
#         async with httpx.AsyncClient() as client:
#             response = await client.get(api_url, params=params)
#             if response.status_code == 200:
#                 data = response.json()
#                 if data.get("success") and data.get("data"):
#                     for tx in data["data"]:
#                         # Сумма в USDT приходит с 6 нулями, приводим к нормальному виду
#                         amount = Decimal(tx.get("value", "0")) / Decimal("1000000")
#                         tx_info = {
#                             "txid": tx.get("transaction_id"),
#                             "amount": amount,
#                             "from": tx.get("from"),
#                         }
#                         new_transactions.append(tx_info)
#     except Exception as e:
#         print(f"Ошибка при проверке транзакций для {wallet_address}: {e}")
    
#     return new_transactions

import os
import httpx
from decimal import Decimal

# Константа адреса контракта USDT в сети Tron
USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

async def generate_new_wallet():
    # ... (эта функция остается без изменений, ее код не важен для теста)
    API_KEY = os.getenv("NOW_PAYMENTS_API_KEY") 
    API_URL = "https://api.nowpayments.io/v1/payment"
    if not API_KEY: return None
    headers = {'x-api-key': API_KEY}
    payload = {"price_amount": 20, "price_currency": "usd", "pay_currency": "usdttrc20", "ipn_callback_url": "https://nowpayments.io"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get('pay_address')
    except Exception: return None


# --- ВРЕМЕННАЯ ВЕРСИЯ ФУНКЦИИ ДЛЯ ТЕСТА ---
async def check_new_transactions(wallet_address: str):
    """(ТЕСТОВАЯ) Всегда возвращает одну и ту же фейковую транзакцию."""
    print(f"--- РЕЖИМ ТЕСТИРОВАНИЯ: Симуляция проверки кошелька {wallet_address} ---")
    
    test_transaction = {
        "txid": "fake_txid_for_testing_12345", # Уникальный ID нашей тестовой транзакции
        "amount": Decimal("7.77"),
        "from": "test_sender_address",
    }
    
    return [test_transaction]