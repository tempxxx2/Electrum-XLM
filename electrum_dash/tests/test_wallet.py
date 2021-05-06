import shutil
import tempfile
import sys
import os
import json
from decimal import Decimal
import time
from io import StringIO
import asyncio

from electrum_dash.storage import WalletStorage
from electrum_dash.wallet_db import FINAL_SEED_VERSION
from electrum_dash.wallet import (Abstract_Wallet, Standard_Wallet, create_new_wallet,
                             restore_wallet_from_text, Imported_Wallet, Wallet)
from electrum_dash.exchange_rate import ExchangeBase, FxThread
from electrum_dash.util import TxMinedInfo, InvalidPassword
from electrum_dash.bitcoin import COIN
from electrum_dash.wallet_db import WalletDB
from electrum_dash.simple_config import SimpleConfig
from electrum_dash import util

from . import ElectrumTestCase


class FakeSynchronizer(object):

    def __init__(self):
        self.store = []

    def add(self, address):
        self.store.append(address)


class WalletTestCase(ElectrumTestCase):

    def setUp(self):
        super(WalletTestCase, self).setUp()
        self.user_dir = tempfile.mkdtemp()
        self.config = SimpleConfig({'electrum_path': self.user_dir})

        self.wallet_path = os.path.join(self.user_dir, "somewallet")

        self._saved_stdout = sys.stdout
        self._stdout_buffer = StringIO()
        sys.stdout = self._stdout_buffer

    def tearDown(self):
        super(WalletTestCase, self).tearDown()
        shutil.rmtree(self.user_dir)
        # Restore the "real" stdout
        sys.stdout = self._saved_stdout


class TestWalletStorage(WalletTestCase):

    def test_read_dictionary_from_file(self):

        some_dict = {"a":"b", "c":"d"}
        contents = json.dumps(some_dict)
        with open(self.wallet_path, "w") as f:
            contents = f.write(contents)

        storage = WalletStorage(self.wallet_path)
        db = WalletDB(storage.read(), manual_upgrades=True)
        self.assertEqual("b", db.get("a"))
        self.assertEqual("d", db.get("c"))

    def test_write_dictionary_to_file(self):

        storage = WalletStorage(self.wallet_path)
        db = WalletDB('', manual_upgrades=True)

        some_dict = {
            u"a": u"b",
            u"c": u"d",
            u"seed_version": FINAL_SEED_VERSION}

        for key, value in some_dict.items():
            db.put(key, value)
        db.write(storage)

        with open(self.wallet_path, "r") as f:
            contents = f.read()
        d = json.loads(contents)
        for key, value in some_dict.items():
            self.assertEqual(d[key], value)

class FakeExchange(ExchangeBase):
    def __init__(self, rate):
        super().__init__(lambda self: None, lambda self: None)
        self.quotes = {'TEST': rate}

class FakeFxThread:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ccy = 'TEST'

    remove_thousands_separator = staticmethod(FxThread.remove_thousands_separator)
    timestamp_rate = FxThread.timestamp_rate
    ccy_amount_str = FxThread.ccy_amount_str
    history_rate = FxThread.history_rate

class FakeWallet:
    def __init__(self, fiat_value):
        super().__init__()
        self.fiat_value = fiat_value
        self.db = WalletDB("{}", manual_upgrades=True)
        self.db.transactions = self.db.verified_tx = {'abc':'Tx'}

    def get_tx_height(self, txid):
        # because we use a current timestamp, and history is empty,
        # FxThread.history_rate will use spot prices
        return TxMinedInfo(height=10, conf=10, timestamp=int(time.time()), header_hash='def')

    default_fiat_value = Abstract_Wallet.default_fiat_value
    price_at_timestamp = Abstract_Wallet.price_at_timestamp
    class storage:
        put = lambda self, x: None

txid = 'abc'
ccy = 'TEST'

