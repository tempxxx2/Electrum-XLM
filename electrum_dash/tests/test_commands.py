import unittest
from unittest import mock
from decimal import Decimal

from electrum_dash.util import create_and_start_event_loop
from electrum_dash.commands import Commands, eval_bool
from electrum_dash import storage, wallet
from electrum_dash.wallet import restore_wallet_from_text
from electrum_dash.address_synchronizer import TX_HEIGHT_UNCONFIRMED
from electrum_dash.simple_config import SimpleConfig
from electrum_dash.transaction import Transaction, TxOutput, tx_from_any

from . import TestCaseForTestnet, ElectrumTestCase
from .test_wallet_vertical import WalletIntegrityHelper


class TestCommands(ElectrumTestCase):

    def setUp(self):
        super().setUp()
        self.asyncio_loop, self._stop_loop, self._loop_thread = create_and_start_event_loop()
        self.config = SimpleConfig({'electrum_path': self.electrum_path})

    def tearDown(self):
        super().tearDown()
        self.asyncio_loop.call_soon_threadsafe(self._stop_loop.set_result, 1)
        self._loop_thread.join(timeout=1)

    def test_setconfig_non_auth_number(self):
        self.assertEqual(7777, Commands._setconfig_normalize_value('rpcport', "7777"))
        self.assertEqual(7777, Commands._setconfig_normalize_value('rpcport', '7777'))
        self.assertAlmostEqual(Decimal(2.3), Commands._setconfig_normalize_value('somekey', '2.3'))

    def test_setconfig_non_auth_number_as_string(self):
        self.assertEqual("7777", Commands._setconfig_normalize_value('somekey', "'7777'"))

    def test_setconfig_non_auth_boolean(self):
        self.assertEqual(True, Commands._setconfig_normalize_value('show_console_tab', "true"))
        self.assertEqual(True, Commands._setconfig_normalize_value('show_console_tab', "True"))

    def test_setconfig_non_auth_list(self):
        self.assertEqual(['file:///var/www/', 'https://electrum.org'],
            Commands._setconfig_normalize_value('url_rewrite', "['file:///var/www/','https://electrum.org']"))
        self.assertEqual(['file:///var/www/', 'https://electrum.org'],
            Commands._setconfig_normalize_value('url_rewrite', '["file:///var/www/","https://electrum.org"]'))

    def test_setconfig_auth(self):
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcuser', "7777"))
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcuser', '7777'))
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcpassword', '7777'))
        self.assertEqual("2asd", Commands._setconfig_normalize_value('rpcpassword', '2asd'))
        self.assertEqual("['file:///var/www/','https://electrum.org']",
            Commands._setconfig_normalize_value('rpcpassword', "['file:///var/www/','https://electrum.org']"))

    def test_eval_bool(self):
        self.assertFalse(eval_bool("False"))
        self.assertFalse(eval_bool("false"))
        self.assertFalse(eval_bool("0"))
        self.assertTrue(eval_bool("True"))
        self.assertTrue(eval_bool("true"))
        self.assertTrue(eval_bool("1"))

    def test_convert_xkey(self):
        cmds = Commands(config=self.config)
        xpubs = {
            ("xpub6CCWFbvCbqF92kGwm9nV7t7RvVoQUKaq5USMdyVP6jvv1NgN52KAX6NNYCeE8Ca7JQC4K5tZcnQrubQcjJ6iixfPs4pwAQJAQgTt6hBjg11", "standard"),
        }
        for xkey1, xtype1 in xpubs:
            for xkey2, xtype2 in xpubs:
                self.assertEqual(xkey2, cmds._run('convert_xkey', (xkey1, xtype2)))

        xprvs = {
            ("xprv9yD9r6PJmTgqpGCUf8FUkkAhNTxv4rryiFWkqb5mYQPw8aMDXUzuyJ3tgv5vUqYkdK1E6Q5jKxPss4HkMBYV4q8AfG8t7rxgyS4xQX4ndAm", "standard"),
        }
        for xkey1, xtype1 in xprvs:
            for xkey2, xtype2 in xprvs:
                self.assertEqual(xkey2, cmds._run('convert_xkey', (xkey1, xtype2)))

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_encrypt_decrypt(self, mock_save_db):
        wallet = restore_wallet_from_text('p2pkh:XJvTzLoBy3jPMZFSTzK6KqTiNR3n5xbreSScEy7u9C8fEf1GZG3X',
                                          path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        cmds = Commands(config=self.config)
        cleartext = "asdasd this is the message"
        pubkey = "021f110909ded653828a254515b58498a6bafc96799fb0851554463ed44ca7d9da"
        ciphertext = cmds._run('encrypt', (pubkey, cleartext))
        self.assertEqual(cleartext, cmds._run('decrypt', (pubkey, ciphertext), wallet=wallet))

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_export_private_key_imported(self, mock_save_db):
        wallet = restore_wallet_from_text('p2pkh:XGx8LpkmLRv9RiMvpYx965BCaQKQbeMVVqgAh7B5SQVdosQiKJ4i p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd',
                                          path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        cmds = Commands(config=self.config)
        # single address tests
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', ("asdasd",), wallet=wallet)  # invalid addr, though might raise "not in wallet"
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', ("XdDHzW6aTeuQsraNXeEsPy5gAv1nUz7Y7Q",), wallet=wallet)  # not in wallet
        self.assertEqual("p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd",
                         cmds._run('getprivatekeys', ("Xci5KnMVkHrqBQk9cU4jwmzJfgaTPopHbz",), wallet=wallet))
        # list of addresses tests
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', (['XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', 'asd'],), wallet=wallet)
        self.assertEqual(['p2pkh:XGx8LpkmLRv9RiMvpYx965BCaQKQbeMVVqgAh7B5SQVdosQiKJ4i', 'p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd'],
                         cmds._run('getprivatekeys', (['XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', 'Xci5KnMVkHrqBQk9cU4jwmzJfgaTPopHbz'],), wallet=wallet))

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_export_private_key_deterministic(self, mock_save_db):
        wallet = restore_wallet_from_text('hint shock chair puzzle shock traffic drastic note dinosaur mention suggest sweet',
                                          gap_limit=2,
                                          path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        cmds = Commands(config=self.config)
        # single address tests
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', ("asdasd",), wallet=wallet)  # invalid addr, though might raise "not in wallet"
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', ("XdDHzW6aTeuQsraNXeEsPy5gAv1nUz7Y7Q",), wallet=wallet)  # not in wallet
        self.assertEqual("p2pkh:XE5VEmWKQRK5N7kQMfw6KqoRp3ExKWgaeCKsxsmDFBxJJBgdQdTH",
                         cmds._run('getprivatekeys', ("XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W",), wallet=wallet))
        # list of addresses tests
        with self.assertRaises(Exception):
            cmds._run('getprivatekeys', (['XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W', 'asd'],), wallet=wallet)
        self.assertEqual(['p2pkh:XE5VEmWKQRK5N7kQMfw6KqoRp3ExKWgaeCKsxsmDFBxJJBgdQdTH', 'p2pkh:XGtpLmVGmaRnfvRvd4qxSeE7PqJoi9FUfkgPKD24PeoJsZCh1EXg'],
                         cmds._run('getprivatekeys', (['XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W', 'XoEUKPPiPETff1S4oQmo4HGR1rYrRAX6uT'],), wallet=wallet))


class TestCommandsTestnet(TestCaseForTestnet):

    def setUp(self):
        super().setUp()
        self.asyncio_loop, self._stop_loop, self._loop_thread = create_and_start_event_loop()
        self.config = SimpleConfig({'electrum_path': self.electrum_path})

    def tearDown(self):
        super().tearDown()
        self.asyncio_loop.call_soon_threadsafe(self._stop_loop.set_result, 1)
        self._loop_thread.join(timeout=1)

    def test_convert_xkey(self):
        cmds = Commands(config=self.config)
        xpubs = {
            ("tpubD8p5qNfjczgTGbh9qgNxsbFgyhv8GgfVkmp3L88qtRm5ibUYiDVCrn6WYfnGey5XVVw6Bc5QNQUZW5B4jFQsHjmaenvkFUgWtKtgj5AdPm9", "standard"),
        }
        for xkey1, xtype1 in xpubs:
            for xkey2, xtype2 in xpubs:
                self.assertEqual(xkey2, cmds._run('convert_xkey', (xkey1, xtype2)))

        xprvs = {
            ("tprv8c83gxdVUcznP8fMx2iNUBbaQgQC7MUbBUDG3c6YU9xgt7Dn5pfcgHUeNZTAvuYmNgVHjyTzYzGWwJr7GvKCm2FkPaaJipyipbfJeB3tdPW", "standard"),
        }
        for xkey1, xtype1 in xprvs:
            for xkey2, xtype2 in xprvs:
                self.assertEqual(xkey2, cmds._run('convert_xkey', (xkey1, xtype2)))

    def test_serialize(self):
        cmds = Commands(config=self.config)
        jsontx = {
            "inputs": [
                {
                    "prevout_hash": "9d221a69ca3997cbeaf5624d723e7dc5f829b1023078c177d37bdae95f37c539",
                    "prevout_n": 1,
                    "value_sats": 1000000,
                    "privkey": "p2pkh:cVDXzzQg6RoCTfiKpe8MBvmm5d5cJc6JLuFApsFDKwWa6F5TVHpD"
                }
            ],
            "outputs": [
                {
                    "address": "yVMyvBvALMa12EJaqhEwu4tJ5h4tWcn9Yz",
                    "value_sats": 990000
                }
            ]
        }
        self.assertEqual("020000000139c5375fe9da7bd377c1783002b129f8c57d3e724d62f5eacb9739ca691a229d010000006a4730440220724a67810148fdc9474a71fafd116065d918c494dbabd4ad979a597045e9291c0220728fc15a0422cdb2624d6642fdd2e3c817131c5563309c8a6ac7a02846a082000121021f110909ded653828a254515b58498a6bafc96799fb0851554463ed44ca7d9dafeffffff01301b0f00000000001976a9146333e61a83cf112553c2f93629dbc9bba70b594f88ac00000000",
                         cmds._run('serialize', (jsontx,)))

    def test_serialize_custom_nsequence(self):
        cmds = Commands(config=self.config)
        jsontx = {
            "inputs": [
                {
                    "prevout_hash": "9d221a69ca3997cbeaf5624d723e7dc5f829b1023078c177d37bdae95f37c539",
                    "prevout_n": 1,
                    "value_sats": 1000000,
                    "privkey": "p2pkh:cVDXzzQg6RoCTfiKpe8MBvmm5d5cJc6JLuFApsFDKwWa6F5TVHpD",
                    "nsequence": 0xfffffffd
                }
            ],
            "outputs": [
                {
                    "address": "yVMyvBvALMa12EJaqhEwu4tJ5h4tWcn9Yz",
                    "value_sats": 990000
                }
            ]
        }
        print(cmds._run('serialize', (jsontx,)))
        self.assertEqual("020000000139c5375fe9da7bd377c1783002b129f8c57d3e724d62f5eacb9739ca691a229d010000006a4730440220100ca9083e11fb3adfc201591c8de7d6c8f6da70cddf090416ed4e7d54a1277702200c86304c89a187075d4992eb4741794f28aef08c1d025a009fb52d9ada8039860121021f110909ded653828a254515b58498a6bafc96799fb0851554463ed44ca7d9dafdffffff01301b0f00000000001976a9146333e61a83cf112553c2f93629dbc9bba70b594f88ac00000000",
                         cmds._run('serialize', (jsontx,)))

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_getprivatekeyforpath(self, mock_save_db):
        wallet = restore_wallet_from_text('hint shock chair puzzle shock traffic drastic note dinosaur mention suggest sweet',
                                          gap_limit=2,
                                          path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        cmds = Commands(config=self.config)
        self.assertEqual("p2pkh:cRVRdGfHrP9zb3cNTT1HGoG9JPcZfvjBMqUa2vTDMGDnKG1dNu24",
                         cmds._run('getprivatekeyforpath', ([0, 10000],), wallet=wallet))
        self.assertEqual("p2pkh:cRVRdGfHrP9zb3cNTT1HGoG9JPcZfvjBMqUa2vTDMGDnKG1dNu24",
                         cmds._run('getprivatekeyforpath', ("m/0/10000",), wallet=wallet))
        self.assertEqual("p2pkh:cS2exaULytoQ9CR89QHJDMg82NWKZ6f8rFboU7LGbHhdUMXxpPcd",
                         cmds._run('getprivatekeyforpath', ("m/5h/100000/88h/7",), wallet=wallet))

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_payto(self, mock_save_db):
        wallet = restore_wallet_from_text('ignore hospital shallow unit river glue battle chat pet option position icon',
                                          gap_limit=2,
                                          path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        # bootstrap wallet
        funding_tx = Transaction('0200000001688b4db66baaae1dde4a59aaccc1282757db5b192033a8d41718cd9e3949f7d2050000006a47304402203f8fca0aa5ef38d20e275d3c7cf191ce56e5f12e351ffa12f0a667440374ef7a0220206ef46cf234a35b7f4e3f062c99a118966403f9e788ec870d93114dd20081fa01210211db4efc20880c5b57cfa947550a7f337c99adf5c11999de380e2e4e196eebcefeffffff02ac150700000000001976a914eb9fad52a0664d10798fdcc0c4776ef07d910d7788acbc2b0800000000001976a914a9262375de5f7a2c81e0d28f3c6ab42e594627e888acea0e0800')
        funding_txid = funding_tx.txid()
        self.assertEqual('2304741a3b690d5c52bac443792e9ec6af535b527c562899b36e72e8c4e3bf4f', funding_txid)
        wallet.receive_tx_callback(funding_txid, funding_tx, TX_HEIGHT_UNCONFIRMED)

        cmds = Commands(config=self.config)
        tx_str = cmds._run(
            'payto', (),
            destination="yR41Y7aXsoYCSugFhXJ2DU5asRtW1rpzV3",
            amount="0.00123456",
            feerate=1000,
            locktime=1972344,
            wallet=wallet)

        tx = tx_from_any(tx_str)
        self.assertEqual(2, len(tx.outputs()))
        txout = TxOutput.from_address_and_value("yR41Y7aXsoYCSugFhXJ2DU5asRtW1rpzV3", 123456)
        self.assertTrue(txout in tx.outputs())
        self.assertEqual("02000000014fbfe3c4e8726eb39928567c525b53afc69e2e7943c4ba525c0d693b1a740423000000006a4730440220234aa5fe38e0d99013fb1427a0998560f513b26c37a1e53540da5b9b7859c0540220316735a04e115941b3f62e54f7f30473bf44c483eef335e702d542b8d6e81f3e0121025837acda77e6b2295e03c50c356057e3f2ec8d6c498ba9351618ee913510daa3feffffff0240e20100000000001976a91433ed429c95e392a27f1194b400b07cff5167284588ac8a320500000000001976a9144ee5b85791f50a4270ad7277ff307fd66f7254e188ac78181e00",
                         tx_str)
        assert  tx_from_any(tx_str).is_complete()


    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_signtransaction_without_wallet(self, mock_save_db):
        cmds = Commands(config=self.config)
        unsigned_tx = "cHNidP8BAFUCAAAAAfYPG8xEZIPSCFUQvT9hKSebChcHfRf44VBKdvCv+BvUAQAAAAD+////AVtGSgAAAAAAGXapFHbdRv3NIGILeiru0ElFh/yu1oXmiKxePwcAAAEA/SUBAgAAAAF895Ja488aAx4I7yq55Jxlr50rK3fkjjIx3Uxsgh7Z8wAAAABqRzBEAiBAE2MpeZYzp5QC2J7V9/KfvF7uQk/XcUs8YI9K+12zBAIgex7/mvNPvdj91u7WFnCMSJZAHxMW1XGvPD815CbeJ3wBIQK8Z9v+zCc0HugaBAKfsufI4SgHicvnhb2rbgZz8ceFuf7///8EQJwAAAAAAAAZdqkU+InI3CUUVo7OLrKa7Q7hZ6qArceIrHtHSgAAAAAAGXapFMXi0i9hMWlau5GQFeiPwlJxs4dQiKzklpgAAAAAABl2qRQjqj1H4J1g4HSr2IvCdVOedvNkQIis5JaYAAAAAAAZdqkU+gvqRTG5zwueDUbg7AZ1AQmAF9aIrJ01BwAiBgK8Z9v+zCc0HugaBAKfsufI4SgHicvnhb2rbgZz8ceFuQzZ3FryAAAAAAAAAAAAIgIDJgOS9iOn/pO/96NpC3pK5xamEiGEQs3wIF/8r9G1G+MM2dxa8gAAAAAIAAAAAA=="
        assert not tx_from_any(unsigned_tx).is_complete()
        privkey = "cVigSP6aKjWJVX9Gp1fGMLJyCbuxYWaMBVQyjUt5mL17WhMH53e6"
        tx_str = cmds._run('signtransaction_with_privkey', (), tx=unsigned_tx, privkey=privkey)
        self.assertEqual(tx_str,
                         "0200000001f60f1bcc446483d2085510bd3f6129279b0a17077d17f8e1504a76f0aff81bd4010000006a473044022040ea78e690d2a323daadc19831b3d1294e2a28d96c48481677c9786d9a1be3ac0220672ce6745e5de73f61cefb0f2f3fac5ce5aa9b9728fd28efb55e0660f92cc3d9012102bc67dbfecc27341ee81a04029fb2e7c8e1280789cbe785bdab6e0673f1c785b9feffffff015b464a00000000001976a91476dd46fdcd20620b7a2aeed0494587fcaed685e688ac5e3f0700")
        assert  tx_from_any(tx_str).is_complete()

    @mock.patch.object(wallet.Abstract_Wallet, 'save_db')
    def test_signtransaction_with_wallet(self, mock_save_db):
        wallet = restore_wallet_from_text('nest trophy glide lemon humor rose faint able keep squirrel major inform',
                                          gap_limit=2, path='if_this_exists_mocking_failed_648151893',
                                          config=self.config)['wallet']
        # bootstrap wallet1
        funding_tx = Transaction('0200000003912b2b205a528c5e875f9a64c1105217061135a9539e64c1953e40f9a7f9ec80000000006a473044022077d3ad93dc64e86cb7a3d754fc4fa7c5ffd8d86419f64b223939619e63dc0e0702202b6023914d57ea73c03a49af2512db6c2bf6f5280551f73c8da033128012a30f012102bc67dbfecc27341ee81a04029fb2e7c8e1280789cbe785bdab6e0673f1c785b9feffffff0e6c1fb9ced6f9595a6598b730d07a322461ec8e8556629be050129d5f95768b000000006a47304402204390383ecc05cd44d40c416c8bd748c7605636138d79c511f63563a989845ecd0220634d55c886988220bd57fe524c4ec152db9ee8f545350be6c74402c8151fa733012102bc67dbfecc27341ee81a04029fb2e7c8e1280789cbe785bdab6e0673f1c785b9feffffff688b4db66baaae1dde4a59aaccc1282757db5b192033a8d41718cd9e3949f7d2000000006a47304402206d9f25099d8a74ada78ce88b80ae5b6bdbfd563805d2cf7509ebdabaed8414ba02200ddff1fff3cf3d23684554097a7c85683f7bf93113833c5c78f6538141b3f4360121028bc7e29feff7fd705f2eec7820702ed9d62ef4f8edd32ac2d4094eee099fbb42feffffff022bd00200000000001976a914a9262375de5f7a2c81e0d28f3c6ab42e594627e888ac40420f00000000001976a914ebba561d2da1ed2a43b2cbbd45607a9d9c8e47de88ace80e0800')
        funding_txid = funding_tx.txid()
        funding_output_value = 1000000
        self.assertEqual('ebf85f1ef2573600e0038cfbb57b17b80965bccc95ece63e5cad81352493fd2f', funding_txid)
        wallet.receive_tx_callback(funding_txid, funding_tx, TX_HEIGHT_UNCONFIRMED)

        cmds = Commands(config=self.config)

        unsigned_tx = "cHNidP8BAFUCAAAAAW257E6UZwYN8f8oSrXTHHQhrK00vYMrFSVIFaLNuBzvAQAAAAD+////AYBBDwAAAAAAGXapFIEDYGhr7BAsosv+fSX+OJesHBTliKzoDggAAAEA/QcCAgAAAAORKysgWlKMXodfmmTBEFIXBhE1qVOeZMGVPkD5p/nsgAAAAABqRzBEAiA/YGaD9myM7jsgAKGPTkZkhAR83JfJ8AgnPhogmwI5AwIgZjnryK4gKljX0J6zkaQ1Isz1MA/GbPWbefza/pN3wFEBIQK8Z9v+zCc0HugaBAKfsufI4SgHicvnhb2rbgZz8ceFuf7///8ObB+5ztb5WVplmLcw0HoyJGHsjoVWYpvgUBKdX5V2iwAAAABqRzBEAiBbdcP7jckAeGXZXHp46ZBH4U1bDq8Zp+RQpVitjDaKBgIgRQ4D0fNZSOhB4BH2PN9WyXUKoFoCks5w5iIeBsxNXYUBIQK8Z9v+zCc0HugaBAKfsufI4SgHicvnhb2rbgZz8ceFuf7///9oi022a6quHd5KWarMwSgnV9tbGSAzqNQXGM2eOUn30gMAAABqRzBEAiAyuysUoJnTDUDGk4/fmHYx0aKYU2bNrSYwXzDwNAZPKgIgBnE5zJCd/euIA+veCqKLyS5GhMcsFurnvYM172pmWjsBIQI1+Z+RoF3aiVvdC4tijoYUrgvwX+/3/opJHVizOvOetv7///8CK9ACAAAAAAAZdqkUqSYjdd5feiyB4NKPPGq0LllGJ+iIrEBCDwAAAAAAGXapFOu6Vh0toe0qQ7LLvUVgep2cjkfeiKzoDggAIgYCtmt/3e46deXJByeDz2ClAftD16M6V+F+707JwW33iI8MUH3lsAAAAAAAAAAAACICAgtxqKxniaqjNXAhaSoUrgiutogL+DmPN+ilU/46YWm6DFB95bAAAAAAAQAAAAA="

        tx_str = cmds._run('signtransaction', (), tx=unsigned_tx, wallet=wallet)
        self.assertEqual("02000000016db9ec4e9467060df1ff284ab5d31c7421acad34bd832b15254815a2cdb81cef010000006a47304402204be923230cfe88dc76a140f68c99f1df62b3567619279f5ba042123a0ce53a0b02204c5fe44e2395896b15db968c07937c7b7c1bfc7243540a4079a675eea0085579012102b66b7fddee3a75e5c9072783cf60a501fb43d7a33a57e17eef4ec9c16df7888ffeffffff0180410f00000000001976a914810360686bec102ca2cbfe7d25fe3897ac1c14e588ace80e0800",
                         tx_str)
        assert  tx_from_any(tx_str).is_complete()