class TestFiat(ElectrumTestCase):
    def setUp(self):
        super().setUp()
        self.value_sat = COIN
        self.fiat_value = {}
        self.wallet = FakeWallet(fiat_value=self.fiat_value)
        self.fx = FakeFxThread(FakeExchange(Decimal('1000.001')))
        default_fiat = Abstract_Wallet.default_fiat_value(self.wallet, txid, self.fx, self.value_sat)
        self.assertEqual(Decimal('1000.001'), default_fiat)
        self.assertEqual('1,000.00', self.fx.ccy_amount_str(default_fiat, commas=True))

    def test_save_fiat_and_reset(self):
        self.assertEqual(False, Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, '1000.01', self.fx, self.value_sat))
        saved = self.fiat_value[ccy][txid]
        self.assertEqual('1,000.01', self.fx.ccy_amount_str(Decimal(saved), commas=True))
        self.assertEqual(True,       Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, '', self.fx, self.value_sat))
        self.assertNotIn(txid, self.fiat_value[ccy])
        # even though we are not setting it to the exact fiat value according to the exchange rate, precision is truncated away
        self.assertEqual(True, Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, '1,000.002', self.fx, self.value_sat))

    def test_too_high_precision_value_resets_with_no_saved_value(self):
        self.assertEqual(True, Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, '1,000.001', self.fx, self.value_sat))

    def test_empty_resets(self):
        self.assertEqual(True, Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, '', self.fx, self.value_sat))
        self.assertNotIn(ccy, self.fiat_value)

    def test_save_garbage(self):
        self.assertEqual(False, Abstract_Wallet.set_fiat_value(self.wallet, txid, ccy, 'garbage', self.fx, self.value_sat))
        self.assertNotIn(ccy, self.fiat_value)


class TestCreateRestoreWallet(WalletTestCase):

    def test_create_new_wallet(self):
        passphrase = 'mypassphrase'
        password = 'mypassword'
        encrypt_file = True
        d = create_new_wallet(path=self.wallet_path,
                              passphrase=passphrase,
                              password=password,
                              encrypt_file=encrypt_file,
                              gap_limit=1,
                              config=self.config)
        wallet = d['wallet']  # type: Standard_Wallet
        wallet.check_password(password)
        self.assertEqual(passphrase, wallet.keystore.get_passphrase(password))
        self.assertEqual(d['seed'], wallet.keystore.get_seed(password))
        self.assertEqual(encrypt_file, wallet.storage.is_encrypted())

    def test_restore_wallet_from_text_mnemonic(self):
        text = 'hint shock chair puzzle shock traffic drastic note dinosaur mention suggest sweet'
        passphrase = 'mypassphrase'
        password = 'mypassword'
        encrypt_file = True
        d = restore_wallet_from_text(text,
                                     path=self.wallet_path,
                                     passphrase=passphrase,
                                     password=password,
                                     encrypt_file=encrypt_file,
                                     gap_limit=1,
                                     config=self.config)
        wallet = d['wallet']  # type: Standard_Wallet
        self.assertEqual(passphrase, wallet.keystore.get_passphrase(password))
        self.assertEqual(text, wallet.keystore.get_seed(password))
        self.assertEqual(encrypt_file, wallet.storage.is_encrypted())
        self.assertEqual('XdDHzW6aTeuQsraNXeEsPy5gAv1nUz7Y7Q', wallet.get_receiving_addresses()[0])

    def test_restore_wallet_from_text_xpub(self):
        text = 'xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ'
        d = restore_wallet_from_text(text, path=self.wallet_path, gap_limit=1, config=self.config)
        wallet = d['wallet']  # type: Standard_Wallet
        self.assertEqual(text, wallet.keystore.get_master_public_key())
        self.assertEqual('XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', wallet.get_receiving_addresses()[0])

    def test_restore_wallet_from_text_xkey_that_is_also_a_valid_electrum_seed_by_chance(self):
        text = 'xprv9s21ZrQH143K39BUkM2iuppjFpkJ37KUqQAaDvvTzeCSDCtpqM1TsN47vmuUqyJqEVJUmFBDo55dsKwhzYD6sPTecP2JeNbYoia7hzf6Jzt'
        d = restore_wallet_from_text(text, path=self.wallet_path, gap_limit=1, config=self.config)
        wallet = d['wallet']  # type: Standard_Wallet
        self.assertEqual(text, wallet.keystore.get_master_private_key(password=None))
        self.assertEqual('Xj1vN7ntKrNp87tBjXCSGFXPtAVgJA525U', wallet.get_receiving_addresses()[0])

    def test_restore_wallet_from_text_xprv(self):
        text = 'xprv9wTYmMFdV23N2TdNG573QoEsfRrWKQgWeibmLntzniatZvR9BmLnvSxqu53Kw1UmYPxLgboyZQaXwTCg8MSY3H2EU4pWcQDnRnrVA1xe8fs'
        d = restore_wallet_from_text(text, path=self.wallet_path, gap_limit=1, config=self.config)
        wallet = d['wallet']  # type: Standard_Wallet
        self.assertEqual(text, wallet.keystore.get_master_private_key(password=None))
        self.assertEqual('XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', wallet.get_receiving_addresses()[0])

    def test_restore_wallet_from_text_addresses(self):
        text = 'XdjwdihsyoLpoCHFUpd8x3iH1rsMsS2q5P Xdt8NqE5wSX9ytfP958t4tKdXoZDo6Bm6T'
        d = restore_wallet_from_text(text, path=self.wallet_path, config=self.config)
        wallet = d['wallet']  # type: Imported_Wallet
        self.assertEqual('XdjwdihsyoLpoCHFUpd8x3iH1rsMsS2q5P', wallet.get_receiving_addresses()[0])
        self.assertEqual(2, len(wallet.get_receiving_addresses()))
        # also test addr deletion
        wallet.delete_address('Xdt8NqE5wSX9ytfP958t4tKdXoZDo6Bm6T')
        self.assertEqual(1, len(wallet.get_receiving_addresses()))

    def test_restore_wallet_from_text_privkeys(self):
        text = 'p2pkh:XGx8LpkmLRv9RiMvpYx965BCaQKQbeMVVqgAh7B5SQVdosQiKJ4i p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd'
        d = restore_wallet_from_text(text, path=self.wallet_path, config=self.config)
        wallet = d['wallet']  # type: Imported_Wallet
        addr0 = wallet.get_receiving_addresses()[0]
        self.assertEqual('Xci5KnMVkHrqBQk9cU4jwmzJfgaTPopHbz', addr0)
        self.assertEqual('p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd',
                         wallet.export_private_key(addr0, password=None))
        self.assertEqual(2, len(wallet.get_receiving_addresses()))
        # also test addr deletion
        wallet.delete_address('XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2')
        self.assertEqual(1, len(wallet.get_receiving_addresses()))


class TestWalletPassword(WalletTestCase):

    def setUp(self):
        super().setUp()
        self.asyncio_loop, self._stop_loop, self._loop_thread = util.create_and_start_event_loop()

    def tearDown(self):
        super().tearDown()
        self.asyncio_loop.call_soon_threadsafe(self._stop_loop.set_result, 1)
        self._loop_thread.join(timeout=1)

    def test_update_password_of_imported_wallet(self):
        wallet_str = '{"addr_history":{"Xcmu97gPDoJn6NELShGPRnaRDvbLWyN4ph":[],"Xetp3vzZd25tqMTHUs826okUcpnxAtG73J":[],"XpeViGqbFaUYUQYHeYx62tSBNfrctAu6er":[]},"addresses":{"change":[],"receiving":["Xcmu97gPDoJn6NELShGPRnaRDvbLWyN4ph","XpeViGqbFaUYUQYHeYx62tSBNfrctAu6er","Xetp3vzZd25tqMTHUs826okUcpnxAtG73J"]},"keystore":{"keypairs":{"0344b1588589958b0bcab03435061539e9bcf54677c104904044e4f8901f4ebdf5":"XGw9fNSxGB9e7c9G1wHwMdVsfgvLZGxrUVDEiuozYtEJbQQ8Djc9","0389508c13999d08ffae0f434a085f4185922d64765c0bff2f66e36ad7f745cc5f":"XHLdYVniEEZajZEreWmi6AwfqgzjJwKDAyZATAjVavZZ58gdjf38","04575f52b82f159fa649d2a4c353eb7435f30206f0a6cb9674fbd659f45082c37d559ffd19bea9c0d3b7dcc07a7b79f4cffb76026d5d4dff35341efe99056e22d2":"7ri5P9aS73pjQp62MU6MdhNz2cp6XamakdXtp91aRLEJUtZLd1M"},"type":"imported"},"pruned_txo":{},"seed_version":13,"stored_height":-1,"transactions":{},"tx_fees":{},"txi":{},"txo":{},"use_encryption":false,"verified_tx3":{},"wallet_type":"standard","winpos-qt":[100,100,840,405]}'
        db = WalletDB(wallet_str, manual_upgrades=False)
        storage = WalletStorage(self.wallet_path)
        wallet = Wallet(db, storage, config=self.config)

        wallet.check_password(None)

        wallet.update_password(None, "1234")

        with self.assertRaises(InvalidPassword):
            wallet.check_password(None)
        with self.assertRaises(InvalidPassword):
            wallet.check_password("wrong password")
        wallet.check_password("1234")

    def test_update_password_of_standard_wallet(self):
        wallet_str = '''{"addr_history":{"Xbv3X1eD4PWQ4Fm8yJ32f4shaLnNLKkXLc":[],"XcQFtHuHYnZi2sewUkAJpYU59zQNDAzRyP":[],"XcyHr5KQac62gRra6zR8QSBRFGBrQw3JPn":[],"XdS6ykw3GYehruoCMDL6isKAkQADt91sso":[],"Xe9Vt6NcF1k33P4eBf2sdhP5VdQoM82ra3":[],"XeNc2DCP6pzaht7DAKcGCz2PHd5Lj12iCK":[],"Xew7jYaKqb7c15Fno2T9i8WTSNvm3hRdL2":[],"XhV9qCJ3zPzgbALTBZUNmztsdx2hq5fL4D":[],"XiPDShPTx8q1Y2tcTYZepLqGW9wVCJV67t":[],"XiTzDWc5hUXnReDeF5XT6NEfYMnZmiEhD6":[],"XjFyxpK8chRxk9YJVP2uLc4x9HS72A6PDd":[],"XjftZGtEoJtcyMihTrWRjZrzoezouTEM9D":[],"XjjNH2aENdryvsMhqWD68Gs9MV91GxL38J":[],"XatcXWHv95EHJgJE7hcsvSiwMhyR74t4Mh":[],"XobfXwwNYgYakhEWbP3swAaXcMjtnV7KnJ":[],"XokpWh2CnDYd4L899597czi8gH99sMzMbQ":[],"Xr94GzQPZs9Aj7mm8Fp4Jh458XCVUPw8wx":[],"XrCgX57b8cfRCppFxnX2BweBMjxUXCGFj7":[],"XrPcfCVLCnqRXdES1gcaRUpRAHy7uaUUqj":[],"XsmJHj4DfesHP6oWxC47VXuorb5YdZQftK":[],"XtCpHFPtGR5pcR5ChNsWmT67zTQMHr4u81":[],"Xu68nscwNBVkfHsuLmcxgjqQBf7JWmGLej":[],"XuVoPvFvHauQ5fropq9oe7w1zSgc1FRCM3":[],"XuXL4eBXnJmWPYYTRVYYcNqqzNKZmrTTTV":[],"XwgwoU6SbpKCE6dnfP7j1re5UW4J93vYE9":[],"XySy8SMtS1SRoLzuif72fXwegQQPhDRtgp":[]},"addresses":{"change":["XrPcfCVLCnqRXdES1gcaRUpRAHy7uaUUqj","Xr94GzQPZs9Aj7mm8Fp4Jh458XCVUPw8wx","Xew7jYaKqb7c15Fno2T9i8WTSNvm3hRdL2","XjjNH2aENdryvsMhqWD68Gs9MV91GxL38J","XjftZGtEoJtcyMihTrWRjZrzoezouTEM9D","XtCpHFPtGR5pcR5ChNsWmT67zTQMHr4u81"],"receiving":["XeNc2DCP6pzaht7DAKcGCz2PHd5Lj12iCK","XcyHr5KQac62gRra6zR8QSBRFGBrQw3JPn","XjFyxpK8chRxk9YJVP2uLc4x9HS72A6PDd","XsmJHj4DfesHP6oWxC47VXuorb5YdZQftK","XySy8SMtS1SRoLzuif72fXwegQQPhDRtgp","XdS6ykw3GYehruoCMDL6isKAkQADt91sso","Xu68nscwNBVkfHsuLmcxgjqQBf7JWmGLej","Xbv3X1eD4PWQ4Fm8yJ32f4shaLnNLKkXLc","XcQFtHuHYnZi2sewUkAJpYU59zQNDAzRyP","Xe9Vt6NcF1k33P4eBf2sdhP5VdQoM82ra3","XuXL4eBXnJmWPYYTRVYYcNqqzNKZmrTTTV","XhV9qCJ3zPzgbALTBZUNmztsdx2hq5fL4D","XokpWh2CnDYd4L899597czi8gH99sMzMbQ","XiPDShPTx8q1Y2tcTYZepLqGW9wVCJV67t","XuVoPvFvHauQ5fropq9oe7w1zSgc1FRCM3","XiTzDWc5hUXnReDeF5XT6NEfYMnZmiEhD6","XatcXWHv95EHJgJE7hcsvSiwMhyR74t4Mh","XobfXwwNYgYakhEWbP3swAaXcMjtnV7KnJ","XrCgX57b8cfRCppFxnX2BweBMjxUXCGFj7","XwgwoU6SbpKCE6dnfP7j1re5UW4J93vYE9"]},"keystore":{"seed":"cereal wise two govern top pet frog nut rule sketch bundle logic","type":"bip32","xprv":"xprv9s21ZrQH143K29XjRjUs6MnDB9wXjXbJP2kG1fnRk8zjdDYWqVkQYUqaDtgZp5zPSrH5PZQJs8sU25HrUgT1WdgsPU8GbifKurtMYg37d4v","xpub":"xpub661MyMwAqRbcEdcCXm1sTViwjBn28zK9kFfrp4C3JUXiW1sfP34f6HA45B9yr7EH5XGzWuTfMTdqpt9XPrVQVUdgiYb5NW9m8ij1FSZgGBF"},"pruned_txo":{},"seed_type":"standard","seed_version":13,"stored_height":-1,"transactions":{},"tx_fees":{},"txi":{},"txo":{},"use_encryption":false,"verified_tx3":{},"wallet_type":"standard","winpos-qt":[619,310,840,405]}'''
        db = WalletDB(wallet_str, manual_upgrades=False)
        storage = WalletStorage(self.wallet_path)
        wallet = Wallet(db, storage, config=self.config)

        wallet.check_password(None)

        wallet.update_password(None, "1234")
        with self.assertRaises(InvalidPassword):
            wallet.check_password(None)
        with self.assertRaises(InvalidPassword):
            wallet.check_password("wrong password")
        wallet.check_password("1234")

    def test_update_password_with_app_restarts(self):
        wallet_str = '{"addr_history":{"Xcmu97gPDoJn6NELShGPRnaRDvbLWyN4ph":[],"Xetp3vzZd25tqMTHUs826okUcpnxAtG73J":[],"XpeViGqbFaUYUQYHeYx62tSBNfrctAu6er":[]},"addresses":{"change":[],"receiving":["Xcmu97gPDoJn6NELShGPRnaRDvbLWyN4ph","XpeViGqbFaUYUQYHeYx62tSBNfrctAu6er","Xetp3vzZd25tqMTHUs826okUcpnxAtG73J"]},"keystore":{"keypairs":{"0344b1588589958b0bcab03435061539e9bcf54677c104904044e4f8901f4ebdf5":"XGw9fNSxGB9e7c9G1wHwMdVsfgvLZGxrUVDEiuozYtEJbQQ8Djc9","0389508c13999d08ffae0f434a085f4185922d64765c0bff2f66e36ad7f745cc5f":"XHLdYVniEEZajZEreWmi6AwfqgzjJwKDAyZATAjVavZZ58gdjf38","04575f52b82f159fa649d2a4c353eb7435f30206f0a6cb9674fbd659f45082c37d559ffd19bea9c0d3b7dcc07a7b79f4cffb76026d5d4dff35341efe99056e22d2":"7ri5P9aS73pjQp62MU6MdhNz2cp6XamakdXtp91aRLEJUtZLd1M"},"type":"imported"},"pruned_txo":{},"seed_version":13,"stored_height":-1,"transactions":{},"tx_fees":{},"txi":{},"txo":{},"use_encryption":false,"verified_tx3":{},"wallet_type":"standard","winpos-qt":[100,100,840,405]}'
        db = WalletDB(wallet_str, manual_upgrades=False)
        storage = WalletStorage(self.wallet_path)
        wallet = Wallet(db, storage, config=self.config)
        asyncio.run_coroutine_threadsafe(wallet.stop(), self.asyncio_loop).result()

        storage = WalletStorage(self.wallet_path)
        # if storage.is_encrypted():
        #     storage.decrypt(password)
        db = WalletDB(storage.read(), manual_upgrades=False)
        wallet = Wallet(db, storage, config=self.config)

        wallet.check_password(None)

        wallet.update_password(None, "1234")
        with self.assertRaises(InvalidPassword):
            wallet.check_password(None)
        with self.assertRaises(InvalidPassword):
            wallet.check_password("wrong password")
        wallet.check_password("1234")
