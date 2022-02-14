import asyncio
import copy
import os
import gzip
import random
import shutil
import tempfile
import time
from collections import defaultdict, Counter
from pprint import pprint

from electrum_dash import dash_ps, ecc
from electrum_dash.address_synchronizer import (TX_HEIGHT_LOCAL,
                                                TX_HEIGHT_UNCONF_PARENT,
                                                TX_HEIGHT_UNCONFIRMED)
from electrum_dash.bitcoin import COIN
from electrum_dash.dash_ps_util import (COLLATERAL_VAL, CREATE_COLLATERAL_VAL,
                                        CREATE_COLLATERAL_VALS, PS_DENOMS_VALS,
                                        MIN_DENOM_VAL, PSMinRoundsCheckFailed,
                                        PSPossibleDoubleSpendError,
                                        PSStates, PSTxWorkflow, PSTxData,
                                        PSDenominateWorkflow, filter_log_line,
                                        FILTERED_TXID, FILTERED_ADDR,
                                        PSCoinRounds, ps_coin_rounds_str,
                                        calc_tx_size, calc_tx_fee, to_duffs,
                                        MixingStats, PSFeeTooHigh)
from electrum_dash.dash_ps_wallet import (KPStates, KP_ALL_TYPES, KP_SPENDABLE,
                                          KP_PS_COINS, KP_PS_CHANGE,
                                          PSKsInternalAddressCorruption)
from electrum_dash.dash_tx import PSTxTypes, SPEC_TX_NAMES
from electrum_dash import keystore
from electrum_dash.simple_config import SimpleConfig
from electrum_dash.storage import WalletStorage
from electrum_dash.transaction import (Transaction, PartialTxOutput,
                                       PartialTransaction)
from electrum_dash.util import Satoshis, NotEnoughFunds, TxMinedInfo, bh2u
from electrum_dash.wallet import Wallet
from electrum_dash.wallet_db import WalletDB

from . import TestCaseForTestnet


TEST_MNEMONIC = ('total small during tattoo congress faith'
                 ' acoustic fashion zone fringe fit crisp')


class NetworkBroadcastMock:

    def __init__(self, pass_cnt=None):
        self.pass_cnt = pass_cnt
        self.passed_cnt = 0

    async def broadcast_transaction(self, tx, *, timeout=None) -> None:
        if self.pass_cnt is not None and self.passed_cnt >= self.pass_cnt:
            raise Exception('Broadcast Failed')
        self.passed_cnt += 1


class WalletGetTxHeigthMock:

    def __init__(self, nonlocal_txids):
        self.nonlocal_txids = nonlocal_txids

    def is_local_tx(self, txid):
        tx_mined_info = self.get_tx_height(txid)
        if tx_mined_info.height == TX_HEIGHT_LOCAL:
            return True
        else:
            return False

    def get_tx_height(self, txid):
        if txid not in self.nonlocal_txids:
            return TxMinedInfo(height=TX_HEIGHT_LOCAL, conf=0)
        else:
            height = random.choice([TX_HEIGHT_UNCONF_PARENT,
                                    TX_HEIGHT_UNCONFIRMED])
        return TxMinedInfo(height=height, conf=0)


class PSWalletTestCase(TestCaseForTestnet):

    def setUp(self):
        super(PSWalletTestCase, self).setUp()
        self.user_dir = tempfile.mkdtemp()
        self.wallet_path = os.path.join(self.user_dir, 'wallet_ps1')
        tests_path = os.path.dirname(os.path.abspath(__file__))
        test_data_file = os.path.join(tests_path, 'data', 'wallet_ps1.gz')
        shutil.copyfile(test_data_file, '%s.gz' % self.wallet_path)
        with gzip.open('%s.gz' % self.wallet_path, 'rb') as rfh:
            wallet_data = rfh.read()
            wallet_data = wallet_data.decode('utf-8')
        with open(self.wallet_path, 'w') as wfh:
            wfh.write(wallet_data)
        self.config = SimpleConfig({'electrum_path': self.user_dir})
        self.config.set_key('dynamic_fees', False, True)
        self.storage = WalletStorage(self.wallet_path)
        self.w_db = WalletDB(self.storage.read(), manual_upgrades=True)
        self.w_db.upgrade()  # wallet_ps1 have version 18
        self.wallet = Wallet(self.w_db, self.storage, config=self.config)
        psman = self.wallet.psman
        psman.MIN_NEW_DENOMS_DELAY = 0
        psman.MAX_NEW_DENOMS_DELAY = 0
        psman.state = PSStates.Ready
        psman.loop = asyncio.get_event_loop()
        psman.can_find_untracked = lambda: True
        psman.is_unittest_run = True

    def tearDown(self):
        super(PSWalletTestCase, self).tearDown()
        shutil.rmtree(self.user_dir)

    def test_ps_coin_rounds_str(self):
        assert ps_coin_rounds_str(PSCoinRounds.MINUSINF) == 'Unknown'
        assert ps_coin_rounds_str(PSCoinRounds.OTHER) == 'Other'
        assert ps_coin_rounds_str(PSCoinRounds.MIX_ORIGIN) == 'Mix Origin'
        assert ps_coin_rounds_str(PSCoinRounds.COLLATERAL) == 'Collateral'

    def test_PSTxData(self):
        psman = self.wallet.psman
        tx_type = PSTxTypes.NEW_DENOMS
        raw_tx = '02000000000000000000'
        txid = '0'*64
        uuid = 'uuid'
        tx_data = PSTxData(uuid=uuid, txid=txid,
                           raw_tx=raw_tx, tx_type=tx_type)
        assert tx_data.txid == txid
        assert tx_data.raw_tx == raw_tx
        assert tx_data.tx_type == int(tx_type)
        assert tx_data.uuid == uuid
        assert tx_data.sent is None
        assert tx_data.next_send is None

        # test _as_dict
        d = tx_data._as_dict()
        assert d == {txid: (uuid, None, None, int(tx_type), raw_tx)}

        # test _from_txid_and_tuple
        new_tx_data = PSTxData._from_txid_and_tuple(txid, d[txid])
        assert id(new_tx_data) != id(tx_data)
        assert new_tx_data == tx_data

        # test send
        t1 = time.time()
        psman.network = NetworkBroadcastMock(pass_cnt=1)
        coro = tx_data.send(psman)
        asyncio.get_event_loop().run_until_complete(coro)
        t2 = time.time()
        assert t2 > tx_data.sent > t1

        # test next_send
        tx_data.sent = None
        t1 = time.time()
        psman.network = NetworkBroadcastMock(pass_cnt=0)
        coro = tx_data.send(psman)
        asyncio.get_event_loop().run_until_complete(coro)
        t2 = time.time()
        assert tx_data.sent is None
        assert t2 > tx_data.next_send - 10 > t1

    def test_PSTxWorkflow(self):
        with self.assertRaises(TypeError):
            workflow = PSTxWorkflow()
        uuid = 'uuid'
        workflow = PSTxWorkflow(uuid=uuid)
        wallet = WalletGetTxHeigthMock([])
        assert workflow.uuid == 'uuid'
        assert not workflow.completed
        assert workflow.next_to_send(wallet) is None
        assert workflow.tx_data == {}
        assert workflow.tx_order == []

        raw_tx = '02000000000000000000'
        tx_type = PSTxTypes.NEW_DENOMS
        txid1 = '1'*64
        txid2 = '2'*64
        txid3 = '3'*64
        workflow.add_tx(txid=txid1, tx_type=tx_type)
        workflow.add_tx(txid=txid2, tx_type=tx_type, raw_tx=raw_tx)
        workflow.add_tx(txid=txid3, tx_type=tx_type, raw_tx=raw_tx)
        workflow.completed = True

        assert workflow.tx_order == [txid1, txid2, txid3]
        tx_data1 = workflow.tx_data[txid1]
        tx_data2 = workflow.tx_data[txid2]
        tx_data3 = workflow.tx_data[txid3]
        assert workflow.next_to_send(wallet) == tx_data1
        assert tx_data1._as_dict() == {txid1:
                                       (uuid, None, None,
                                        int(tx_type), None)}
        assert tx_data2._as_dict() == {txid2:
                                       (uuid, None, None,
                                        int(tx_type), raw_tx)}
        assert tx_data3._as_dict() == {txid3:
                                       (uuid, None, None,
                                        int(tx_type), raw_tx)}
        tx_data1.sent = time.time()
        assert workflow.next_to_send(wallet) == tx_data2

        assert workflow.pop_tx(txid2) == tx_data2
        assert workflow.next_to_send(wallet) == tx_data3

        # test next_to_send if txid has nonlocal height in wallet.get_tx_height
        wallet = WalletGetTxHeigthMock([txid3])
        assert workflow.next_to_send(wallet) is None

        # test _as_dict
        d = workflow._as_dict()
        assert id(d['tx_order']) != id(workflow.tx_order)
        assert id(d['tx_data']) != id(workflow.tx_data)
        assert d['uuid'] == uuid
        assert d['completed']
        assert d['tx_order'] == [txid1, txid3]
        assert set(d['tx_data'].keys()) == {txid1, txid3}
        assert d['tx_data'][txid1] == (uuid, tx_data1.sent, None, tx_type,
                                       tx_data1.raw_tx)
        assert d['tx_data'][txid3] == (uuid, tx_data3.sent, None, tx_type,
                                       tx_data3.raw_tx)

        # test _from_dict
        workflow2 = PSTxWorkflow._from_dict(d)
        assert id(workflow2) != id(workflow)
        assert id(d['tx_order']) != id(workflow2.tx_order)
        assert id(d['tx_data']) != id(workflow2.tx_data)
        assert workflow2 == workflow

    def test_PSDenominateWorkflow(self):
        with self.assertRaises(TypeError):
            workflow = PSDenominateWorkflow()
        uuid = 'uuid'
        workflow = PSDenominateWorkflow(uuid=uuid)
        assert workflow.uuid == 'uuid'
        assert workflow.denom == 0
        assert workflow.rounds == 0
        assert workflow.inputs == []
        assert workflow.outputs == []
        assert workflow.completed == 0

        tc = time.time()
        workflow.denom = 1
        workflow.rounds = 1
        workflow.inputs = ['12345:0', '12345:5', '12345:7']
        workflow.outputs = ['addr1', 'addr2', 'addr3']
        workflow.completed = tc

        # test _as_dict
        d = workflow._as_dict()
        data_tuple = d[uuid]
        assert data_tuple == (workflow.denom, workflow.rounds,
                              workflow.inputs, workflow.outputs,
                              workflow.completed)
        assert id(data_tuple[1]) != id(workflow.inputs)
        assert id(data_tuple[2]) != id(workflow.outputs)

        # test _from_uuid_and_tuple
        workflow2 = PSDenominateWorkflow._from_uuid_and_tuple(uuid, data_tuple)
        assert id(workflow2) != id(workflow)
        assert uuid == workflow2.uuid
        assert data_tuple[0] == workflow2.denom
        assert data_tuple[1] == workflow2.rounds
        assert data_tuple[2] == workflow2.inputs
        assert data_tuple[3] == workflow2.outputs
        assert id(data_tuple[3]) != id(workflow2.inputs)
        assert id(data_tuple[3]) != id(workflow2.outputs)
        assert data_tuple[4] == workflow2.completed
        assert workflow == workflow2

    def test_MixingStats_DSMsgStat(self):
        ms = MixingStats()
        assert ms.dsa.msg_sent == ms.dsi.msg_sent == ms.dss.msg_sent == 0
        assert ms.dsa.sent_cnt == ms.dsi.sent_cnt == ms.dss.sent_cnt == 0
        assert ms.dsa.dssu_cnt == ms.dsi.dssu_cnt == ms.dss.dssu_cnt == 0
        assert (ms.dsa.success_cnt == ms.dsi.success_cnt
                    == ms.dss.success_cnt == 0)
        assert (ms.dsa.timeout_cnt == ms.dsi.timeout_cnt
                    == ms.dss.timeout_cnt == 0)
        assert (ms.dsa.peer_closed_cnt == ms.dsi.peer_closed_cnt
                    == ms.dss.peer_closed_cnt == 0)
        assert ms.dsa.error_cnt == ms.dsi.error_cnt == ms.dss.error_cnt == 0
        assert (ms.dsa.min_wait_sec == ms.dsi.min_wait_sec
                    == ms.dss.min_wait_sec == 1e9)
        assert (ms.dsa.total_wait_sec == ms.dsi.total_wait_sec
                    == ms.dss.total_wait_sec == 0)
        assert (ms.dsa.max_wait_sec == ms.dsi.max_wait_sec
                    == ms.dss.max_wait_sec == 0)

        t0 = time.time()
        ms.dsa.send_msg()
        t1 = time.time()
        assert t0 <= ms.dsa.msg_sent <= t1
        ms.dsa.on_dssu()
        assert ms.dsa.dssu_cnt == 1
        time.sleep(0.0001)
        ms.dsa.on_read_msg()
        assert ms.dsa.success_cnt == 1
        assert ms.dsa.min_wait_sec > 0
        assert ms.dsa.total_wait_sec > 0
        assert ms.dsa.max_wait_sec > 0

        ms.dsi.send_msg()
        ms.on_timeout()
        assert ms.dsi.success_cnt == 0
        assert ms.dsi.timeout_cnt == 1

        ms.dss.send_msg()
        ms.on_peer_closed()
        assert ms.dss.success_cnt == 0
        assert ms.dss.peer_closed_cnt == 1

        ms.dsa.send_msg()
        ms.on_error()
        assert ms.dsa.success_cnt == 1
        assert ms.dsa.error_cnt == 1

    def test_find_untracked_ps_txs(self):
        w = self.wallet
        psman = w.psman
        ps_txs = w.db.get_ps_txs()
        ps_denoms = w.db.get_ps_denoms()
        ps_spent_denoms = w.db.get_ps_spent_denoms()
        ps_spent_collaterals = w.db.get_ps_spent_collaterals()
        assert len(ps_txs) == 0
        assert len(ps_denoms) == 0
        assert len(ps_spent_denoms) == 0
        assert len(ps_spent_collaterals) == 0
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert c_outpoint is None

        coro = psman.find_untracked_ps_txs(log=False)
        found_txs = asyncio.get_event_loop().run_until_complete(coro)
        assert found_txs == 86
        assert len(ps_txs) == 86
        assert len(ps_denoms) == 131
        assert len(ps_spent_denoms) == 179
        assert len(ps_spent_collaterals) == 6
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert c_outpoint == ('9b6cfb93fe6b002e0c60833fa9bcbeef'
                              '057673ebae64d05864827b5dd808fb23:0')
        assert ps_collateral == ('yiozDzgTrjyXqie28y7z2YEmjaYUZ7gveQ', 20000)

        coro = psman.find_untracked_ps_txs(log=False)
        found_txs = asyncio.get_event_loop().run_until_complete(coro)
        assert found_txs == 0
        assert len(ps_txs) == 86
        assert len(ps_denoms) == 131
        assert len(ps_spent_denoms) == 179
        assert len(ps_spent_collaterals) == 6
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert c_outpoint == ('9b6cfb93fe6b002e0c60833fa9bcbeef'
                              '057673ebae64d05864827b5dd808fb23:0')
        assert ps_collateral == ('yiozDzgTrjyXqie28y7z2YEmjaYUZ7gveQ', 20000)

    def test_ps_history_show_all(self):
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        # check with show_dip2_tx_type on
        self.config.set_key('show_dip2_tx_type', True, True)
        h = self.wallet.get_detailed_history()
        h_f = self.wallet.get_full_history()
        assert h['summary']['end']['BTC_balance'] == Satoshis(1484831773)
        txs = h['transactions']
        txf = list(h_f.values())
        assert len(txs) == 88
        assert len(txf) == 88
        for i in [1, 6, 7, 10, 11, 83, 84, 85, 86]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
        for i in [51]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
        for i in [2, 3, 4, 5, 8, 9, 12, 13, 14, 15, 16, 81]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(18, 36):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(37, 49):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(52, 64):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(65, 80):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in [17, 36, 49, 50, 64, 80]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PAY_COLLATERAL]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PAY_COLLATERAL]
        for i in [82]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PRIVATESEND]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PRIVATESEND]
        for tx in txs:
            assert not tx['group_txid']
            assert tx['group_data'] == []
        for tx in txf:
            assert not tx['group_txid']
            assert tx['group_data'] == []
        # check with show_dip2_tx_type off
        self.config.set_key('show_dip2_tx_type', False, True)
        h = self.wallet.get_detailed_history()
        h_f = self.wallet.get_full_history()
        assert h['summary']['end']['BTC_balance'] == Satoshis(1484831773)
        txs = h['transactions']
        txf = list(h_f.values())
        assert len(txs) == 88
        assert len(txf) == 88
        for tx in txs:
            assert not tx['group_txid']
            assert tx['group_data'] == []
        for tx in txf:
            assert not tx['group_txid']
            assert tx['group_data'] == []

    def test_ps_history_show_grouped(self):
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        # check with show_dip2_tx_type off
        self.config.set_key('show_dip2_tx_type', False, True)
        h = self.wallet.get_detailed_history(group_ps=True)
        h_f = self.wallet.get_full_history(group_ps=True)
        end_balance = Satoshis(1484831773)
        assert h['summary']['end']['BTC_balance'] == end_balance
        txs = h['transactions']
        txf = list(h_f.values())
        group0 = txs[81]['group_data']
        group0f = txf[81]['group_data']
        group0_val = Satoshis(-64144)
        group0_balance = Satoshis(1599935856)
        group0_txs_cnt = 81
        group1 = txs[86]['group_data']
        group1f = txf[86]['group_data']
        group1_txs = ['d9565c9cf5d819acb0f94eca4522c442'
                      'f40d8ebee973f6f0896763af5868db4b',
                      '4a256db62ff0c1764d6eeb8708b87d8a'
                      'c61c6c0f8c17db76d8a0c11dcb6477cb',
                      'a58b8396f95489e2f47769ac085e7fb9'
                      '4a2502ed8e32f617927c2f818c41b099',
                      '612bee0394963117251c006c64676c16'
                      '2aa98bd257094f017ae99b4003dfbbab']
        group1_val = Satoshis(-2570)
        group1_balance = Satoshis(1484832135)
        group1_txs_cnt = 4

        # group is tuple: (val, balance, ['txid1', 'txid2, ...])
        assert group0[0] == group0f[0] == group0_val
        assert group0[1] == group0f[1] == group0_balance
        assert len(group0[2]) == len(group0f[2]) == group0_txs_cnt
        assert group1[0] == group1f[0] == group1_val
        assert group1[1] == group1f[1] == group1_balance
        assert len(group1[2]) == len(group1f[2]) == group1_txs_cnt
        assert group1[2] == group1[2] == group1_txs

        for i, tx in enumerate(txs):
            if i not in [81, 86]:
                assert txs[i]['group_data'] == []
                assert txf[i]['group_data'] == []
            if i in [0, 81, 82, 86, 87]:
                assert not txs[i]['group_txid']
                assert not txf[i]['group_txid']
            if i in range(1, 81):
                assert txf[i]['group_txid'] == txf[81]['txid']
                assert txf[i]['group_txid'] == txf[81]['txid']
            if i in range(83, 86):
                assert txs[i]['group_txid'] == txs[86]['txid']
                assert txf[i]['group_txid'] == txf[86]['txid']

        # check with show_dip2_tx_type on
        self.config.set_key('show_dip2_tx_type', True, True)
        h = self.wallet.get_detailed_history(group_ps=True)
        h_f = self.wallet.get_full_history(group_ps=True)
        assert h['summary']['end']['BTC_balance'] == end_balance
        txs = h['transactions']
        txf = list(h_f.values())
        for i in [1, 6, 7, 10, 11, 83, 84, 85, 86]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
        for i in [51]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
        for i in [2, 3, 4, 5, 8, 9, 12, 13, 14, 15, 16, 81]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(18, 36):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(37, 49):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(52, 64):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in range(65, 80):
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.DENOMINATE]
        for i in [17, 36, 49, 50, 64, 80]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PAY_COLLATERAL]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PAY_COLLATERAL]
        for i in [82]:
            assert txs[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PRIVATESEND]
            assert txf[i]['tx_type'] == SPEC_TX_NAMES[PSTxTypes.PRIVATESEND]

        group0 = txs[81]['group_data']
        group0f = txf[81]['group_data']
        group1 = txs[86]['group_data']
        group1f = txf[86]['group_data']
        assert group0[0] == group0f[0] == group0_val
        assert group0[1] == group0f[1] == group0_balance
        assert len(group0[2]) == len(group0f[2]) == group0_txs_cnt
        assert group1[0] == group1f[0] == group1_val
        assert group1[1] == group1f[1] == group1_balance
        assert len(group1[2]) == len(group1f[2]) == group1_txs_cnt
        assert group1[2] == group1f[2] == group1_txs

        for i, tx in enumerate(txs):
            if i not in [81, 86]:
                assert txs[i]['group_data'] == []
            if i in [0, 81, 82, 86, 87]:
                assert not txs[i]['group_txid']
            if i in range(1, 81):
                assert txs[i]['group_txid'] == txs[81]['txid']
            if i in range(83, 86):
                assert txs[i]['group_txid'] == txs[86]['txid']
        for i, tx in enumerate(txf):
            if i not in [81, 86]:
                assert txf[i]['group_data'] == []
            if i in [0, 81, 82, 86, 87]:
                assert not txf[i]['group_txid']
            if i in range(1, 81):
                assert txf[i]['group_txid'] == txf[81]['txid']
            if i in range(83, 86):
                assert txf[i]['group_txid'] == txf[86]['txid']

    def test_ps_get_utxos_all(self):
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        ps_denoms = self.wallet.db.get_ps_denoms()
        for utxo in self.wallet.get_utxos():
            ps_rounds = utxo.ps_rounds
            ps_denom = ps_denoms.get(utxo.prevout.to_str())
            if ps_denom:
                assert ps_rounds == ps_denom[2]
            else:
                assert ps_rounds is None

    def test_get_balance(self):
        wallet = self.wallet
        psman = wallet.psman
        assert wallet.get_balance() == (1484831773, 0, 0)
        assert wallet.get_balance(include_ps=False) == (1484831773, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=5) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=4) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=3) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=2) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=1) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=0) == (0, 0, 0)

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        assert wallet.get_balance() == (1484831773, 0, 0)
        assert wallet.get_balance(include_ps=False) == (984806773, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=5) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=4) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=3) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=2) == \
            (384803848, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=1) == \
            (384903849, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=0) == \
            (500005000, 0, 0)

        # check balance with ps_other
        w = wallet
        coins = w.get_spendable_coins(domain=None)
        denom_addr = list(w.db.get_ps_denoms().values())[0][0]
        outputs = [PartialTxOutput.from_address_and_value(denom_addr, 300000)]
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        w.sign_transaction(tx, None)
        txid = tx.txid()
        w.add_transaction(tx)
        w.db.add_islock(txid)

        # check when transaction is standard
        assert wallet.get_balance() == (1484831547, 0, 0)
        assert wallet.get_balance(include_ps=False) == (984506547, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=5) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=4) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=3) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=2) == \
            (384803848, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=1) == \
            (384903849, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=0) == \
            (500005000, 0, 0)

        coro = psman.find_untracked_ps_txs(log=True)
        asyncio.get_event_loop().run_until_complete(coro)

        # check when transaction is other ps coins
        assert wallet.get_balance() == (1484831547, 0, 0)
        assert wallet.get_balance(include_ps=False) == (984506547, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=5) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=4) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=3) == (0, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=2) == \
            (384803848, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=1) == \
            (384903849, 0, 0)
        assert wallet.get_balance(include_ps=False, min_rounds=0) == \
            (500005000, 0, 0)

    def test_get_ps_addresses(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        assert self.wallet.db.get_ps_addresses() == set()
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        assert len(self.wallet.db.get_ps_addresses()) == 317
        assert len(self.wallet.db.get_ps_addresses(min_rounds=C_RNDS)) == 317
        assert len(self.wallet.db.get_ps_addresses(min_rounds=0)) == 131
        assert len(self.wallet.db.get_ps_addresses(min_rounds=1)) == 78
        assert len(self.wallet.db.get_ps_addresses(min_rounds=2)) == 77
        assert len(self.wallet.db.get_ps_addresses(min_rounds=3)) == 0

    def test_get_spendable_coins(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        coins = self.wallet.get_spendable_coins(None)
        assert len(coins) == 6
        for c in coins:
            assert c.ps_rounds is None

        coins = self.wallet.get_spendable_coins(None, include_ps=True)
        assert len(coins) == 138
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert rounds[None] == 6
        assert rounds[C_RNDS] == 1
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_spendable_coins(None, min_rounds=C_RNDS)
        assert len(coins) == 132
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert rounds[C_RNDS] == 1
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_spendable_coins(None, min_rounds=0)
        assert len(coins) == 131
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_spendable_coins(None, min_rounds=1)
        assert len(coins) == 78
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert 0 not in rounds
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_spendable_coins(None, min_rounds=2)
        assert len(coins) == 77
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert 0 not in rounds
        assert 1 not in rounds
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_spendable_coins(None, min_rounds=3)
        assert len(coins) == 0

    def test_get_spendable_coins_allow_others(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        # add other coins
        coins = w.get_spendable_coins(domain=None)
        denom_addr = list(w.db.get_ps_denoms().values())[0][0]
        outputs = [PartialTxOutput.from_address_and_value(denom_addr, 300000)]
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        w.sign_transaction(tx, None)
        txid = tx.txid()
        w.add_transaction(tx)
        w.db.add_islock(txid)
        coro = psman.find_untracked_ps_txs(log=True)
        asyncio.get_event_loop().run_until_complete(coro)

        assert not psman.allow_others
        coins = w.get_spendable_coins(domain=None, include_ps=True)
        cset = set([c.ps_rounds for c in coins])
        assert cset == {None, 0, 1, 2, PSCoinRounds.COLLATERAL}
        assert len(coins) == 138

        psman.allow_others = True
        coins = w.get_spendable_coins(domain=None, include_ps=True)
        cset = set([c.ps_rounds for c in coins])
        assert cset == {None, 0, 1, 2,
                        PSCoinRounds.COLLATERAL, PSCoinRounds.OTHER}
        assert len(coins) == 139

    def test_get_utxos(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        coins = self.wallet.get_utxos()
        assert len(coins) == 6
        for c in coins:
            assert c.ps_rounds is None

        coins = self.wallet.get_utxos(include_ps=True)
        assert len(coins) == 138
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert rounds[None] == 6
        assert rounds[C_RNDS] == 1
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0
        assert rounds[4] == 0

        coins = self.wallet.get_utxos(min_rounds=C_RNDS)
        assert len(coins) == 132
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert rounds[C_RNDS] == 1
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0
        assert rounds[4] == 0

        coins = self.wallet.get_utxos(min_rounds=0)
        assert len(coins) == 131
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert rounds[0] == 53
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_utxos(min_rounds=1)
        assert len(coins) == 78
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert 0 not in rounds
        assert rounds[1] == 1
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_utxos(min_rounds=2)
        assert len(coins) == 77
        rounds = defaultdict(int)
        for c in coins:
            rounds[c.ps_rounds] += 1
        assert None not in rounds
        assert 0 not in rounds
        assert 1 not in rounds
        assert rounds[2] == 77
        assert rounds[3] == 0

        coins = self.wallet.get_utxos(min_rounds=3)
        assert len(coins) == 0

    def test_keep_amount(self):
        psman = self.wallet.psman
        assert psman.keep_amount == psman.DEFAULT_KEEP_AMOUNT

        psman.keep_amount = psman.min_keep_amount - 0.1
        assert psman.keep_amount == psman.min_keep_amount

        psman.keep_amount = psman.max_keep_amount + 0.1
        assert psman.keep_amount == psman.max_keep_amount

        psman.keep_amount = 5
        assert psman.keep_amount == 5

        psman.state = PSStates.Mixing
        psman.keep_amount = 10
        assert psman.keep_amount == 5

    def test_limit_spend_fee(self):
        psman = self.wallet.psman
        assert psman.limit_spend_fee == psman.DEFAULT_LIMIT_SPEND_FEE

        psman.limit_spend_fee = False
        assert psman.limit_spend_fee == False

        psman.limit_spend_fee = ''
        assert psman.limit_spend_fee == False

        psman.limit_spend_fee = True
        assert psman.limit_spend_fee == True

        psman.limit_spend_fee = 'yes'
        assert psman.limit_spend_fee == True

        assert psman.limit_spend_fee_data()
        assert psman.limit_spend_fee_data(full_txt=True)

    def test_keep_amount_on_abs_denoms_calc(self):
        cur_cnt = {}
        d1, d2, d3, d4, d5 = PS_DENOMS_VALS

        def mock_calc_denoms_by_values():
            return cur_cnt

        psman = self.wallet.psman
        psman.calc_denoms_by_values = mock_calc_denoms_by_values
        assert psman.keep_amount == psman.DEFAULT_KEEP_AMOUNT

        psman.calc_denoms_method = psman.CalcDenomsMethod.ABS
        assert psman.keep_amount == 0

        psman.keep_amount = 50 * psman.DEFAULT_KEEP_AMOUNT
        assert psman.keep_amount == 0

        psman.calc_denoms_method = psman.CalcDenomsMethod.DEF
        assert psman.keep_amount == psman.DEFAULT_KEEP_AMOUNT
        psman.calc_denoms_method = psman.CalcDenomsMethod.ABS

        cur_cnt.update({d1: 10, d2: 10, d3: 1, d4: 0, d5: 0})
        assert psman.keep_amount == 0
        psman.abs_denoms_cnt = {d1: 20, d2: 0, d3: 0, d4: 10, d5: 1}
        assert psman.keep_amount == (d1*20 + d4*10 + d5)/COIN
        psman.abs_denoms_cnt = {d1: 0, d2: 0, d3: 0, d4: 0, d5: 1}
        assert psman.keep_amount == d5/COIN

    def test_mix_rounds(self):
        psman = self.wallet.psman
        assert psman.mix_rounds == psman.DEFAULT_MIX_ROUNDS

        psman.mix_rounds = psman.min_mix_rounds - 1
        assert psman.mix_rounds == psman.min_mix_rounds

        psman.mix_rounds = psman.max_mix_rounds + 1
        assert psman.mix_rounds == psman.max_mix_rounds

        psman.mix_rounds = 3
        assert psman.mix_rounds == 3

        psman.state = PSStates.Mixing
        psman.mix_rounds = 4
        assert psman.mix_rounds == 3

    def test_group_origin_coins_by_addr(self):
        psman = self.wallet.psman
        assert not psman.group_origin_coins_by_addr
        psman.group_origin_coins_by_addr = 1
        assert psman.group_origin_coins_by_addr
        assert psman.group_origin_coins_by_addr is True
        psman.group_origin_coins_by_addr = 0
        assert not psman.group_origin_coins_by_addr
        assert psman.group_origin_coins_by_addr is False

    def test_gather_mix_stat(self):
        psman = self.wallet.psman
        assert not psman.gather_mix_stat
        psman.gather_mix_stat = 1
        assert psman.gather_mix_stat
        assert psman.gather_mix_stat is True
        psman.gather_mix_stat = 0
        assert not psman.gather_mix_stat
        assert psman.gather_mix_stat is False

    def test_check_min_rounds(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        psman = self.wallet.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        coins = self.wallet.get_utxos()
        with self.assertRaises(PSMinRoundsCheckFailed):
            psman.check_min_rounds(coins, 0)

        coins = self.wallet.get_utxos(include_ps=True)
        with self.assertRaises(PSMinRoundsCheckFailed):
            psman.check_min_rounds(coins, 0)

        coins = self.wallet.get_utxos(min_rounds=C_RNDS)
        with self.assertRaises(PSMinRoundsCheckFailed):
            psman.check_min_rounds(coins, 0)

        coins = self.wallet.get_utxos(min_rounds=0)
        psman.check_min_rounds(coins, 0)

        coins = self.wallet.get_utxos(min_rounds=1)
        psman.check_min_rounds(coins, 1)

        coins = self.wallet.get_utxos(min_rounds=2)
        psman.check_min_rounds(coins, 2)

    def test_mixing_progress(self):
        psman = self.wallet.psman
        psman.mix_rounds = 2
        assert psman.mixing_progress() == 0
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.mixing_progress() == 77
        psman.mix_rounds = 3
        assert psman.mixing_progress() == 51
        psman.mix_rounds = 4
        assert psman.mixing_progress() == 38
        psman.mix_rounds = 5
        assert psman.mixing_progress() == 31

    def test_calc_denoms_method(self):
        psman = self.wallet.psman
        assert psman.calc_denoms_method == psman.CalcDenomsMethod.DEF
        psman.calc_denoms_method = psman.CalcDenomsMethod.ABS
        assert psman.calc_denoms_method == psman.CalcDenomsMethod.ABS

        with self.assertRaises(AssertionError):
            psman.calc_denoms_method = -1
        assert psman.calc_denoms_method == psman.CalcDenomsMethod.ABS

        assert psman.calc_denoms_method_str(psman.CalcDenomsMethod.DEF) == \
            psman.CALC_DENOMS_METHOD_STR[psman.CalcDenomsMethod.DEF]
        assert psman.calc_denoms_method_str(psman.CalcDenomsMethod.ABS) == \
            psman.CALC_DENOMS_METHOD_STR[psman.CalcDenomsMethod.ABS]
        with self.assertRaises(AssertionError):
            psman.calc_denoms_method_str(psman.CalcDenomsMethod.DEF.value - 1)

        assert psman.calc_denoms_method_data()
        assert psman.calc_denoms_method_data(full_txt=True)

    def test_abs_denoms_cnt(self):
        psman = self.wallet.psman
        abs_cnt = psman.abs_denoms_cnt
        with self.assertRaises(AssertionError):
            psman.abs_denoms_cnt = {}
        with self.assertRaises(AssertionError):
            psman.abs_denoms_cnt = {v: 20 for v in PS_DENOMS_VALS[1:]}
        psman.abs_denoms_cnt = abs_cnt
        abs_cnt.update({100001: 10, 1000010: 30})
        psman.abs_denoms_cnt = copy.deepcopy(abs_cnt)
        assert psman.abs_denoms_cnt == abs_cnt

    def test_is_waiting(self):
        psman = self.wallet.psman
        assert not psman.is_waiting
        psman.state = PSStates.Mixing
        assert psman.is_waiting

        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_new_denoms_wfl(wfl)
        assert not psman.is_waiting
        psman.clear_new_denoms_wfl()
        assert psman.is_waiting

        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_new_collateral_wfl(wfl)
        assert not psman.is_waiting
        psman.clear_new_collateral_wfl()
        assert psman.is_waiting

        wfl = PSDenominateWorkflow(uuid='uuid')
        psman.set_denominate_wfl(wfl)
        assert not psman.is_waiting
        psman.clear_denominate_wfl('uuid')
        assert psman.is_waiting

        psman.keypairs_state = KPStates.Empty
        assert psman.is_waiting
        psman.keypairs_state = KPStates.NeedCache
        assert not psman.is_waiting
        psman.keypairs_state = KPStates.Caching
        assert not psman.is_waiting

    def test_get_change_addresses_for_new_transaction(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        unused1 = w.calc_unused_change_addresses()
        assert len(unused1) == 17
        for addr in unused1:
            psman.add_ps_reserved(addr, 'test')
        unused2 = w.calc_unused_change_addresses()
        assert len(unused2) == 0
        for i in range(100):
            addrs = w.get_change_addresses_for_new_transaction()
            for addr in addrs:
                assert addr not in unused1

    def test_synchronize_sequence(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        unused1 = w.get_unused_addresses()
        assert len(unused1) == 20

        w.synchronize_sequence(for_change=False)

        unused1 = w.get_unused_addresses()
        assert len(unused1) == 20
        for addr in unused1:
            psman.add_ps_reserved(addr, 'test')
        unused2 = w.get_unused_addresses()
        assert len(unused2) == 0

        w.synchronize_sequence(for_change=False)
        unused2 = w.get_unused_addresses()
        assert len(unused2) == 0

    def test_synchronize_sequence_for_change(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        unused1 = w.calc_unused_change_addresses()
        assert len(unused1) == 17

        w.synchronize_sequence(for_change=True)

        unused1 = w.calc_unused_change_addresses()
        assert len(unused1) == 17
        for addr in unused1:
            psman.add_ps_reserved(addr, 'test')
        unused2 = w.calc_unused_change_addresses()
        assert len(unused2) == 0

        w.synchronize_sequence(for_change=True)
        unused2 = w.calc_unused_change_addresses()
        assert len(unused2) == 0

    def test_reserve_addresses(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        ps_addrs = w.db.get_ps_addresses()
        assert len(set(w.get_receiving_addresses()) - ps_addrs) == 21
        assert len(w.get_receiving_addresses()) == 333
        assert len(set(w.get_change_addresses()) - ps_addrs) == 22
        assert len(w.get_change_addresses()) == 27
        unused_change = w.calc_unused_change_addresses()
        assert len(unused_change) == 17
        unused = w.get_unused_addresses()
        assert len(unused) == 20

        res1 = psman.reserve_addresses(10)
        assert len(res1) == 10
        res2 = psman.reserve_addresses(1, for_change=True, data='coll')
        assert len(res2) == 1
        sel1 = w.db.select_ps_reserved()
        sel2 = w.db.select_ps_reserved(for_change=True, data='coll')
        assert res1 == sel1
        assert res2 == sel2
        unused = w.get_unused_addresses()
        for a in sel1:
            assert a not in unused
        assert len(unused) == 10
        unused_change = w.calc_unused_change_addresses()
        for a in sel2:
            assert a not in unused_change
        assert len(unused_change) == 16

        ps_addrs = w.db.get_ps_addresses()
        assert len(set(w.get_receiving_addresses()) - ps_addrs) == 11
        assert len(w.get_receiving_addresses()) == 333
        assert len(set(w.get_change_addresses()) - ps_addrs) == 21
        assert len(w.get_change_addresses()) == 27

    def test_first_unused_index(self):
        w = self.wallet
        psman = w.psman

        assert psman.first_unused_index() == 313
        assert psman.first_unused_index(for_change=True) == 7

        assert w.db.num_receiving_addresses() == 333
        assert w.db.num_change_addresses() == 27
        assert len(w.get_unused_addresses()) == 20
        assert len(w.calc_unused_change_addresses()) == 17

        psman.reserve_addresses(20)
        psman.reserve_addresses(13, for_change=True)

        assert psman.first_unused_index() == 333
        assert psman.first_unused_index(for_change=True) == 23

        assert w.db.num_receiving_addresses() == 333
        assert w.db.num_change_addresses() == 27
        assert len(w.get_unused_addresses()) == 0
        assert len(w.calc_unused_change_addresses()) == 4

    def test_add_ps_collateral(self):
        w = self.wallet
        outpoint0 = '0'*64 + ':0'
        collateral0 = (w.dummy_address(), 10000)
        outpoint1 = '1'*64 + ':1'
        collateral1 = (w.dummy_address(), 40000)
        w.db.add_ps_collateral(outpoint0, collateral0)
        assert set(w.db.ps_collaterals.keys()) == {outpoint0}
        assert w.db.ps_collaterals[outpoint0] == collateral0
        w.db.add_ps_collateral(outpoint1, collateral1)
        assert set(w.db.ps_collaterals.keys()) == {outpoint0, outpoint1}
        assert w.db.ps_collaterals[outpoint1] == collateral1

    def test_pop_ps_collateral(self):
        w = self.wallet
        outpoint0 = '0'*64 + ':0'
        collateral0 = (w.dummy_address(), 10000)
        outpoint1 = '1'*64 + ':1'
        collateral1 = (w.dummy_address(), 40000)
        w.db.add_ps_collateral(outpoint0, collateral0)
        w.db.add_ps_collateral(outpoint1, collateral1)
        w.db.pop_ps_collateral(outpoint1)
        assert set(w.db.ps_collaterals.keys()) == {outpoint0}
        assert w.db.ps_collaterals[outpoint0] == collateral0
        w.db.pop_ps_collateral(outpoint0)
        assert set(w.db.ps_collaterals.keys()) == set()

    def test_get_ps_collateral(self):
        w = self.wallet
        psman = w.psman
        outpoint0 = '0'*64 + ':0'
        collateral0 = (w.dummy_address(), 10000)
        outpoint1 = '1'*64 + ':1'
        collateral1 = (w.dummy_address(), 40000)

        w.db.add_ps_collateral(outpoint0, collateral0)
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert psman.ps_collateral_cnt
        assert c_outpoint == outpoint0
        assert ps_collateral == collateral0

        w.db.add_ps_collateral(outpoint1, collateral1)
        assert psman.ps_collateral_cnt == 2
        with self.assertRaises(Exception):  # multiple values
            assert w.db.get_ps_collateral()

        assert w.db.get_ps_collateral(outpoint0) == collateral0
        assert w.db.get_ps_collateral(outpoint1) == collateral1

        w.db.pop_ps_collateral(outpoint0)
        w.db.pop_ps_collateral(outpoint1)
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert not psman.ps_collateral_cnt
        assert c_outpoint is None
        assert ps_collateral is None

    def test_add_ps_denom(self):
        w = self.wallet
        psman = w.psman
        outpoint = '0'*64 + ':0'
        denom = (w.dummy_address(), 100001, 0)
        assert w.db.ps_denoms == {}
        assert psman._ps_denoms_amount_cache == 0
        psman.add_ps_denom(outpoint, denom)
        assert w.db.ps_denoms == {outpoint: denom}
        assert psman._ps_denoms_amount_cache == 100001

    def test_pop_ps_denom(self):
        w = self.wallet
        psman = w.psman
        outpoint1 = '0'*64 + ':0'
        outpoint2 = '1'*64 + ':0'
        denom1 = (w.dummy_address(), 100001, 0)
        denom2 = (w.dummy_address(), 1000010, 0)
        assert w.db.ps_denoms == {}
        assert psman._ps_denoms_amount_cache == 0
        psman.add_ps_denom(outpoint1, denom1)
        psman.add_ps_denom(outpoint2, denom2)
        assert w.db.ps_denoms == {outpoint1: denom1, outpoint2: denom2}
        assert psman._ps_denoms_amount_cache == 1100011
        assert denom2 == psman.pop_ps_denom(outpoint2)
        assert w.db.ps_denoms == {outpoint1: denom1}
        assert psman._ps_denoms_amount_cache == 100001
        assert denom1 == psman.pop_ps_denom(outpoint1)
        assert w.db.ps_denoms == {}
        assert psman._ps_denoms_amount_cache == 0

    def test_ps_origin_addrs(self):
        w = self.wallet
        psman = w.psman
        txid1 = 'txid1'
        txid2 = 'txid2'
        txid3 = 'txid3'
        addr1 = 'addr1'
        addr2 = 'addr2'
        addr3 = 'addr3'
        assert w.db.get_ps_origin_addrs() == []

        w.db.add_ps_origin_addrs(txid1, addr1)
        assert w.db.get_ps_origin_addrs() == [addr1]
        w.db.add_ps_origin_addrs(txid2, [addr1, addr2])
        assert sorted(w.db.get_ps_origin_addrs()) == [addr1, addr2]
        w.db.add_ps_origin_addrs(txid3, [addr2, addr3])
        assert sorted(w.db.get_ps_origin_addrs()) == [addr1, addr2, addr3]

        assert w.db.get_tx_ps_origin_addrs(txid3) == [addr2, addr3]
        assert w.db.pop_ps_origin_addrs(txid3) == [addr2, addr3]
        assert w.db.pop_ps_origin_addrs(txid3) is None
        assert sorted(w.db.get_ps_origin_addrs()) == [addr1, addr2]

        assert w.db.get_tx_ps_origin_addrs(txid2) == [addr1, addr2]
        assert w.db.pop_ps_origin_addrs(txid2) == [addr1, addr2]
        assert w.db.pop_ps_origin_addrs(txid2) is None
        assert sorted(w.db.get_ps_origin_addrs()) == [addr1]

        assert w.db.get_tx_ps_origin_addrs(txid1) == [addr1]
        assert w.db.pop_ps_origin_addrs(txid1) == [addr1]
        assert w.db.pop_ps_origin_addrs(txid1) is None
        assert sorted(w.db.get_ps_origin_addrs()) == []

    def test_denoms_to_mix_cache(self):
        w = self.wallet
        psman = w.psman
        psman.mix_rounds = 2
        outpoint1 = 'outpoint1'
        outpoint2 = 'outpoint2'
        outpoint3 = 'outpoint3'
        outpoint4 = 'outpoint4'
        denom1 = ('addr1', 100001, 0)
        denom2 = ('addr2', 100001, 1)
        denom3 = ('addr3', 100001, 1)
        denom4 = ('addr4', 100001, 2)

        assert psman._denoms_to_mix_cache == {}
        psman.add_ps_denom(outpoint1, denom1)
        psman.add_ps_denom(outpoint2, denom2)
        psman.add_ps_denom(outpoint3, denom3)
        psman.add_ps_denom(outpoint4, denom4)

        assert len(psman._denoms_to_mix_cache) == 3
        psman.add_ps_spending_denom(outpoint1, 'uuid')
        assert len(psman._denoms_to_mix_cache) == 2
        psman.add_ps_spending_denom(outpoint2, 'uuid')
        assert len(psman._denoms_to_mix_cache) == 1
        psman.mix_rounds = 3
        assert len(psman._denoms_to_mix_cache) == 2

        psman.pop_ps_denom(outpoint1)
        assert len(psman._denoms_to_mix_cache) == 2
        psman.pop_ps_spending_denom(outpoint1)
        assert len(psman._denoms_to_mix_cache) == 2
        psman.pop_ps_spending_denom(outpoint2)
        assert len(psman._denoms_to_mix_cache) == 3

        psman.pop_ps_denom(outpoint1)
        psman.pop_ps_denom(outpoint2)
        psman.pop_ps_denom(outpoint3)
        psman.pop_ps_denom(outpoint4)

        psman.mix_rounds = 2
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        denoms = psman._denoms_to_mix_cache
        assert len(denoms) == 54
        for outpoint, denom in denoms.items():
            assert denom[2] < psman.mix_rounds

        psman.mix_rounds = 3
        denoms = psman._denoms_to_mix_cache
        assert len(denoms) == 131
        for outpoint, denom in denoms.items():
            assert denom[2] < psman.mix_rounds

    def test_denoms_to_mix(self):
        w = self.wallet
        psman = w.psman
        psman.mix_rounds = 2
        outpoint1 = 'outpoint1'
        outpoint2 = 'outpoint2'
        outpoint3 = 'outpoint3'
        outpoint4 = 'outpoint4'
        denom1 = ('addr1', 100001, 0)
        denom2 = ('addr2', 100001, 1)
        denom3 = ('addr3', 100001, 1)
        denom4 = ('addr4', 100001, 2)

        assert psman.denoms_to_mix() == {}
        psman.add_ps_denom(outpoint1, denom1)
        psman.add_ps_denom(outpoint2, denom2)
        psman.add_ps_denom(outpoint3, denom3)
        psman.add_ps_denom(outpoint4, denom4)

        assert len(psman.denoms_to_mix(mix_rounds=0)) == 1
        assert len(psman.denoms_to_mix(mix_rounds=1)) == 2
        assert len(psman.denoms_to_mix(mix_rounds=2)) == 1
        assert len(psman.denoms_to_mix(mix_rounds=3)) == 0

        assert len(psman.denoms_to_mix(mix_rounds=1, denom_value=100001)) == 2
        assert len(psman.denoms_to_mix(mix_rounds=1, denom_value=1000010)) == 0

        assert len(psman.denoms_to_mix()) == 3
        psman.add_ps_spending_denom(outpoint1, 'uuid')
        assert len(psman.denoms_to_mix()) == 2
        psman.add_ps_spending_denom(outpoint2, 'uuid')
        assert len(psman.denoms_to_mix()) == 1
        psman.mix_rounds = 3
        assert len(psman.denoms_to_mix()) == 2

        psman.pop_ps_denom(outpoint1)
        assert len(psman.denoms_to_mix()) == 2
        psman.pop_ps_spending_denom(outpoint1)
        assert len(psman.denoms_to_mix()) == 2
        psman.pop_ps_spending_denom(outpoint2)
        assert len(psman.denoms_to_mix()) == 3

        psman.pop_ps_denom(outpoint1)
        psman.pop_ps_denom(outpoint2)
        psman.pop_ps_denom(outpoint3)
        psman.pop_ps_denom(outpoint4)

        psman.mix_rounds = 2
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        denoms = psman.denoms_to_mix()
        assert len(denoms) == 54
        for outpoint, denom in denoms.items():
            assert denom[2] < psman.mix_rounds

        psman.mix_rounds = 3
        denoms = psman.denoms_to_mix()
        assert len(denoms) == 131
        for outpoint, denom in denoms.items():
            assert denom[2] < psman.mix_rounds

    def _prepare_workflow(self):
        uuid = 'uuid'
        raw_tx = '02000000000000000000'
        tx_type = PSTxTypes.NEW_DENOMS
        txid1 = '1'*64
        txid2 = '2'*64
        txid3 = '3'*64
        workflow = PSTxWorkflow(uuid=uuid)
        workflow.add_tx(txid=txid1, tx_type=tx_type, raw_tx=raw_tx)
        workflow.add_tx(txid=txid2, tx_type=tx_type, raw_tx=raw_tx)
        workflow.add_tx(txid=txid3, tx_type=tx_type, raw_tx=raw_tx)
        workflow.completed = True
        return workflow

    def test_pay_collateral_wfl(self):
        psman = self.wallet.psman
        assert psman.pay_collateral_wfl is None
        workflow = self._prepare_workflow()
        psman.set_pay_collateral_wfl(workflow)
        workflow2 = psman.pay_collateral_wfl
        assert id(workflow2) != id(workflow)
        assert workflow2 == workflow
        workflow2 = psman.clear_pay_collateral_wfl()
        assert psman.pay_collateral_wfl is None

    def test_new_collateral_wfl(self):
        psman = self.wallet.psman
        assert psman.new_collateral_wfl is None
        workflow = self._prepare_workflow()
        psman.set_new_collateral_wfl(workflow)
        workflow2 = psman.new_collateral_wfl
        assert id(workflow2) != id(workflow)
        assert workflow2 == workflow
        workflow2 = psman.clear_new_collateral_wfl()
        assert psman.new_collateral_wfl is None

    def test_new_denoms_wfl(self):
        psman = self.wallet.psman
        assert psman.new_denoms_wfl is None
        workflow = self._prepare_workflow()
        psman.set_new_denoms_wfl(workflow)
        workflow2 = psman.new_denoms_wfl
        assert id(workflow2) != id(workflow)
        assert workflow2 == workflow
        workflow2 = psman.clear_new_denoms_wfl()
        assert psman.new_denoms_wfl is None

    def test_denominate_wfl(self):
        uuid1 = 'uuid1'
        uuid2 = 'uuid2'
        outpoint1 = 'outpoint1'
        addr1 = 'addr1'
        psman = self.wallet.psman
        assert psman.denominate_wfl_list == []
        wfl1 = PSDenominateWorkflow(uuid=uuid1)
        wfl2 = PSDenominateWorkflow(uuid=uuid2)
        psman.set_denominate_wfl(wfl1)
        assert set(psman.denominate_wfl_list) == set([uuid1])
        wfl1.denom = 4
        wfl1.rounds = 1
        wfl1.inputs.append(outpoint1)
        wfl1.outputs.append(addr1)
        psman.set_denominate_wfl(wfl1)
        assert set(psman.denominate_wfl_list) == set([uuid1])
        psman.set_denominate_wfl(wfl2)
        assert set(psman.denominate_wfl_list) == set([uuid1, uuid2])

        dwfl_ps_data = self.wallet.db.get_ps_data('denominate_workflows')
        assert dwfl_ps_data[uuid1] == (4, 1, [outpoint1], [addr1], 0)
        assert dwfl_ps_data[uuid2] == (0, 0, [], [], 0)

        wfl1_get = psman.get_denominate_wfl(uuid1)
        assert wfl1_get == wfl1
        wfl2_get = psman.get_denominate_wfl(uuid2)
        assert wfl2_get == wfl2
        assert set(psman.denominate_wfl_list) == set([uuid1, uuid2])

        psman.clear_denominate_wfl(uuid1)
        assert set(psman.denominate_wfl_list) == set([uuid2])
        assert psman.get_denominate_wfl(uuid1) is None
        wfl2_get = psman.get_denominate_wfl(uuid2)
        assert wfl2_get == wfl2
        dwfl_ps_data = self.wallet.db.get_ps_data('denominate_workflows')
        assert dwfl_ps_data[uuid2] == (0, 0, [], [], 0)
        assert uuid1 not in dwfl_ps_data

        psman.clear_denominate_wfl(uuid2)
        assert psman.denominate_wfl_list == []
        assert psman.get_denominate_wfl(uuid1) is None
        assert psman.get_denominate_wfl(uuid2) is None

        dwfl_ps_data = self.wallet.db.get_ps_data('denominate_workflows')
        assert dwfl_ps_data == {}

    def test_spending_collaterals(self):
        uuid1 = 'uuid1'
        uuid2 = 'uuid2'
        outpoint1 = 'outpoint1'
        outpoint2 = 'outpoint2'
        w = self.wallet
        psman = w.psman
        assert w.db.get_ps_spending_collaterals() == {}
        psman.add_ps_spending_collateral(outpoint1, uuid1)
        assert len(w.db.get_ps_spending_collaterals()) == 1
        assert w.db.get_ps_spending_collateral(outpoint1) == uuid1
        psman.add_ps_spending_collateral(outpoint2, uuid2)
        assert len(w.db.get_ps_spending_collaterals()) == 2
        assert w.db.get_ps_spending_collateral(outpoint2) == uuid2
        assert w.db.get_ps_spending_collateral(outpoint1) == uuid1

        assert psman.pop_ps_spending_collateral(outpoint1) == uuid1
        assert len(w.db.get_ps_spending_collaterals()) == 1
        assert w.db.get_ps_spending_collateral(outpoint2) == uuid2
        assert psman.pop_ps_spending_collateral(outpoint2) == uuid2
        assert w.db.get_ps_spending_collaterals() == {}

    def test_spending_denoms(self):
        uuid1 = 'uuid1'
        uuid2 = 'uuid2'
        outpoint1 = 'outpoint1'
        outpoint2 = 'outpoint2'
        w = self.wallet
        psman = w.psman
        assert w.db.get_ps_spending_denoms() == {}
        psman.add_ps_spending_denom(outpoint1, uuid1)
        assert len(w.db.get_ps_spending_denoms()) == 1
        assert w.db.get_ps_spending_denom(outpoint1) == uuid1
        psman.add_ps_spending_denom(outpoint2, uuid2)
        assert len(w.db.get_ps_spending_denoms()) == 2
        assert w.db.get_ps_spending_denom(outpoint2) == uuid2
        assert w.db.get_ps_spending_denom(outpoint1) == uuid1

        assert psman.pop_ps_spending_denom(outpoint1) == uuid1
        assert len(w.db.get_ps_spending_denoms()) == 1
        assert w.db.get_ps_spending_denom(outpoint2) == uuid2
        assert psman.pop_ps_spending_denom(outpoint2) == uuid2
        assert w.db.get_ps_spending_denoms() == {}

    def test_prepare_pay_collateral_wfl(self):
        w = self.wallet
        psman = w.psman

        # check not created if no ps_collateral exists
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.pay_collateral_wfl

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        # check not created if pay_collateral_wfl is not empty
        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_pay_collateral_wfl(wfl)
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.pay_collateral_wfl == wfl
        psman.clear_pay_collateral_wfl()

        # check not created if utxo not found
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        w.db.pop_ps_collateral(c_outpoint)
        outpoint0 = '0'*64 + ':0'
        collateral0 = (w.dummy_address(), 40000)
        w.db.add_ps_collateral(outpoint0, collateral0)
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.pay_collateral_wfl
        w.db.pop_ps_collateral(outpoint0)
        w.db.add_ps_collateral(c_outpoint, ps_collateral)

        # check created pay collateral tx
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.pay_collateral_wfl
        assert wfl.completed
        assert len(wfl.tx_order) == 1
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data[txid]
        tx = Transaction(tx_data.raw_tx)
        assert txid == tx.txid()
        txins = tx.inputs()
        txouts = tx.outputs()
        assert len(txins) == 1
        assert len(txouts) == 1
        in0 = txins[0]
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        assert w.db.get_ps_spending_collateral(c_outpoint) == wfl.uuid
        assert in0.prevout.to_str() == c_outpoint
        assert txouts[0].value == COLLATERAL_VAL
        reserved = w.db.select_ps_reserved(for_change=True, data=c_outpoint)
        assert len(reserved) == 1
        assert txouts[0].address in reserved
        assert tx.locktime == 0
        assert txins[0].nsequence == 0xffffffff

    def test_cleanup_pay_collateral_wfl(self):
        w = self.wallet
        psman = w.psman

        # check if pay_collateral_wfl is empty
        assert not psman.pay_collateral_wfl
        coro = psman.cleanup_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.pay_collateral_wfl

        # check no cleanup if completed and tx_order is not empty
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.cleanup_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.pay_collateral_wfl

        # check cleanup if not completed and tx_order is not empty
        wfl = psman.pay_collateral_wfl
        for outpoint, ps_collateral in w.db.get_ps_collaterals().items():
            pass
        reserved = w.db.select_ps_reserved(for_change=True, data=outpoint)
        assert len(reserved) == 1

        wfl.completed = False
        psman.set_pay_collateral_wfl(wfl)
        coro = psman.cleanup_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert w.db.get_ps_spending_collaterals() == {}

        assert not psman.pay_collateral_wfl
        reserved = w.db.select_ps_reserved(for_change=True, data=outpoint)
        assert len(reserved) == 1
        reserved = w.db.select_ps_reserved(for_change=True)
        assert len(reserved) == 0
        assert not psman.pay_collateral_wfl

        # check cleaned up with force
        assert not psman.pay_collateral_wfl
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.pay_collateral_wfl
        coro = psman.cleanup_pay_collateral_wfl(force=True)
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.pay_collateral_wfl
        assert w.db.get_ps_spending_collaterals() == {}

    def test_process_by_pay_collateral_wfl(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        old_c_outpoint, old_collateral = w.db.get_ps_collateral()
        coro = psman.prepare_pay_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)

        wfl = psman.pay_collateral_wfl
        txid = wfl.tx_order[0]
        tx = Transaction(wfl.tx_data[txid].raw_tx)
        w.add_unverified_tx(txid, TX_HEIGHT_UNCONFIRMED)
        assert not w.is_local_tx(txid)
        psman._add_ps_data(txid, tx, PSTxTypes.PAY_COLLATERAL)
        psman._process_by_pay_collateral_wfl(txid, tx)
        assert not psman.pay_collateral_wfl
        reserved = w.db.select_ps_reserved(for_change=True, data=wfl.uuid)
        assert reserved == []
        reserved = w.db.select_ps_reserved(for_change=True)
        assert reserved == []
        new_c_outpoint, new_collateral = w.db.get_ps_collateral()
        out0 = tx.outputs()[0]
        assert new_collateral[0] == out0.address
        assert new_collateral[1] == out0.value
        assert new_c_outpoint == f'{txid}:0'
        spent_c = w.db.get_ps_spent_collaterals()
        assert spent_c[old_c_outpoint] == old_collateral
        assert w.db.get_ps_spending_collaterals() == {}

    def test_create_new_collateral_wfl(self):
        w = self.wallet
        psman = w.psman

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # check not created if new_collateral_wfl is not empty
        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_new_collateral_wfl(wfl)
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl == wfl
        psman.clear_new_collateral_wfl()

        # check prepared tx
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.completed
        assert len(wfl.tx_order) == 1
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data[txid]
        tx = w.db.get_transaction(txid)
        assert tx.serialize_to_network() == tx_data.raw_tx
        txins = tx.inputs()
        txouts = tx.outputs()
        assert len(txins) == 1
        assert len(txouts) == 2
        assert txouts[0].value == CREATE_COLLATERAL_VAL
        assert txouts[0].address in w.db.select_ps_reserved(data=wfl.uuid)

    def test_create_new_collateral_wfl_group_origin_by_addr(self):
        w = self.wallet
        psman = w.psman
        psman.group_origin_coins_by_addr = True

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # check not created if new_collateral_wfl is not empty
        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_new_collateral_wfl(wfl)
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl == wfl
        psman.clear_new_collateral_wfl()

        # check prepared tx
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.completed
        assert len(wfl.tx_order) == 1
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data[txid]
        tx = w.db.get_transaction(txid)
        assert tx.serialize_to_network() == tx_data.raw_tx
        txins = tx.inputs()
        txouts = tx.outputs()
        assert len(txins) == 2
        assert len(txouts) == 2
        assert txouts[0].value == CREATE_COLLATERAL_VAL
        assert txouts[0].address in w.db.select_ps_reserved(data=wfl.uuid)

    def test_create_new_collateral_wfl_from_gui(self):
        w = self.wallet
        psman = w.psman

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        coins = w.get_spendable_coins(domain=None)
        coins = sorted([c for c in coins], key=lambda x: x.value_sats())
        # check selected to many utxos
        assert not psman.new_collateral_from_coins_info(coins)
        wfl, err = psman.create_new_collateral_wfl_from_gui(coins, None)
        assert err
        assert not wfl

        # check selected to large utxo
        assert not psman.new_collateral_from_coins_info(coins[-1:])
        wfl, err = psman.create_new_collateral_wfl_from_gui(coins, None)
        assert err
        assert not wfl

        # check on single minimal denom
        coins = w.get_utxos(None, mature_only=True,
                            confirmed_funding_only=True,
                            consider_islocks=True, min_rounds=0)
        coins = [c for c in coins if c.value_sats() == MIN_DENOM_VAL]
        coins = sorted(coins, key=lambda x: x.ps_rounds)
        coins = coins[0:1]
        assert psman.new_collateral_from_coins_info(coins) == \
            ('Transactions type: PS New Collateral\n'
             'Count of transactions: 1\n'
             'Total sent amount: 100001\n'
             'Total output amount: 90000\n'
             'Total fee: 10001')

        # check not created if mixing
        psman.state = PSStates.Mixing
        wfl, err = psman.create_new_collateral_wfl_from_gui(coins, None)
        assert err
        assert not wfl
        psman.state = PSStates.Ready

        # check created on minimal denom
        wfl, err = psman.create_new_collateral_wfl_from_gui(coins, None)
        assert not err
        txid = wfl.tx_order[0]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        inputs = tx.inputs()
        outputs = tx.outputs()
        assert len(inputs) == 1
        assert len(outputs) == 1  # no change
        txin = inputs[0]
        prev_h = txin.prevout.txid.hex()
        prev_n = txin.prevout.out_idx
        prev_tx = w.db.get_transaction(prev_h)
        prev_txout = prev_tx.outputs()[prev_n]
        assert prev_txout.value == MIN_DENOM_VAL
        assert outputs[0].value == CREATE_COLLATERAL_VALS[-2]  # 90000

        # check denom is spent
        denom_oupoint = f'{prev_h}:{prev_n}'
        assert not w.db.get_ps_denom(denom_oupoint)
        assert w.db.get_ps_spent_denom(denom_oupoint)[1] == MIN_DENOM_VAL

        assert psman.new_collateral_wfl
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_collateral_wfl(txid, tx)
        assert not psman.new_collateral_wfl

    def test_cleanup_new_collateral_wfl(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        w.db.pop_ps_collateral(c_outpoint)

        # check if new_collateral_wfl is empty
        assert not psman.new_collateral_wfl
        coro = psman.cleanup_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_collateral_wfl

        # check no cleanup if completed and tx_order is not empty
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl
        coro = psman.cleanup_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl

        # check cleanup if not completed and tx_order is not empty
        wfl = psman.new_collateral_wfl
        txid = wfl.tx_order[0]
        assert w.db.get_transaction(txid) is not None
        reserved = w.db.select_ps_reserved(data=wfl.uuid)
        assert len(reserved) == 1

        wfl.completed = False
        psman.set_new_collateral_wfl(wfl)
        coro = psman.cleanup_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)

        assert not psman.new_collateral_wfl
        reserved = w.db.select_ps_reserved(data=wfl.uuid)
        assert len(reserved) == 0
        reserved = w.db.select_ps_reserved()
        assert len(reserved) == 0
        assert w.db.get_transaction(txid) is None
        assert not psman.new_collateral_wfl

        # check cleaned up with force
        assert not psman.new_collateral_wfl
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl
        coro = psman.cleanup_new_collateral_wfl(force=True)
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_collateral_wfl

        # check cleaned up when all txs removed
        assert not psman.new_collateral_wfl
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl
        txid = psman.new_collateral_wfl.tx_order[0]
        w.remove_transaction(txid)
        assert not psman.new_collateral_wfl

    def test_broadcast_new_collateral_wfl(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        w.db.pop_ps_collateral(c_outpoint)
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.completed

        # check not broadcasted (no network)
        assert wfl.next_to_send(w) is not None
        coro = psman.broadcast_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.next_to_send(w) is not None

        # check not broadcasted (mock network method raises)
        assert wfl.next_to_send(w) is not None
        psman.network = NetworkBroadcastMock(pass_cnt=0)
        coro = psman.broadcast_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.next_to_send(w) is not None

        # check not broadcasted (skipped) if tx is in wallet.unverified_tx
        assert wfl.next_to_send(w) is not None
        txid = wfl.tx_order[0]
        w.add_unverified_tx(txid, TX_HEIGHT_UNCONF_PARENT)
        assert wfl.next_to_send(w) is None
        psman.network = NetworkBroadcastMock()
        coro = psman.broadcast_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.next_to_send(w) is None
        w.unverified_tx.pop(txid)

        # check not broadcasted (mock network) but recently send failed
        assert wfl.next_to_send(w) is not None
        psman.network = NetworkBroadcastMock()
        coro = psman.broadcast_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.next_to_send(w) is not None

        # check broadcasted (mock network)
        assert wfl.next_to_send(w) is not None
        tx_data = wfl.next_to_send(w)
        tx_data.next_send = None
        psman.set_new_collateral_wfl(wfl)
        coro = psman.broadcast_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_collateral_wfl

    def test_process_by_new_collateral_wfl(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing
        c_outpoint, ps_collateral = w.db.get_ps_collateral()
        w.db.pop_ps_collateral(c_outpoint)
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)

        wfl = psman.new_collateral_wfl
        txid = wfl.tx_order[0]
        tx = Transaction(wfl.tx_data[txid].raw_tx)
        w.add_unverified_tx(txid, TX_HEIGHT_UNCONFIRMED)
        psman._process_by_new_collateral_wfl(txid, tx)
        assert not psman.new_collateral_wfl
        reserved = w.db.select_ps_reserved(data=wfl.uuid)
        assert reserved == []
        reserved = w.db.select_ps_reserved()
        assert reserved == []
        new_c_outpoint, new_collateral = w.db.get_ps_collateral()
        tx = w.db.get_transaction(txid)
        out0 = tx.outputs()[0]
        assert new_collateral[0] == out0.address
        assert new_collateral[1] == out0.value
        assert new_c_outpoint == f'{txid}:0'
        assert w.db.get_ps_tx(txid) == (PSTxTypes.NEW_COLLATERAL, True)

    def test_find_denoms_approx_def(self):
        psman = self.wallet.psman
        need_val = psman.keep_amount*COIN + CREATE_COLLATERAL_VAL
        res = psman._find_denoms_approx_def(need_val)
        assert sum(v for amnts in res for v in amnts) - need_val == 62001

    def test_find_denoms_approx_abs(self):
        cur_cnt = {}
        d1, d2, d3, d4, d5 = PS_DENOMS_VALS

        def mock_calc_denoms_by_values():
            return cur_cnt

        psman = self.wallet.psman
        psman.calc_denoms_by_values = mock_calc_denoms_by_values
        psman.calc_denoms_method = psman.CalcDenomsMethod.ABS

        cur_cnt.update({d1: 400, d2: 0, d3: 500, d4: 0, d5: 0})
        psman.abs_denoms_cnt = abs_cnt = {d1: 10, d2: 0, d3: 0, d4: 5, d5: 10}
        need_val = psman.keep_amount*COIN

        res = psman._find_denoms_approx_abs(need_val)
        assert sum(v for amnts in res for v in amnts) == need_val - d1*10

        cur_cnt.update({d1: 400, d2: 0, d3: 500, d4: 1, d5: 0})
        res = psman._find_denoms_approx_abs(need_val)
        assert sum(v for amnts in res for v in amnts) == need_val - d1*10 - d4

        cur_cnt.update({d1: 0, d2: 0, d3: 0, d4: 0, d5: 0})
        res = psman._find_denoms_approx_abs(need_val)
        assert sum(v for amnts in res for v in amnts) == need_val

    def test_calc_need_denoms_amounts(self):
        all_test_amounts = [
            [40000] + [100001]*11 + [1000010]*11 + [10000100]*11,
            [100001]*11 + [1000010]*11 + [10000100]*6,
            [100001]*11 + [1000010]*4,
            [100001]*8,
        ]
        w = self.wallet
        psman = w.psman
        res = psman.calc_need_denoms_amounts()
        assert res == all_test_amounts
        res = psman.calc_need_denoms_amounts(use_cache=True)
        assert res == all_test_amounts
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        res = psman.calc_need_denoms_amounts()
        assert res == []
        res = psman.calc_need_denoms_amounts(use_cache=True)
        assert res == []

    def test_calc_need_denoms_amounts_from_coins(self):
        w = self.wallet
        psman = w.psman

        dv0001 = PS_DENOMS_VALS[0]
        dv001 = PS_DENOMS_VALS[1]
        dv01 = PS_DENOMS_VALS[2]
        dv1 = PS_DENOMS_VALS[3]
        coins = w.get_spendable_coins(domain=None)
        c0001 = list(filter(lambda x: x.value_sats() == dv0001, coins))
        c001 = list(filter(lambda x: x.value_sats() == dv001, coins))
        c01 = list(filter(lambda x: x.value_sats() == dv01, coins))
        c1 = list(filter(lambda x: x.value_sats() == dv1, coins))
        other = list(filter(lambda x: x.value_sats() not in PS_DENOMS_VALS,
                            coins))
        ccv = COLLATERAL_VAL*9
        assert len(c1) == 2
        assert len(c01) == 26
        assert len(c001) == 33
        assert len(c0001) == 70

        assert psman.calc_need_denoms_amounts(coins=c0001[0:1]) == []

        expected = [[ccv] + [dv0001]]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:2]) == expected

        expected = [[ccv] + [dv0001]*2]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:3]) == expected

        expected = [[ccv] + [dv0001]*3]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:4]) == expected

        expected = [[ccv] + [dv0001]*4]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:5]) == expected

        expected = [[ccv] + [dv0001]*5]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:6]) == expected

        expected = [[ccv] + [dv0001]*6]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:7]) == expected

        expected = [[ccv] + [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c0001[0:8]) == expected


        expected = [[ccv] + [dv0001]*9]
        assert psman.calc_need_denoms_amounts(coins=c001[0:1]) == expected

        expected = [[ccv] + [dv0001]*11, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:2]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001], [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:3]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*2, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:4]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*3, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:5]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*4, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:6]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*5, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:7]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*6, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c001[0:8]) == expected


        expected = [[ccv] + [dv0001]*11 + [dv001]*8, [dv0001]*8]
        assert psman.calc_need_denoms_amounts(coins=c01[0:1]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:2]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01],
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:3]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*2,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:4]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*3,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:5]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*4,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:6]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*5,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:7]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*6,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:8]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*7,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c01[0:9]) == expected


        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*8,
                    [dv0001]*11 + [dv001]*6, [dv0001]*7]
        assert psman.calc_need_denoms_amounts(coins=c1[0:1]) == expected

        expected = [[ccv] + [dv0001]*11 + [dv001]*11 + [dv01]*11,
                    [dv0001]*11 + [dv001]*11 + [dv01]*6,
                    [dv0001]*11 + [dv001]*4, [dv0001]*6]
        assert psman.calc_need_denoms_amounts(coins=c1[0:2]) == expected

        expected = [[10000] + [dv0001]*11 + [dv001]*11 + [dv01]*11 + [dv1]*8,
                    [dv0001]*11 + [dv001]*11 + [dv01]*5, [dv0001]*6]
        assert psman.calc_need_denoms_amounts(coins=other) == expected

    def test_calc_need_denoms_amounts_on_keep_amount(self):
        w = self.wallet
        psman = w.psman
        two_dash_amnts_val = 200142001

        res = psman.calc_need_denoms_amounts()
        assert sum([sum(amnts)for amnts in res]) == two_dash_amnts_val
        res = psman.calc_need_denoms_amounts(on_keep_amount=True)
        assert sum([sum(amnts)for amnts in res]) == two_dash_amnts_val

        # test with spendable amount < keep_amount
        coins0 = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                             mature_only=True, include_ps=True)
        coins = [c for c in coins0 if c.value_sats() < 50000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = [c for c in coins0 if c.value_sats() >= 100000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        assert sum([c.value_sats() for c in coins]) == 50000000  # 0.5 Dash

        res = psman.calc_need_denoms_amounts()
        assert sum([sum(amnts)for amnts in res]) == 49740497
        res = psman.calc_need_denoms_amounts(on_keep_amount=True)
        assert sum([sum(amnts)for amnts in res]) == two_dash_amnts_val

        # test with zero spendable amount
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]

        res = psman.calc_need_denoms_amounts()
        assert sum([sum(amnts)for amnts in res]) == 0
        res = psman.calc_need_denoms_amounts(on_keep_amount=True)
        assert sum([sum(amnts)for amnts in res]) == two_dash_amnts_val

    def test_calc_need_denoms_amounts_on_abs_cnt(self):
        w = self.wallet
        psman = w.psman
        psman.calc_denoms_method = psman.CalcDenomsMethod.ABS
        assert psman.keep_amount == 0
        assert psman.calc_need_denoms_amounts() == []
        abs_cnt = psman.abs_denoms_cnt
        abs_cnt[PS_DENOMS_VALS[2]] = 3
        abs_cnt[PS_DENOMS_VALS[3]] = 2
        abs_cnt[PS_DENOMS_VALS[4]] = 1
        psman.abs_denoms_cnt = abs_cnt

        coins_data = psman._get_next_coins_for_mixing()
        assert coins_data['total_val'] > psman.keep_amount*COIN + MIN_DENOM_VAL
        assert psman.keep_amount == 12.300123
        res = psman.calc_need_denoms_amounts()
        total_val = sum(v for amnts in res for v in amnts)
        cnt = Counter(res[0])
        assert cnt[PS_DENOMS_VALS[2]] == 3
        assert cnt[PS_DENOMS_VALS[3]] == 2
        assert cnt[PS_DENOMS_VALS[4]] == 1
        assert total_val - 40000 == psman.keep_amount*COIN

        # check with on_keep_amount=True
        abs_cnt[PS_DENOMS_VALS[4]] = 10
        psman.abs_denoms_cnt = abs_cnt
        assert psman.keep_amount == 102.301023

        res = psman.calc_need_denoms_amounts(on_keep_amount=True)
        total_val = sum(v for amnts in res for v in amnts)
        assert total_val - 40000 == psman.keep_amount*COIN
        assert coins_data['total_val'] < psman.keep_amount*COIN

        # find untracked ps data
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        abs_cnt[PS_DENOMS_VALS[4]] = 1
        psman.abs_denoms_cnt = abs_cnt
        assert psman.keep_amount == 12.300123

        res = psman.calc_need_denoms_amounts()
        total_val = sum(v for amnts in res for v in amnts)
        assert total_val == 0
        found_cnt = psman.calc_denoms_by_values()
        assert found_cnt[PS_DENOMS_VALS[2]] >= 3
        assert found_cnt[PS_DENOMS_VALS[3]] >= 2
        coins_data = psman._get_next_coins_for_mixing()
        assert coins_data['total_val'] < PS_DENOMS_VALS[4]
        assert found_cnt[PS_DENOMS_VALS[4]] == 0  # not enough funds

        # check with on_keep_amount=True
        res = psman.calc_need_denoms_amounts(on_keep_amount=True)
        total_val = sum(v for amnts in res for v in amnts)
        assert total_val == 1000010000

    def test_calc_tx_size(self):
        # average sizes
        assert 192 == calc_tx_size(1, 1)
        assert 226 == calc_tx_size(1, 2)
        assert 37786 == calc_tx_size(255, 1)
        assert 8830 == calc_tx_size(1, 255)
        assert 46424 == calc_tx_size(255, 255)
        assert 148046 == calc_tx_size(1000, 1)
        assert 34160 == calc_tx_size(1, 1000)
        assert 182014 == calc_tx_size(1000, 1000)

        # max sizes
        assert 193 == calc_tx_size(1, 1, max_size=True)
        assert 227 == calc_tx_size(1, 2, max_size=True)
        assert 38041 == calc_tx_size(255, 1, max_size=True)
        assert 8831 == calc_tx_size(1, 255, max_size=True)
        assert 46679 == calc_tx_size(255, 255, max_size=True)
        assert 149046 == calc_tx_size(1000, 1, max_size=True)
        assert 34161 == calc_tx_size(1, 1000, max_size=True)
        assert 183014 == calc_tx_size(1000, 1000, max_size=True)

    def test_calc_tx_fee(self):
        # average sizes
        assert 192 == calc_tx_fee(1, 1, 1000)
        assert 226 == calc_tx_fee(1, 2, 1000)
        assert 37786 == calc_tx_fee(255, 1, 1000)
        assert 8830 == calc_tx_fee(1, 255, 1000)
        assert 46424 == calc_tx_fee(255, 255, 1000)
        assert 148046 == calc_tx_fee(1000, 1, 1000)
        assert 34160 == calc_tx_fee(1, 1000, 1000)
        assert 182014 == calc_tx_fee(1000, 1000, 1000)

        # max sizes
        assert 193 == calc_tx_fee(1, 1, 1000, max_size=True)
        assert 227 == calc_tx_fee(1, 2, 1000, max_size=True)
        assert 38041 == calc_tx_fee(255, 1, 1000, max_size=True)
        assert 8831 == calc_tx_fee(1, 255, 1000, max_size=True)
        assert 46679 == calc_tx_fee(255, 255, 1000, max_size=True)
        assert 149046 == calc_tx_fee(1000, 1, 1000, max_size=True)
        assert 34161 == calc_tx_fee(1, 1000, 1000, max_size=True)
        assert 183014 == calc_tx_fee(1000, 1000, 1000, max_size=True)

    def test_get_next_coins_for_mixing(self):
        w = self.wallet
        psman = w.psman
        psman.MIN_NEW_DENOMS_DELAY = 3
        psman.MAX_NEW_DENOMS_DELAY = 3

        now = time.time()
        psman.last_denoms_tx_time = now

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        assert time.time() - now < 1
        total_val = coins['total_val']
        assert total_val == 1484831773
        coins = coins['coins']
        assert len(coins) == 138

        # freeze found coins to test next coins found
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        total_val = coins['total_val']
        assert total_val == 0
        coins = coins['coins']
        assert len(coins) == 0

    def test_get_next_coins_for_mixing_group_origin_by_addr(self):
        w = self.wallet
        psman = w.psman
        psman.MIN_NEW_DENOMS_DELAY = 3
        psman.MAX_NEW_DENOMS_DELAY = 3

        psman.group_origin_coins_by_addr = True

        now = time.time()
        psman.last_denoms_tx_time = now

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        assert time.time() - now > 3.0
        assert time.time() - now < 4.0
        total_val = coins['total_val']
        assert total_val == 802806773
        coins = coins['coins']
        assert len(coins) == 2
        assert coins[0].address == coins[1].address
        assert coins[0].value_sats() + coins[1].value_sats() == total_val

        # freeze found coins to test next coins found
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        total_val = coins['total_val']
        assert total_val == 100001000
        coins = coins['coins']
        assert len(coins) == 1
        assert coins[0].value_sats() == total_val


        # check coins filtered by calc_need_denoms_amounts
        coins = w.get_utxos(None)
        coins = [c for c in coins
                 if c.value_sats() in [100001000, 100000000, 50000000,
                                       30000000, 10000100, 2000000]]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        total_val = coins['total_val']
        assert total_val == 1000010
        coins = coins['coins']
        assert len(coins) == 1

        w.db.set_ps_data('mix_rounds', 500)  # high rounds to check skip coins
        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        total_val = coins['total_val']
        assert total_val == 0
        coins = coins['coins']
        assert len(coins) == 0

        # freeze all to test coins absence
        coins = w.get_utxos(None)
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        coro = psman.get_next_coins_for_mixing()
        coins = asyncio.get_event_loop().run_until_complete(coro)
        total_val = coins['total_val']
        assert total_val == 0
        coins = coins['coins']
        assert len(coins) == 0

    def test_create_new_denoms_wfl(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing

        # check not created if new_denoms_wfl is not empty
        wfl = PSTxWorkflow(uuid='uuid')
        psman.set_new_denoms_wfl(wfl)
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl == wfl
        psman.clear_new_denoms_wfl()

        # check created successfully
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed
        all_test_amounts = [
            [100001]*11 + [1000010]*11 + [10000100]*11,
            [100001]*11 + [1000010]*11 + [10000100]*6,
            [100001]*11 + [1000010]*4,
            [100001]*8,
        ]
        for i, txid in enumerate(wfl.tx_order):
            tx = w.db.get_transaction(txid)
            collaterals_count = 0
            denoms_count = 0
            change_count = 0
            for o in tx.outputs():
                val = o.value
                if val == CREATE_COLLATERAL_VAL:
                    collaterals_count += 1
                elif val in PS_DENOMS_VALS:
                    assert all_test_amounts[i][denoms_count] == val
                    denoms_count += 1
                else:
                    change_count += 1
            if i == 0:
                assert collaterals_count == 1
            else:
                assert collaterals_count == 0
            assert denoms_count == len(all_test_amounts[i])
            assert change_count == 1
        assert len(w.db.select_ps_reserved(data=wfl.uuid)) == 85

        wfl.completed = False
        psman.set_new_denoms_wfl(wfl)
        coro = psman.cleanup_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        outpoint0 = '0'*64 + ':0'
        w.db.add_ps_collateral(outpoint0, (w.dummy_address(), 1))
        assert not psman.new_denoms_wfl

        # check created successfully without ps_collateral output
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed
        all_test_amounts = [
            [100001]*11 + [1000010]*11 + [10000100]*11,
            [100001]*11 + [1000010]*11 + [10000100]*6,
            [100001]*11 + [1000010]*4,
            [100001]*8,
        ]
        for i, txid in enumerate(wfl.tx_order):
            tx = w.db.get_transaction(txid)
            collaterals_count = 0
            denoms_count = 0
            change_count = 0
            for o in tx.outputs():
                val = o.value
                if val == CREATE_COLLATERAL_VAL:
                    collaterals_count += 1
                elif val in PS_DENOMS_VALS:
                    assert all_test_amounts[i][denoms_count] == val
                    denoms_count += 1
                else:
                    change_count += 1
            assert collaterals_count == 0
            assert denoms_count == len(all_test_amounts[i])
            assert change_count == 1
        assert len(w.db.select_ps_reserved(data=wfl.uuid)) == 84

        # check not created if enoug denoms exists
        psman.keep_amount = 5
        wfl.completed = False
        psman.set_new_denoms_wfl(wfl)
        coro = psman.cleanup_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        w.db.pop_ps_collateral(outpoint0)
        psman.state = PSStates.Ready
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_denoms_wfl

    def test_create_new_denoms_wfl_low_balance(self):
        w = self.wallet
        psman = w.psman
        psman.keep_amount = 1000
        fee_per_kb = self.config.fee_per_kb()

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed

        # assert coins left less of half_minimal_denom
        c, u, x = w.get_balance(include_ps=False)
        new_collateral_cnt = 19
        new_collateral_fee = calc_tx_fee(1, 2, fee_per_kb, max_size=True)
        half_minimal_denom = MIN_DENOM_VAL // 2
        assert (c + u -
                CREATE_COLLATERAL_VAL * new_collateral_cnt -
                new_collateral_fee * new_collateral_cnt) < half_minimal_denom

    def test_create_new_denoms_wfl_low_balance_group_origin_by_addr(self):
        w = self.wallet
        psman = w.psman
        psman.group_origin_coins_by_addr = True
        psman.keep_amount = 1000
        fee_per_kb = self.config.fee_per_kb()

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # freeze coins except smallest
        coins = sorted(w.get_utxos(), key=lambda x: -x.value_sats())[:-1]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        assert len(coins) == 1
        assert coins[0].value_sats() == 1000000

        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed

        # assert coins left less of half_minimal_denom
        new_collateral_cnt = 19
        new_collateral_fee = calc_tx_fee(1, 2, fee_per_kb, max_size=True)
        half_minimal_denom = MIN_DENOM_VAL // 2
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        assert len(coins) == 1
        assert (coins[0].value_sats() -
                CREATE_COLLATERAL_VAL * new_collateral_cnt -
                new_collateral_fee * new_collateral_cnt) < half_minimal_denom

    def test_create_new_denoms_wfl_from_gui(self):
        w = self.wallet
        psman = w.psman

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        coins = w.get_spendable_coins(domain=None)
        coins = sorted([c for c in coins], key=lambda x: x.value_sats())
        # check selected to many utxos
        assert not psman.new_denoms_from_coins_info(coins)
        wfl, err = psman.create_new_denoms_wfl_from_gui(coins, None)
        assert err
        assert not wfl

        coins = w.get_utxos(None, mature_only=True,
                            confirmed_funding_only=True,
                            consider_islocks=True, min_rounds=0)
        coins = [c for c in coins if c.value_sats() == PS_DENOMS_VALS[-2]]
        coins = sorted(coins, key=lambda x: x.ps_rounds)

        # check on single max value available denom
        coins = coins[0:1]

        # check not created if mixing
        psman.state = PSStates.Mixing
        wfl, err = psman.create_new_denoms_wfl_from_gui(coins, None)
        assert err
        assert not wfl
        psman.state = PSStates.Ready

        # check on 100001000 denom
        assert psman.new_denoms_from_coins_info(coins) == \
            ('Transactions type: PS New Denoms\n'
             'Count of transactions: 3\n'
             'Total sent amount: 100001000\n'
             'Total output amount: 99990999\n'
             'Total fee: 10001')

        wfl, err = psman.create_new_denoms_wfl_from_gui(coins, None)
        assert not err
        txid = wfl.tx_order[0]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        inputs = tx.inputs()
        outputs = tx.outputs()
        assert len(inputs) == 1
        assert len(outputs) == 32
        txin = inputs[0]
        prev_h = txin.prevout.txid.hex()
        prev_n = txin.prevout.out_idx
        prev_tx = w.db.get_transaction(prev_h)
        prev_txout = prev_tx.outputs()[prev_n]
        assert prev_txout.value == PS_DENOMS_VALS[-2]
        assert outputs[0].value == CREATE_COLLATERAL_VALS[-2]  # 90000
        total_out_vals = 0
        out_vals = [o.value for o in outputs]
        total_out_vals += sum(out_vals) - 7808833
        assert out_vals == [90000, 100001, 100001, 100001, 100001, 100001,
                            100001, 100001, 100001, 100001, 100001, 100001,
                            1000010, 1000010, 1000010, 1000010, 1000010,
                            1000010, 1000010, 1000010, 1000010, 1000010,
                            1000010, 7808833, 10000100, 10000100, 10000100,
                            10000100, 10000100, 10000100, 10000100, 10000100]

        txid = wfl.tx_order[1]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        inputs = tx.inputs()
        outputs = tx.outputs()
        out_vals = [o.value for o in outputs]
        total_out_vals += sum(out_vals) - 707992
        assert out_vals == [100001, 100001, 100001, 100001, 100001, 100001,
                            100001, 100001, 100001, 100001, 100001, 707992,
                            1000010, 1000010, 1000010, 1000010, 1000010,
                            1000010]

        txid = wfl.tx_order[2]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        inputs = tx.inputs()
        outputs = tx.outputs()
        out_vals = [o.value for o in outputs]
        total_out_vals += sum(out_vals)
        assert out_vals == [100001, 100001, 100001, 100001, 100001, 100001,
                            100001]
        assert total_out_vals == 99990999

        # check denom is spent
        denom_oupoint = f'{prev_h}:{prev_n}'
        assert not w.db.get_ps_denom(denom_oupoint)
        assert w.db.get_ps_spent_denom(denom_oupoint)[1] == PS_DENOMS_VALS[-2]

        # process
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_denoms_wfl(txid, tx)
        assert not psman.new_denoms_wfl

        # check on 10000100 denom
        total_out_vals = 0
        coins = w.get_utxos(None, mature_only=True,
                            confirmed_funding_only=True,
                            consider_islocks=True, min_rounds=0)
        coins = [c for c in coins if c.value_sats() == PS_DENOMS_VALS[-3]]
        coins = sorted(coins, key=lambda x: x.ps_rounds)
        coins = coins[0:1]
        assert psman.new_denoms_from_coins_info(coins) == \
            ('Transactions type: PS New Denoms\n'
             'Count of transactions: 2\n'
             'Total sent amount: 10000100\n'
             'Total output amount: 9990099\n'
             'Total fee: 10001')
        wfl, err = psman.create_new_denoms_wfl_from_gui(coins, None)
        txid = wfl.tx_order[0]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        out_vals = [o.value for o in tx.outputs()]
        total_out_vals += sum(out_vals) - 809137
        assert out_vals == [90000, 100001, 100001, 100001, 100001, 100001,
                            100001, 100001, 100001, 100001, 100001, 100001,
                            809137, 1000010, 1000010, 1000010, 1000010,
                            1000010, 1000010, 1000010, 1000010]
        txid = wfl.tx_order[1]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        out_vals = [o.value for o in tx.outputs()]
        total_out_vals += sum(out_vals)
        assert out_vals == [100001, 100001, 100001, 100001, 100001, 100001,
                            100001, 100001]
        assert total_out_vals == 9990099
        # process
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_denoms_wfl(txid, tx)
        assert not psman.new_denoms_wfl

        # check on 1000010 denom
        total_out_vals = 0
        coins = w.get_utxos(None, mature_only=True,
                            confirmed_funding_only=True,
                            consider_islocks=True, min_rounds=0)
        coins = [c for c in coins if c.value_sats() == PS_DENOMS_VALS[-4]]
        coins = sorted(coins, key=lambda x: x.ps_rounds)
        coins = coins[0:1]
        assert psman.new_denoms_from_coins_info(coins) == \
            ('Transactions type: PS New Denoms\n'
             'Count of transactions: 1\n'
             'Total sent amount: 1000010\n'
             'Total output amount: 990009\n'
             'Total fee: 10001')
        wfl, err = psman.create_new_denoms_wfl_from_gui(coins, None)
        txid = wfl.tx_order[0]
        raw_tx = wfl.tx_data[txid].raw_tx
        tx = Transaction(raw_tx)
        out_vals = [o.value for o in tx.outputs()]
        total_out_vals += sum(out_vals)
        assert out_vals == [90000, 100001, 100001, 100001, 100001, 100001,
                            100001, 100001, 100001, 100001]
        assert total_out_vals == 990009
        # process
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_denoms_wfl(txid, tx)
        assert not psman.new_denoms_wfl

    def test_cleanup_new_denoms_wfl(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing

        # check if new_denoms_wfl is empty
        assert not psman.new_denoms_wfl
        coro = psman.cleanup_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_denoms_wfl

        # check no cleanup if completed and tx_order is not empty
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl
        coro = psman.cleanup_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl

        # check cleanup if not completed and tx_order is not empty
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        for txid in wfl.tx_order:
            assert w.db.get_transaction(txid) is not None
        reserved = w.db.select_ps_reserved(data=wfl.uuid)
        assert len(reserved) == 85

        wfl.completed = False
        psman.set_new_denoms_wfl(wfl)
        coro = psman.cleanup_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_denoms_wfl

        for txid in wfl.tx_order:
            assert w.db.get_transaction(txid) is None
        reserved = w.db.select_ps_reserved(data=wfl.uuid)
        assert len(reserved) == 0
        reserved = w.db.select_ps_reserved()
        assert len(reserved) == 0

        # check cleaned up with force
        assert not psman.new_denoms_wfl
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl
        coro = psman.cleanup_new_denoms_wfl(force=True)
        asyncio.get_event_loop().run_until_complete(coro)
        assert not psman.new_denoms_wfl

        # check cleaned up when all txs removed
        assert not psman.new_denoms_wfl
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl
        assert len(psman.new_denoms_wfl.tx_order) == 4
        txid = psman.new_denoms_wfl.tx_order[0]
        w.remove_transaction(txid)
        assert len(psman.new_denoms_wfl.tx_order) == 3
        txid = psman.new_denoms_wfl.tx_order[0]
        w.remove_transaction(txid)
        assert len(psman.new_denoms_wfl.tx_order) == 2
        txid = psman.new_denoms_wfl.tx_order[0]
        w.remove_transaction(txid)
        assert len(psman.new_denoms_wfl.tx_order) == 1
        txid = psman.new_denoms_wfl.tx_order[0]
        w.remove_transaction(txid)
        assert not psman.new_denoms_wfl

    def test_broadcast_new_denoms_wfl(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed
        tx_order = wfl.tx_order
        tx_data = wfl.tx_data

        assert psman.last_denoms_tx_time == 0
        # check not broadcasted (no network)
        assert wfl.next_to_send(w) == tx_data[tx_order[0]]
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        tx_data = wfl.tx_data
        for txid in wfl.tx_order:
            assert tx_data[txid].sent is None
        assert wfl.next_to_send(w) == tx_data[tx_order[0]]

        assert psman.last_denoms_tx_time == 0
        # check not broadcasted (mock network method raises)
        psman.network = NetworkBroadcastMock(pass_cnt=0)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        tx_data = wfl.tx_data
        for txid in wfl.tx_order:
            assert tx_data[txid].sent is None

        assert psman.last_denoms_tx_time == 0
        # check not broadcasted (skipped) if tx is in wallet.unverified_tx
        assert wfl.next_to_send(w) == tx_data[tx_order[0]]
        for i, txid in enumerate(tx_order):
            w.add_unverified_tx(txid, TX_HEIGHT_UNCONF_PARENT)
            if i < len(tx_order) - 1:
                assert wfl.next_to_send(w) == tx_data[tx_order[i+1]]
        assert wfl.next_to_send(w) is None
        psman.network = NetworkBroadcastMock()
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        tx_data = wfl.tx_data
        for i, txid in enumerate(tx_order):
            assert tx_data[txid].sent is None
            w.unverified_tx.pop(txid)
        assert wfl.next_to_send(w) == tx_data[tx_order[0]]

        assert psman.last_denoms_tx_time == 0
        # check not broadcasted (mock network) but recently send failed
        psman.network = NetworkBroadcastMock()
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        tx_data = wfl.tx_data
        assert wfl.next_to_send(w) is not None
        for txid in wfl.tx_order:
            assert not tx_data[txid].sent

        assert psman.last_denoms_tx_time == 0
        # check broadcasted (mock network)
        for txid in wfl.tx_order:
            tx_data[txid].next_send = None
        psman.set_new_denoms_wfl(wfl)

        psman.network = NetworkBroadcastMock()
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman.broadcast_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        assert psman.new_denoms_wfl
        assert time.time() - psman.last_denoms_tx_time < 100

    def test_process_by_new_denoms_wfl(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing
        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        uuid = wfl.uuid

        denoms_cnt = [84, 84, 84, 84]
        reserved_cnt = [85, 85, 85, 0]
        reserved_none_cnt = [0, 0, 0, 0]
        for i, txid in enumerate(wfl.tx_order):
            w.add_unverified_tx(txid, TX_HEIGHT_UNCONFIRMED)
            assert not w.is_local_tx(txid)
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_denoms_wfl(txid, tx)
            wfl = psman.new_denoms_wfl
            if i == 0:
                c_outpoint, collateral = w.db.get_ps_collateral()
                assert c_outpoint and collateral
            if i < len(denoms_cnt) - 1:
                assert len(wfl.tx_order) == len(denoms_cnt) - i - 1
            assert len(w.db.ps_denoms) == denoms_cnt[i]
            assert len(w.db.select_ps_reserved(data=uuid)) == reserved_cnt[i]
            assert len(w.db.select_ps_reserved()) == reserved_none_cnt[i]
            assert w.db.get_ps_tx(txid) == (PSTxTypes.NEW_DENOMS, True)
        assert not wfl

    def test_sign_denominate_tx(self):
        raw_tx_final = (
            '020000000b35c83d33f4eb22cf07b87d6deb1f73ff9e72df33d5b9699255bbab'
            'b41b7823290e00000000ffffffffabaae659d6a4f2072d722dd7e7c478d5c366'
            '3b454f7f143dd20374e54a3478442000000000ffffffff3c3328c309414c2573'
            '43ab98f818f2cfe29bbb4b4e1a472d89e892c2eb22c3702000000000ffffffff'
            'c8c88b7e49a966a03eca39f5248cd66095f54278af3d1ba525955d150e0fbb7a'
            '0100000000ffffffff6679b6c0a77142c0a3c9812788d279ecee873c13cc85d5'
            '1d4b5081fbb464d87d0800000000ffffffffc2a56cd9393c4f3f207a721232a9'
            '6fa570c03062cd19fad1f2dfc9e5e84f4c7f0c00000000ffffffff4de733c7d2'
            'b2d5d82af0deb8edded3ac51916c2d47c726f362e7ddcb66354ca10000000000'
            'ffffffff47b4d30c252862f099027353c15ad264853c4569936f04bd8b24bdeb'
            '9ca8f1c10900000000fffffffff03bb7d5d3e6f46ccc36ea2b32cd00ad21aaa2'
            '644cf7e98b0e0ba8b1b80f27c91200000000ffffffff73fa0a029ecf2f95559b'
            '87bdf94207a1c6e35ea551c409862c0a298affe023de0800000000ffffffff92'
            '61f033620895cfd212b083d65b2fa607b799d20d834e9df8eeeb819dddb7f604'
            '00000000ffffffff0be4969800000000001976a9140dab039f105c1f0cf685f1'
            '7802241cf74c5a3c0a88ace4969800000000001976a91427d38aa89216e0f38d'
            '8b3b2dfca6c5a0813c319988ace4969800000000001976a9146d9aad56a22f5c'
            '22adc138838b967480032579ab88ace4969800000000001976a9148e8953727d'
            '6f70fa2c6fa073f660f6698dd7902788ace4969800000000001976a9149669e8'
            '2a91a1275204098d9960d3c6e73a37711588ace4969800000000001976a914ca'
            'bb5582885d713a5a5055cda3ba22d546eeb22288ace4969800000000001976a9'
            '14cf839533846857f38e6399a598588b49c3cb763d88ace49698000000000019'
            '76a914e3987f30f44ea35bc938cdfaf10203b8c1f6d84288ace4969800000000'
            '001976a914f2cadaf584f9ec642c9bdac7edc4690a3e8f833d88ace496980000'
            '0000001976a914f787ffced16771f5f88445450af7b8cf9262fffd88ace49698'
            '00000000001976a914f7ad936793f2206a0fb67fa27f4c4dc5723175c188ac00'
            '000000')
        tx = PartialTransaction.from_tx(Transaction(raw_tx_final))
        tx.deserialize()
        tx = self.wallet.psman._sign_denominate_tx(tx)
        assert bh2u(tx.inputs()[-1].script_sig) == (
            '47304402203dff9b1e2c4d0d7b2e5835a84aa2136280fa0def81299c7046074b'
            '27ef080439022062ab7a09ac4670f632110148c7e37234a5a1dee7042fedf909'
            'aa29945d20c1d0012102a17ec54ed6f8ba9a110d4f4f61f5b9d20024c16bf962'
            '39ec8a3ebfc4b1f658b0')

    def _check_tx_io(self, tx, spend_to, spend_duffs, fee_duffs,
                     change=None, change_duffs=None,
                     include_ps=False, min_rounds=None):
        o = tx.outputs()
        in_duffs = 0
        out_duffs = 0
        if not include_ps and min_rounds is None:
            for _in_ in tx.inputs():
                in_duffs += _in_.value_sats()
                assert _in_.ps_rounds is None
        elif include_ps:
            for _in_ in tx.inputs():
                in_duffs += _in_.value_sats()
        elif min_rounds is not None:
            assert len(o) == 1
            for _in_ in tx.inputs():
                in_duffs += _in_.value_sats()
                assert _in_.ps_rounds == min_rounds

        if change is not None:
            assert len(o) == 2

        for oi in o:
            out_duffs += oi.value
            if oi.value == spend_duffs:
                assert oi.address == spend_to
            elif oi.value == change_duffs:
                assert oi.address == change
            else:
                raise Exception(f'Unknown amount: {oi.value}')
        assert fee_duffs == (in_duffs - out_duffs)

    def test_make_unsigned_transaction(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'
        change = 'yanRmD5ZR66L1G51ixvXvUiJEmso5trn97'
        test_amounts = [0.0123, 0.123, 1.23, 5.123]
        test_fees = [226, 226, 374, 374]
        test_changes = [0.00769774, 0.17699774, 0.26999626, 2.90506399]

        coins = w.get_spendable_coins(domain=None)
        for i in range(len(test_amounts)):
            amount_duffs = to_duffs(test_amounts[i])
            change_duffs = to_duffs(test_changes[i])
            outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
            self._check_tx_io(tx, spend_to, amount_duffs, test_fees[i],
                              change, change_duffs)

        # check max amount
        amount_duffs = to_duffs(9.84805841)
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
        coins = w.get_spendable_coins(domain=None)
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        self._check_tx_io(tx, spend_to, amount_duffs, 932)  # no change

        amount_duffs = to_duffs(9.84805842)  # NotEnoughFunds
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
        coins = w.get_spendable_coins(domain=None)
        with self.assertRaises(NotEnoughFunds):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)

    def test_make_unsigned_transaction_include_ps(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'
        change = 'yanRmD5ZR66L1G51ixvXvUiJEmso5trn97'
        test_amounts = [0.0123, 0.123, 1.23, 5.123]
        test_fees = [226, 226, 374, 374]
        test_changes = [0.00769774, 0.17699774, 0.26999626, 2.90506399]

        coins = w.get_spendable_coins(domain=None, include_ps=True)
        for i in range(len(test_amounts)):
            amount_duffs = to_duffs(test_amounts[i])
            change_duffs = to_duffs(test_changes[i])
            outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
            self._check_tx_io(tx, spend_to, amount_duffs, test_fees[i],
                              change, change_duffs)

        # check max amount
        amount_duffs = to_duffs(9.84805841)
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
        coins = w.get_spendable_coins(domain=None)
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        self._check_tx_io(tx, spend_to, amount_duffs, 932)  # no change

        # check with include_ps
        amount_duffs = to_duffs(14.84811305)
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
        coins = w.get_spendable_coins(domain=None, include_ps=True)
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        self._check_tx_io(tx, spend_to, amount_duffs, 20468,  # no change
                          include_ps=True)

        # check max amount with include_ps
        amount_duffs = to_duffs(14.84811306)  # NotEnoughFunds
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
        coins = w.get_spendable_coins(domain=None, include_ps=True)
        with self.assertRaises(NotEnoughFunds):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)

    def test_make_unsigned_transaction_min_rounds(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'

        amount_duffs = to_duffs(1)
        outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]

        # check PSMinRoundsCheckFailed raises on inappropriate coins selection
        coins = w.get_spendable_coins(domain=None)
        with self.assertRaises(PSMinRoundsCheckFailed):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2)

        coins = w.get_spendable_coins(domain=None, min_rounds=C_RNDS)
        with self.assertRaises(PSMinRoundsCheckFailed):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=0)

        coins = w.get_spendable_coins(domain=None, min_rounds=0)
        with self.assertRaises(PSMinRoundsCheckFailed):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=1)

        coins = w.get_spendable_coins(domain=None, min_rounds=1)
        with self.assertRaises(PSMinRoundsCheckFailed):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2)

        # check different amounts and resulting fees
        test_amounts = [0.00001000]
        test_amounts += [0.00009640, 0.00005314, 0.00002269, 0.00005597,
                         0.00008291, 0.00009520, 0.00004102, 0.00009167,
                         0.00005735, 0.00001904, 0.00009245, 0.00002641,
                         0.00009115, 0.00003185, 0.00004162, 0.00003386,
                         0.00007656, 0.00006820, 0.00005044, 0.00006789]
        test_amounts += [0.00010000]
        test_amounts += [0.00839115, 0.00372971, 0.00654267, 0.00014316,
                         0.00491488, 0.00522527, 0.00627107, 0.00189861,
                         0.00092579, 0.00324560, 0.00032433, 0.00707310,
                         0.00737818, 0.00022760, 0.00235986, 0.00365554,
                         0.00975527, 0.00558680, 0.00506627, 0.00390911]
        test_amounts += [0.01000000]
        test_amounts += [0.74088413, 0.51044833, 0.81502578, 0.63804620,
                         0.38508255, 0.38838208, 0.20597175, 0.61405212,
                         0.23782970, 0.67059459, 0.29112021, 0.01425332,
                         0.44445507, 0.47530820, 0.04363325, 0.86807901,
                         0.82236638, 0.38637845, 0.04937359, 0.77029427]
        test_amounts += [1.00000000]
        test_amounts += [3.15592994, 1.51850574, 3.35457853, 1.20958635,
                         3.14494582, 3.43228624, 2.14182061, 1.30301733,
                         3.40340773, 1.21422826, 2.99683531, 1.3497565,
                         1.56368795, 2.60851955, 3.62983949, 3.13599564,
                         3.30433324, 2.67731925, 2.75157724, 1.48492533]

        test_fees = [99001, 90361, 94687, 97732, 94404, 91710, 90481, 95899,
                     90834, 94266, 98097, 90756, 97360, 90886, 96816, 95839,
                     96615, 92345, 93181, 94957, 93212, 90001, 60894, 27033,
                     45740, 85685, 8517, 77479, 72900, 10141, 7422, 75444,
                     67568, 92698, 62190, 77241, 64017, 34450, 24483, 41326,
                     93379, 9093, 100011, 12328, 55678, 98238, 96019, 92131,
                     62181, 3031, 95403, 17268, 41212, 88271, 74683, 54938,
                     69656, 36719, 92968, 64185, 62542, 62691, 71344, 1000,
                     10162, 50945, 45502, 42575, 8563, 74809, 20081, 99571,
                     62631, 78389, 19466, 25700, 32769, 50654, 19681, 3572,
                     69981, 70753, 45028, 8952]
        coins = w.get_spendable_coins(domain=None, min_rounds=2)
        for i in range(len(test_amounts)):
            amount_duffs = to_duffs(test_amounts[i])
            outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2)
            self._check_tx_io(tx, spend_to, amount_duffs,  # no change
                              test_fees[i],
                              min_rounds=2)
        assert min(test_fees) == 1000
        assert max(test_fees) == 100011

    def test_make_unsigned_transaction_high_fees(self):
        C_RNDS = PSCoinRounds.COLLATERAL
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'

        def prepare_outputs(amount):
            amount_duffs = to_duffs(amount)
            return [PartialTxOutput.from_address_and_value(spend_to,
                                                           amount_duffs)]

        coins = w.get_spendable_coins(None, min_rounds=2)
        coins_d0 = [c for c in coins if c.value_sats() == 100001]       # 38
        coins_d1 = [c for c in coins if c.value_sats() == 1000010]      # 21
        coins_d2 = [c for c in coins if c.value_sats() == 10000100]     # 16
        coins_d3 = [c for c in coins if c.value_sats() == 100001000]    # 2

        coins = coins_d3[:]
        outputs = prepare_outputs(1.5)
        with self.assertRaises(PSFeeTooHigh):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2)

        psman.limit_spend_fee = False
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                         min_rounds=2)
        assert tx.get_fee() == 50002000

        psman.limit_spend_fee = True
        outputs = prepare_outputs(1.51)
        coins = coins_d3[:1] + coins_d2[:9]
        with self.assertRaises(PSFeeTooHigh):
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2)
        psman.limit_spend_fee = False
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                         min_rounds=2)
        assert tx.get_fee() == 9001600

    def test_double_spend_warn(self):
        psman = self.wallet.psman
        assert psman.double_spend_warn == ''

        psman.state = PSStates.Mixing
        assert psman.double_spend_warn != ''
        psman.state = PSStates.Ready

        psman.last_mix_stop_time = time.time()
        assert psman.double_spend_warn != ''

        psman.last_mix_stop_time = time.time() - psman.wait_for_mn_txs_time
        assert psman.double_spend_warn == ''

    def test_last_denoms_tx_time(self):
        w = self.wallet
        psman = w.psman
        assert psman.last_denoms_tx_time == 0
        assert w.db.get_ps_data('last_denoms_tx_time') is None
        now = time.time()
        psman.last_denoms_tx_time = now
        assert psman.last_denoms_tx_time == now
        assert w.db.get_ps_data('last_denoms_tx_time') == now

    def test_broadcast_transaction(self):
        w = self.wallet
        psman = w.psman
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.network = NetworkBroadcastMock()

        # check spending ps_collateral currently in mixing
        c_outpoint, collateral = w.db.get_ps_collateral()
        psman.add_ps_spending_collateral(c_outpoint, 'uuid')
        inputs = w.get_spendable_coins([collateral[0]])
        dummy = w.dummy_address()
        outputs = [PartialTxOutput.from_address_and_value(dummy, COLLATERAL_VAL)]
        tx = w.make_unsigned_transaction(coins=inputs, outputs=outputs)

        psman.state = PSStates.Mixing
        with self.assertRaises(PSPossibleDoubleSpendError):
            coro = psman.broadcast_transaction(tx)
            asyncio.get_event_loop().run_until_complete(coro)

        psman.state = PSStates.Ready
        psman.last_mix_stop_time = time.time()
        with self.assertRaises(PSPossibleDoubleSpendError):
            coro = psman.broadcast_transaction(tx)
            asyncio.get_event_loop().run_until_complete(coro)

        psman.last_mix_stop_time = time.time() - psman.wait_for_mn_txs_time
        coro = psman.broadcast_transaction(tx)
        asyncio.get_event_loop().run_until_complete(coro)

        # check spending ps_denoms currently in mixing
        ps_denoms = w.db.get_ps_denoms()
        outpoint = list(ps_denoms.keys())[0]
        denom = ps_denoms[outpoint]
        psman.add_ps_spending_denom(outpoint, 'uuid')
        inputs = w.get_spendable_coins([denom[0]])
        dummy = w.dummy_address()
        outputs = [PartialTxOutput.from_address_and_value(dummy, COLLATERAL_VAL)]
        tx = w.make_unsigned_transaction(coins=inputs, outputs=outputs)

        psman.last_mix_stop_time = time.time()
        with self.assertRaises(PSPossibleDoubleSpendError):
            coro = psman.broadcast_transaction(tx)
            asyncio.get_event_loop().run_until_complete(coro)

    def test_sign_transaction(self):
        w = self.wallet
        psman = w.psman

        # test sign with no _keypairs_cache
        coro = psman.create_new_collateral_wfl()
        psman.state = PSStates.Mixing
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.completed
        psman._cleanup_new_collateral_wfl(force=True)
        assert not psman.new_collateral_wfl

        # test sign with _keypairs_cache
        psman._cache_keypairs(password=None)
        coro = psman.create_new_collateral_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_collateral_wfl
        assert wfl.completed
        psman._cleanup_new_collateral_wfl(force=True)
        assert not psman.new_collateral_wfl

    def test_calc_need_new_keypairs_cnt(self):
        w = self.wallet
        psman = w.psman
        psman.keep_amount = 14

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (378, 18, False)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (505, 26, False)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (632, 35, False)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (759, 43, False)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (2154, 136, False)

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (388, 21, False)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (694, 41, False)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (921, 56, False)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (1148, 71, False)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (3645, 237, False)

    def test_calc_need_new_keypairs_cnt_group_origin_by_addr(self):
        w = self.wallet
        psman = w.psman
        psman.group_origin_coins_by_addr = True
        psman.keep_amount = 8

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (278, 13, False)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (371, 19, False)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (464, 26, False)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (557, 32, False)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (1581, 100, False)

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (370, 20, False)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (669, 39, False)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (890, 54, False)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (1111, 69, False)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (3541, 231, False)

    def test_calc_need_new_keypairs_cnt_on_small_mix_funds(self):
        w = self.wallet
        psman = w.psman
        psman.keep_amount = 25

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (1325, 60, True)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (1770, 90, True)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (2215, 120, True)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (2660, 150, True)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (7555, 480, True)

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        assert psman.calc_need_new_keypairs_cnt() == (1865, 100, True)

        psman.mix_rounds = 3
        assert psman.calc_need_new_keypairs_cnt() == (3370, 200, True)

        psman.mix_rounds = 4
        assert psman.calc_need_new_keypairs_cnt() == (4475, 270, True)

        psman.mix_rounds = 5
        assert psman.calc_need_new_keypairs_cnt() == (5585, 345, True)

        psman.mix_rounds = 16
        assert psman.calc_need_new_keypairs_cnt() == (17795, 1160, True)

    def test_check_need_new_keypairs(self):
        w = self.wallet
        psman = w.psman
        psman.mix_rounds = 2
        psman.keep_amount = 2
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # check when wallet has no password
        assert psman.check_need_new_keypairs() == (False, None)

        # mock wallet.has_password
        prev_has_password = w.has_password
        w.has_keystore_encryption = lambda: True
        assert psman.check_need_new_keypairs() == (True, KPStates.Empty)
        assert psman.keypairs_state == KPStates.NeedCache

        assert psman.check_need_new_keypairs() == (False, None)
        assert psman.keypairs_state == KPStates.NeedCache

        psman._keypairs_state = KPStates.Caching
        assert psman.check_need_new_keypairs() == (False, None)
        assert psman.keypairs_state == KPStates.Caching

        psman.keypairs_state = KPStates.Empty
        psman._cache_keypairs(password=None)
        assert psman.keypairs_state == KPStates.Ready

        assert psman.check_need_new_keypairs() == (False, None)
        assert psman.keypairs_state == KPStates.Ready

        psman.keypairs_state = KPStates.Unused
        assert psman.check_need_new_keypairs() == (False, None)
        assert psman.keypairs_state == KPStates.Ready

        # clean some keypairs and check again
        psman.keypairs_state = KPStates.Unused
        psman._keypairs_cache[KP_SPENDABLE] = {}
        assert psman.check_need_new_keypairs() == (True, KPStates.Unused)
        assert psman.keypairs_state == KPStates.NeedCache
        psman._cache_keypairs(password=None)

        psman.keypairs_state = KPStates.Unused
        psman._keypairs_cache[KP_PS_COINS] = {}
        assert psman.check_need_new_keypairs() == (True, KPStates.Unused)
        assert psman.keypairs_state == KPStates.NeedCache
        psman._cache_keypairs(password=None)

        psman.keypairs_state = KPStates.Unused
        psman._keypairs_cache[KP_PS_CHANGE] = {}
        assert psman.check_need_new_keypairs() == (True, KPStates.Unused)
        assert psman.keypairs_state == KPStates.NeedCache
        psman._cache_keypairs(password=None)

        w.has_password = prev_has_password

    def test_find_addrs_not_in_keypairs(self):
        w = self.wallet
        psman = w.psman
        psman.mix_rounds = 2
        psman.keep_amount = 2
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        spendable = ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF',
                     'yUV122HRSuL1scPnvnqSqoQ3TV8SWpRcYd',
                     'yXYkfpHkyR8PRE9GtLB6huKpGvS27wqmTw',
                     'yZwFosFcLXGWomh11ddUNgGBKCBp7yueyo',
                     'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j']

        ps_spendable = ['yextsfRiRvGD5Gv36yhZ96ErYmtKxf4Ffp',
                        'ydeK8hNyBKs1o7eoCr7hC3QAHBTXyJudGU',
                        'ydxBaF2BKMTn7VSUeR7A3zk1jxYt6zCPQ2',
                        'yTAotTVzQipPEHFaR1CcsKEMGtyrdf1mo7',
                        'yVgfDzEodzZh6vfgkGTkmPXv1eJCUytdQS']

        ps_coins = ['yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns',
                    'yXwT5tUAp84wTfFuAJdkedtqGXkh3RP5zv',
                    'yh8nPSALi6mhsFbK5WPoCzBWWjHwonp5iz',
                    'yazd3VRfghZ2VhtFmzpmnYifJXdhLTm9np',
                    'ygEFS3cdoDosJCTdR2moQ9kdrik4UUcNge']

        ps_change = ['yanRmD5ZR66L1G51ixvXvUiJEmso5trn97',
                     'yaWPA5UrUe1kbnLfAbpdYtm3ePZje4YQ1G',
                     'yePrR43WFHSAXirUFsXKxXXRk6wJKiYXzU',
                     'yiYQjsdvXpPGt72eSy7wACwea85Enpa1p4',
                     'ydsi9BZnNUBWNbxN3ymYp4wkuw8q37rTfK']

        psman._cache_keypairs(password=None)
        unk_addrs = [w.dummy_address()] * 2
        res = psman._find_addrs_not_in_keypairs(unk_addrs + spendable)
        assert res == {unk_addrs[0]}

        res = psman._find_addrs_not_in_keypairs(ps_coins + unk_addrs)
        assert res == {unk_addrs[0]}

        res = psman._find_addrs_not_in_keypairs(ps_change + unk_addrs)
        assert res == {unk_addrs[0]}

        res = psman._find_addrs_not_in_keypairs(ps_change + ps_spendable)
        assert res == set()

    def test_cache_keypairs(self):
        w = self.wallet
        psman = w.psman

        psman.mix_rounds = 2
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        # types: incoming, spendable, ps spendable, ps coins, ps change
        cache_results = [0, 137, 0, 259, 12]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 137, 0, 433, 24]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 137, 0, 474, 26]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 5, 55, 111, 8]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 5, 132, 458, 31]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 5, 132, 901, 55]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

    def test_cache_keypairs_group_origin_by_addr(self):
        w = self.wallet
        psman = w.psman
        psman.group_origin_coins_by_addr = True

        # leave 3 coins
        # yXYkfpHkyR8PRE9GtLB6huKpGvS27wqmTw 30000000
        # yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j 50000000
        # yUV122HRSuL1scPnvnqSqoQ3TV8SWpRcYd 100000000
        coins = w.get_utxos(None, include_ps=True)
        for c in coins:
            val = c.value_sats()
            if (val in PS_DENOMS_VALS
                    or val in CREATE_COLLATERAL_VALS
                    or c.address in ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF',
                                     'yZwFosFcLXGWomh11ddUNgGBKCBp7yueyo']):
                w.set_frozen_state_of_coins([c.prevout.to_str()], True)

        psman.mix_rounds = 2
        w.db.set_ps_data('keep_amount', 1.65)  # to override min keep amount 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 3, 0, 520, 30]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]

        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        for txid in wfl.tx_order:
            w.db.add_islock(txid)
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            psman._process_by_new_denoms_wfl(txid, tx)
        assert not psman.new_denoms_wfl

        # types: incoming, spendable, ps spendable, ps coins, ps change
        cache_results = [5, 3, 54, 466, 30]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 3, 54, 2305, 140]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 3, 54, 3170, 190]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 3, 54, 218, 15]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 3, 54, 673, 45]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 3, 54, 4740, 300]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

    def test_cache_keypairs_on_small_mix_funds(self):
        w = self.wallet
        psman = w.psman

        coins0 = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                             mature_only=True, include_ps=True)
        coins = [c for c in coins0 if c.value_sats() < 50000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = [c for c in coins0 if c.value_sats() >= 100000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        assert sum([c.value_sats() for c in coins]) == 50000000  # 0.5 Dash

        psman.mix_rounds = 2
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        # types: incoming, spendable, ps spendable, ps coins, ps change
        cache_results = [5, 1, 0, 765, 40]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 1, 0, 1275, 75]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 1, 0, 2140, 120]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        coro = psman.find_untracked_ps_txs(log=False)  # find already mixed
        asyncio.get_event_loop().run_until_complete(coro)

        psman.mix_rounds = 2
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 1, 0, 111, 8]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 2
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [0, 1, 0, 458, 31]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

        psman.mix_rounds = 4
        psman.keep_amount = 10
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        cache_results = [5, 1, 0, 3715, 230]
        for i, cache_type in enumerate(KP_ALL_TYPES):
            assert len(psman._keypairs_cache[cache_type]) == cache_results[i]
        psman._cleanup_all_keypairs_cache()
        assert psman._keypairs_cache == {}
        psman.state = PSStates.Ready

    def test_cleanup_spendable_keypairs(self):
        # check spendable keypair for change is not cleaned up if change amount
        # is small (change output placed in middle of outputs sorted by bip69)
        w = self.wallet
        psman = w.psman
        psman.keep_amount = 16  # raise keep amount to make small change val
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # freeze some coins to make small change amount
        selected_coins_vals = [801806773, 50000000, 1000000]
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True)
        coins = [c for c in coins if not w.is_frozen_coin(c) ]
        coins = [c for c in coins if not c.value_sats() in selected_coins_vals]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        # check spendable coins
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True)
        coins = [c for c in coins if not w.is_frozen_coin(c) ]
        coins = sorted([c for c in coins if not w.is_frozen_coin(c)],
                       key=lambda x: -x.value_sats())
        assert coins[0].address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'
        assert coins[0].value_sats() == 801806773
        assert coins[1].address == 'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j'
        assert coins[1].value_sats() == 50000000
        assert coins[2].address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'
        assert coins[2].value_sats() == 1000000

        psman._cache_keypairs(password=None)
        spendable = ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF',
                     'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j']
        assert sorted(psman._keypairs_cache[KP_SPENDABLE].keys()) == spendable

        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed

        txid = wfl.tx_order[0]
        tx0 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[1]
        tx1 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[2]
        tx2 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[3]
        tx3 = Transaction(wfl.tx_data[txid].raw_tx)

        outputs = tx0.outputs()
        assert len(outputs) == 41
        change = outputs[33]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx1.outputs()
        assert len(outputs) == 24
        change = outputs[22]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx2.outputs()
        assert len(outputs) == 18
        change = outputs[17]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx3.outputs()
        assert len(outputs) == 8
        change = outputs[7]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        spendable = ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF']
        assert sorted(psman._keypairs_cache[KP_SPENDABLE].keys()) == spendable

    def test_cleanup_spendable_keypairs_group_origin_by_addr(self):
        # check spendable keypair for change is not cleaned up if change amount
        # is small (change output placed in middle of outputs sorted by bip69)
        w = self.wallet
        psman = w.psman
        psman.group_origin_coins_by_addr = True
        psman.keep_amount = 16  # raise keep amount to make small change val
        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)
        psman.state = PSStates.Mixing

        # freeze some coins to make small change amount
        selected_coins_vals = [801806773, 50000000, 1000000]
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True)
        coins = [c for c in coins if not w.is_frozen_coin(c) ]
        coins = [c for c in coins if not c.value_sats() in selected_coins_vals]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)

        # check spendable coins
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True)
        coins = [c for c in coins if not w.is_frozen_coin(c) ]
        coins = sorted([c for c in coins if not w.is_frozen_coin(c)],
                       key=lambda x: -x.value_sats())
        assert coins[0].address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'
        assert coins[0].value_sats() == 801806773
        assert coins[1].address == 'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j'
        assert coins[1].value_sats() == 50000000
        assert coins[2].address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'
        assert coins[2].value_sats() == 1000000

        psman._cache_keypairs(password=None)
        spendable = ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF',
                     'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j']
        assert sorted(psman._keypairs_cache[KP_SPENDABLE].keys()) == spendable

        coro = psman.create_new_denoms_wfl()
        asyncio.get_event_loop().run_until_complete(coro)
        wfl = psman.new_denoms_wfl
        assert wfl.completed

        txid = wfl.tx_order[0]
        tx0 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[1]
        tx1 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[2]
        tx2 = Transaction(wfl.tx_data[txid].raw_tx)
        txid = wfl.tx_order[3]
        tx3 = Transaction(wfl.tx_data[txid].raw_tx)

        outputs = tx0.outputs()
        assert len(outputs) == 40
        change = outputs[33]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx1.outputs()
        assert len(outputs) == 29
        change = outputs[22]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx2.outputs()
        assert len(outputs) == 18
        change = outputs[17]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        outputs = tx3.outputs()
        assert len(outputs) == 8
        change = outputs[7]
        assert change.address == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'

        spendable = ['yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF',
                     'yeeU1n6Bm4Y3rz7Y1JZb9gQAbsc4uv4Y5j']
        assert sorted(psman._keypairs_cache[KP_SPENDABLE].keys()) == spendable

    def test_filter_log_line(self):
        w = self.wallet
        test_line = ''
        assert filter_log_line(test_line) == test_line

        txid = bh2u(bytes(random.getrandbits(8) for _ in range(32)))
        test_line = 'load_and_cleanup rm %s ps data'
        assert filter_log_line(test_line % txid) == test_line % FILTERED_TXID

        txid = bh2u(bytes(random.getrandbits(8) for _ in range(32)))
        test_line = ('Error: err on checking tx %s from'
                     ' pay collateral workflow: wfl.uuid')
        assert filter_log_line(test_line % txid) == test_line % FILTERED_TXID

        test_line = 'Error: %s not found'
        filtered_line = filter_log_line(test_line % w.dummy_address())
        assert filtered_line == test_line % FILTERED_ADDR

    def test_calc_denoms_by_values(self):
        w = self.wallet
        psman = w.psman

        assert psman.calc_denoms_by_values() == {}

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        found_vals = {100001: 70, 1000010: 33, 10000100: 26,
                      100001000: 2, 1000010000: 0}
        assert psman.calc_denoms_by_values() == found_vals

    def test_min_new_denoms_from_coins_val(self):
        w = self.wallet
        psman = w.psman
        assert psman.min_new_denoms_from_coins_val == 110228

    def test_min_new_collateral_from_coins_val(self):
        w = self.wallet
        psman = w.psman
        assert psman.min_new_collateral_from_coins_val == 10193

    def test_check_enough_sm_denoms(self):
        w = self.wallet
        psman = w.psman

        denoms_by_vals = {}
        assert not psman.check_enough_sm_denoms(denoms_by_vals)

        denoms_by_vals = {100001: 5, 1000010: 0,
                          10000100: 5, 100001000: 0, 1000010000: 0}
        assert not psman.check_enough_sm_denoms(denoms_by_vals)

        denoms_by_vals = {100001: 4, 1000010: 5,
                          10000100: 0, 100001000: 0, 1000010000: 0}
        assert not psman.check_enough_sm_denoms(denoms_by_vals)

        denoms_by_vals = {100001: 5, 1000010: 5,
                          10000100: 0, 100001000: 0, 1000010000: 0}
        assert psman.check_enough_sm_denoms(denoms_by_vals)

        denoms_by_vals = {100001: 25, 1000010: 25,
                          10000100: 2, 100001000: 0, 1000010000: 0}
        assert psman.check_enough_sm_denoms(denoms_by_vals)

    def test_check_big_denoms_presented(self):
        w = self.wallet
        psman = w.psman

        denoms_by_vals = {}
        assert not psman.check_big_denoms_presented(denoms_by_vals)

        denoms_by_vals = {100001: 1, 1000010: 0,
                          10000100: 0, 100001000: 0, 1000010000: 0}
        assert not psman.check_big_denoms_presented(denoms_by_vals)

        denoms_by_vals = {100001: 0, 1000010: 1,
                          10000100: 0, 100001000: 0, 1000010000: 0}
        assert psman.check_big_denoms_presented(denoms_by_vals)

        denoms_by_vals = {100001: 0, 1000010: 0,
                          10000100: 1, 100001000: 0, 1000010000: 0}
        assert psman.check_big_denoms_presented(denoms_by_vals)

        denoms_by_vals = {100001: 0, 1000010: 0,
                          10000100: 1, 100001000: 0, 1000010000: 1}
        assert psman.check_big_denoms_presented(denoms_by_vals)

    def test_get_biggest_denoms_by_min_round(self):
        w = self.wallet
        psman = w.psman

        assert psman.get_biggest_denoms_by_min_round() == []

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        coins = psman.get_biggest_denoms_by_min_round()
        res_r = [c.ps_rounds for c in coins]
        res_v = [c.value_sats() for c in coins]
        assert res_r == [0] * 22 + [2] * 39
        assert res_v == ([10000100] * 10 + [1000010] * 12 + [100001000] * 2 +
                         [10000100] * 16 + [1000010] * 21)

    def test_all_mixed(self):
        w = self.wallet
        psman = w.psman

        coro = psman.find_untracked_ps_txs(log=False)
        asyncio.get_event_loop().run_until_complete(coro)

        # move spendable to ps_others
        for c in w.get_spendable_coins(domain=None):
            outpoint = c.prevout.to_str()
            w.db.add_ps_other(outpoint, (c.address, c.value_sats()))

        r = psman.mix_rounds
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))

        assert dn_balance == 500005000
        assert ps_balance == 0
        assert not psman.all_mixed

        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))

        # set rounds to psman.mix_rounds
        for outpoint in list(w.db.get_ps_denoms()):
            addr, val, prev_r = psman.pop_ps_denom(outpoint)
            psman.add_ps_denom(outpoint, (addr, val, r))

        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))

        assert dn_balance == 500005000
        assert ps_balance == 500005000
        assert psman.all_mixed

        psman.keep_amount = 4
        assert psman.all_mixed

        psman.keep_amount = 5
        assert not psman.all_mixed

        psman.keep_amount = 6
        assert not psman.all_mixed

    def enable_ps_ks(func):
        def setup_multi_ks(self, *args, **kwargs):
            w = self.wallet
            psman = w.psman
            psman.enable_ps_keystore()
            return func(self, *args, **kwargs)
        return setup_multi_ks

    @enable_ps_ks
    def test_enable_ps_keystore(self):
        w = self.wallet
        psman = w.psman

        assert type(psman.ps_keystore) == keystore.PS_BIP32_KeyStore
        assert psman.ps_ks_txin_type == 'p2pkh'
        keystore_d = copy.deepcopy(dict(w.keystore.dump()))
        keystore_d['type'] = 'ps_bip32'
        assert psman.ps_keystore.dump() == keystore_d

        keystore_d['addr_deriv_offset'] = 2
        w.db.put('ps_keystore', keystore_d)
        psman.load_ps_keystore()

        keystore_d = copy.deepcopy(w.keystore.dump())
        keystore_d['type'] = 'ps_bip32'
        keystore_d['addr_deriv_offset'] = 2
        assert psman.ps_keystore.dump() == keystore_d

    @enable_ps_ks
    def test_ps_ks_after_wallet_password_set_standard_bip32(self):
        w = self.wallet
        psman = w.psman

        keystore_d = copy.deepcopy(w.keystore.dump())
        xprv = ('tprv8gcGuHWitNxNiGHB37gwo6m41W1fNZBT5m79Fr56Q5F7HkagvRpCCPEs'
                'bPK9xcZFtQe9pcvBrDsEmGfzsY2bsB34MqbwVHFdapts9YM233g')
        assert keystore_d['xprv'] == xprv

        w.update_password(None, 'test password')

        keystore_d = copy.deepcopy(w.keystore.dump())
        keystore_d['type'] = 'ps_bip32'
        assert psman.ps_keystore.dump() == keystore_d
        assert keystore_d['xprv'] != xprv  # encrypted xprv

    @enable_ps_ks
    def test_ps_ks_derive_pubkey(self):
        w = self.wallet
        psman = w.psman

        pubk = w.keystore.derive_pubkey(for_change=False, n=0)
        pubk_chg = w.keystore.derive_pubkey(for_change=True, n=0)

        ps_pubk = psman.ps_keystore.derive_pubkey(for_change=False, n=0)
        ps_pubk_chg = psman.ps_keystore.derive_pubkey(for_change=True, n=0)

        assert pubk.hex() == ('02eda8b0b1356ea544d0741b90b7c5d5'
                              'a69ace3353ff4aa5253ada23458ba7c8ec')
        assert pubk_chg.hex() == ('03e8f307174144ef506fe6e5173da6a8'
                                  '7d0864c0d435cda8029ede0df060a5026d')

        assert ps_pubk.hex() == ('03248abb6109f7e0f60eb05c21df9ddc'
                                 'a21d237013a7f066e88cc0658fb4cf08a1')
        assert ps_pubk_chg.hex() == ('0253bb653ff17f4a5da462ed674c3ace'
                                     '7ed1e94d9b01712c2738ac3d06ee75289c')

    def synchronize_ps_ks(func):
        def generate_ps_addrs(self, *args, **kwargs):
            w = self.wallet
            psman = w.psman
            psman.synchronize()
            return func(self, *args, **kwargs)
        return generate_ps_addrs

    @enable_ps_ks
    @synchronize_ps_ks
    def test_ps_ks_addrs_sync(self):
        w = self.wallet
        psman = w.psman
        addrs = w.get_unused_addresses()
        ps_ks_addrs = psman.get_unused_addresses()
        assert len(addrs) == 20
        assert len(ps_ks_addrs) == 20
        assert not set(addrs) & set(ps_ks_addrs)

    @enable_ps_ks
    @synchronize_ps_ks
    def test_sign_tx_with_ps_ks_input(self):
        w = self.wallet
        psman = w.psman
        addrs = w.get_unused_addresses()
        ps_ks_addrs = psman.get_unused_addresses()

        # make tx from wallet keystore utxo to ps keystore address
        inputs = sorted(w.get_spendable_coins(domain=None),
                        key=lambda x: x.address)[-1:]
        w.add_input_info(inputs[0])
        # one input: from yjGqNe2m4zFZ6RKVxo1VkN6qGUzQbbrGkK val is 1000010
        assert inputs[0].value_sats() == 1000010
        assert inputs[0].address == 'yjGqNe2m4zFZ6RKVxo1VkN6qGUzQbbrGkK'
        # one ouput to yWiHa55aVbBen3ddhyTwQRsMjvwDUFwiya, no fee
        oaddr1 = ps_ks_addrs[0]
        assert oaddr1 == 'yWiHa55aVbBen3ddhyTwQRsMjvwDUFwiya'
        outputs = [PartialTxOutput.from_address_and_value(oaddr1, 1000010)]

        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].sequence = 0xffffffff
        tx = psman.sign_transaction(tx, None)
        txid1 = tx.txid()

        w.add_transaction(tx)
        w.add_unverified_tx(txid1, TX_HEIGHT_UNCONFIRMED)

        # make tx from txid1 output to ps keystore address
        inputs = w.get_spendable_coins(domain=None)
        inputs = [i for i in inputs if i.is_ps_ks]
        w.add_input_info(inputs[0])
        oaddr2 = ps_ks_addrs[1]
        assert oaddr2 == 'yNB6U2WKEKw4gjDnfbmBoRZKbTbsGHvvZ9'
        outputs = [PartialTxOutput.from_address_and_value(oaddr2, 1000010)]

        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].sequence = 0xffffffff
        tx = psman.sign_transaction(tx, None)
        txid2 = tx.txid()

        w.add_transaction(tx)
        w.add_unverified_tx(txid2, TX_HEIGHT_UNCONFIRMED)

        # make tx from txid2 output to wallet keystore address
        inputs = w.get_spendable_coins(domain=None)
        inputs = [i for i in inputs if i.is_ps_ks]
        w.add_input_info(inputs[0])
        oaddr3 = addrs[0]
        assert oaddr3 == 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'
        outputs = [PartialTxOutput.from_address_and_value(oaddr3, 1000010)]

        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].sequence = 0xffffffff
        tx = psman.sign_transaction(tx, None)
        txid3 = tx.txid()
        assert len(txid3) == 64

    @enable_ps_ks
    @synchronize_ps_ks
    def test_sign_message_with_ps_keystore(self):
        w = self.wallet
        psman = w.psman
        ps_ks_addr = psman.get_addresses()[0]
        msg = 'test message'.encode('utf-8')
        res = w.sign_message(ps_ks_addr, msg, None)
        verified = ecc.verify_message_with_address(ps_ks_addr, res, msg)
        assert verified

    def test_create_ps_ks_from_seed_ext_password(self):
        w = self.wallet
        psman = w.psman
        psman.w_ks_type = 'hardware'  # mock
        psman.create_ps_ks_from_seed_ext_password(TEST_MNEMONIC, '222', None)
        ps_ks_dump = psman.ps_keystore.dump()
        assert sorted(ps_ks_dump.keys()) == sorted(['type', 'pw_hash_version',
                                                    'seed', 'seed_type',
                                                    'passphrase',
                                                    'xpub', 'xprv',
                                                    'derivation',
                                                    'root_fingerprint'])
        assert ps_ks_dump['type'] == 'ps_bip32'
        assert ps_ks_dump['pw_hash_version'] == 1
        assert ps_ks_dump['seed'] == TEST_MNEMONIC
        assert ps_ks_dump['seed_type'] == 'standard'
        assert ps_ks_dump['passphrase'] == '222'
        assert ps_ks_dump['xpub'] == ('tpubD6NzVbkrYhZ4XbJGdLV1VF6RSPjMCxn9hM6'
                                      'grY9bhAhPsnRxPEVjyUZbhbB6zMoWTqJEJkwsLv'
                                      'jmpKkjgGork7iG88HH1J8gUGSe8JuzafV')
        assert ps_ks_dump['xprv'] == ('tprv8ZgxMBicQKsPe8GUjgpR5qSJsNDR3dbF83V'
                                      'ua27JGtu13JBBkqg9nywjXTKjdDVbo95m2vz1CG'
                                      'eXR35wy621YuMw1bNevP9qe55f5k5vWqP')
        assert ps_ks_dump['derivation'] == 'm'
        assert ps_ks_dump['root_fingerprint'] == '1345bb4d'

    def test_is_ps_ks_encrypted(self):
        w = self.wallet
        psman = w.psman
        psman.w_ks_type = 'hardware'  # mock
        psman.create_ps_ks_from_seed_ext_password(TEST_MNEMONIC, '222', '111')
        assert psman.ps_keystore.dump()
        assert psman.is_ps_ks_encrypted()
        psman.update_ps_ks_password('111', '')
        assert not psman.is_ps_ks_encrypted()

    def test_need_password(self):
        w = self.wallet
        psman = w.psman
        psman.w_ks_type = 'hardware'  # mock
        psman.create_ps_ks_from_seed_ext_password(TEST_MNEMONIC, '222', '111')
        assert psman.ps_keystore.dump()
        assert psman.need_password()
        psman.update_ps_ks_password('111', '')
        assert not psman.need_password()

    def test_update_ps_ks_password(self):
        w = self.wallet
        psman = w.psman
        psman.w_ks_type = 'hardware'  # mock
        psman.create_ps_ks_from_seed_ext_password(TEST_MNEMONIC, '222', '')
        assert not psman.is_ps_ks_encrypted()
        ps_ks_dump = psman.ps_keystore.dump()
        assert ps_ks_dump['xprv'] == ('tprv8ZgxMBicQKsPe8GUjgpR5qSJsNDR3dbF83V'
                                      'ua27JGtu13JBBkqg9nywjXTKjdDVbo95m2vz1CG'
                                      'eXR35wy621YuMw1bNevP9qe55f5k5vWqP')
        psman.update_ps_ks_password(None, '111')
        assert psman.is_ps_ks_encrypted()
        ps_ks_dump = psman.ps_keystore.dump()
        assert ps_ks_dump['xprv'] != ('tprv8ZgxMBicQKsPe8GUjgpR5qSJsNDR3dbF83V'
                                      'ua27JGtu13JBBkqg9nywjXTKjdDVbo95m2vz1CG'
                                      'eXR35wy621YuMw1bNevP9qe55f5k5vWqP')
        assert ps_ks_dump['seed'] != TEST_MNEMONIC

    @enable_ps_ks
    @synchronize_ps_ks
    def test_is_ps_ks_inputs_in_tx(self):
        w = self.wallet
        psman = w.psman
        addrs = w.get_unused_addresses()
        ps_ks_addrs = psman.get_unused_addresses()

        # make tx from wallet keystore utxo to ps keystore address
        inputs = sorted(w.get_spendable_coins(domain=None),
                        key=lambda x: x.address)[-1:]
        w.add_input_info(inputs[0])
        oaddr1 = ps_ks_addrs[0]
        outputs = [PartialTxOutput.from_address_and_value(oaddr1, 1000010)]
        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)

        assert not psman.is_ps_ks_inputs_in_tx(tx)

        tx.inputs()[0].sequence = 0xffffffff
        tx = psman.sign_transaction(tx, None)
        txid1 = tx.txid()
        w.add_transaction(tx)
        w.add_unverified_tx(txid1, TX_HEIGHT_UNCONFIRMED)

        # make tx from txid1 output to ps keystore address
        inputs = w.get_spendable_coins(domain=None)
        inputs = [i for i in inputs if i.is_ps_ks]
        w.add_input_info(inputs[0])
        oaddr2 = ps_ks_addrs[1]
        outputs = [PartialTxOutput.from_address_and_value(oaddr2, 1000010)]
        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].sequence = 0xffffffff

        assert psman.is_ps_ks_inputs_in_tx(tx)

        tx = psman.sign_transaction(tx, None)
        txid2 = tx.txid()
        w.add_transaction(tx)
        w.add_unverified_tx(txid2, TX_HEIGHT_UNCONFIRMED)

        # make tx from txid2 output to wallet keystore address
        inputs = w.get_spendable_coins(domain=None)
        inputs = [i for i in inputs if i.is_ps_ks]
        w.add_input_info(inputs[0])
        oaddr3 = addrs[0]
        outputs = [PartialTxOutput.from_address_and_value(oaddr3, 1000010)]
        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].sequence = 0xffffffff

        assert psman.is_ps_ks_inputs_in_tx(tx)

    @enable_ps_ks
    @synchronize_ps_ks
    def test_prepare_funds_from_hw_wallet(self):
        w = self.wallet
        psman = w.psman
        psman.keep_amount = 2
        psman.mix_rounds = 2

        # test with spendable amount > keep_amount
        coins0 = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                             mature_only=True, include_ps=True)
        coins = [c for c in coins0 if c.value_sats() < 50000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = [c for c in coins0 if c.value_sats() > 800000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]

        assert sum([c.value_sats() for c in coins]) == 350002000  # 3.5 Dash

        tx = psman.prepare_funds_from_hw_wallet()
        assert tx.txid()
        inputs = tx.inputs()
        outputs = tx.outputs()
        assert len(inputs) == 3
        assert len(outputs) == 2
        change = outputs[0]
        ps_ks_out = outputs[1]

        for txin in inputs:
            assert not psman.is_ps_ks(txin.address)

        assert change.value == 99694653
        assert not psman.is_ps_ks(change.address)

        assert ps_ks_out.value == 200306825
        assert psman.is_ps_ks(ps_ks_out.address)

        # test with spendable amount < keep_amount
        coins = [c for c in coins0 if c.value_sats() >= 100000000]
        coins_str = {c.prevout.to_str() for c in coins}
        w.set_frozen_state_of_coins(coins_str, True)
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        assert sum([c.value_sats() for c in coins]) == 50000000  # 0.5 Dash

        tx = psman.prepare_funds_from_hw_wallet()
        assert tx.txid()
        inputs = tx.inputs()
        outputs = tx.outputs()
        assert len(inputs) == 1
        assert len(outputs) == 1
        ps_ks_out = outputs[0]

        assert ps_ks_out.value == 49999807
        assert psman.is_ps_ks(ps_ks_out.address)
        assert psman.get_tmp_reserved_address() == ps_ks_out.address

    @enable_ps_ks
    @synchronize_ps_ks
    def test_tmp_reserved_address(self):
        w = self.wallet
        psman = self.wallet.psman
        addr = 'address'

        assert psman.get_tmp_reserved_address() == ''
        assert w.db.get_ps_data('tmp_reserved_address') is None

        psman.set_tmp_reserved_address(addr)
        assert psman.get_tmp_reserved_address() == addr
        assert w.db.get_ps_data('tmp_reserved_address') == addr

        psman.set_tmp_reserved_address('')
        assert psman.get_tmp_reserved_address() == ''
        assert w.db.get_ps_data('tmp_reserved_address') == ''

    @enable_ps_ks
    @synchronize_ps_ks
    def test_reserve_addresses_tmp(self):
        w = self.wallet
        psman = w.psman

        with self.assertRaises(Exception):
            psman.reserve_addresses(10, tmp=True)
        with self.assertRaises(Exception):
            psman.reserve_addresses(1, for_change=True, tmp=True)
        with self.assertRaises(Exception):
            psman.reserve_addresses(1, data='*', tmp=True)

        ps_ks_unused_addr = psman.get_unused_addresses()[0]

        res1 = psman.reserve_addresses(1, tmp=True)

        assert psman.get_unused_addresses()[0] != ps_ks_unused_addr

    @enable_ps_ks
    @synchronize_ps_ks
    def test_calc_rounds_for_denominate_tx(self):
        w = self.wallet
        psman = w.psman
        dval = 100001
        fake_outpoint = '0'*64 + ':0'
        ps_ks_addrs = psman.get_unused_addresses()
        main_ks_addrs = w.get_unused_addresses()

        new_outpoints = [
            (fake_outpoint, ps_ks_addrs[0], dval),
            (fake_outpoint, main_ks_addrs[0], dval),
            (fake_outpoint, ps_ks_addrs[1], dval),
            (fake_outpoint, main_ks_addrs[1], dval),
            (fake_outpoint, ps_ks_addrs[2], dval),
        ]
        input_rounds = [5, 3, 1, 0, 5]
        out_rounds = psman._calc_rounds_for_denominate_tx(new_outpoints,
                                                          input_rounds)
        assert out_rounds is not input_rounds
        assert out_rounds == list(map(lambda x: x+1, input_rounds[:]))

        psman.w_ks_type = 'hardware'  # mock
        out_rounds = psman._calc_rounds_for_denominate_tx(new_outpoints,
                                                          input_rounds)
        assert out_rounds is not input_rounds
        assert out_rounds == [4, 6, 2, 6, 1]
        # another test
        new_outpoints = [
            (fake_outpoint, ps_ks_addrs[0], dval),
            (fake_outpoint, ps_ks_addrs[1], dval),
            (fake_outpoint, main_ks_addrs[0], dval),
            (fake_outpoint, main_ks_addrs[1], dval),
        ]
        input_rounds = [2, 3, 3, 1]
        out_rounds = psman._calc_rounds_for_denominate_tx(new_outpoints,
                                                          input_rounds)
        assert out_rounds is not input_rounds
        assert out_rounds == [3, 2, 4, 4]

    @enable_ps_ks
    @synchronize_ps_ks
    def test_prepare_funds_from_ps_keystore(self):
        w = self.wallet
        psman = w.psman

        with self.assertRaises(NotEnoughFunds):
            psman.prepare_funds_from_ps_keystore(None)

        unused = psman.get_unused_addresses()
        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]

        coins1 = coins[:1]
        oaddr1 = unused[0]
        outputs1 = [PartialTxOutput.from_address_and_value(oaddr1, '!')]
        tx = w.make_unsigned_transaction(coins=coins1, outputs=outputs1)
        tx = w.sign_transaction(tx, None)
        w.add_transaction(tx)

        coins2 = coins[1:2]
        oaddr2 = unused[1]
        outputs2 = [PartialTxOutput.from_address_and_value(oaddr2, '!')]
        tx2 = w.make_unsigned_transaction(coins=coins2, outputs=outputs2)
        tx2 = w.sign_transaction(tx2, None)
        txid2 = tx2.txid()
        w.add_transaction(tx2)
        psman.add_ps_denom(f'{txid2}:0', (oaddr2, 100001, 0))

        tx_list = psman.prepare_funds_from_ps_keystore(None)
        assert len(tx_list) == 2
        for tx2 in tx_list:
            assert tx2.is_complete()

    @enable_ps_ks
    @synchronize_ps_ks
    def test_check_funds_on_ps_keystore(self):
        w = self.wallet
        psman = w.psman

        assert not psman.check_funds_on_ps_keystore()

        coins = w.get_utxos(None, excluded_addresses=w._frozen_addresses,
                            mature_only=True, include_ps=True)
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        coins = coins[:1]
        unused = psman.get_unused_addresses()
        oaddr = unused[0]
        outputs = [PartialTxOutput.from_address_and_value(oaddr, '!')]
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        tx = w.sign_transaction(tx, None)
        w.add_transaction(tx)

        assert psman.check_funds_on_ps_keystore()

    def test_prob_denominate_tx_coin(self):
        w = self.wallet
        psman = w.psman
        coins = w.get_utxos(None, mature_only=True)
        denom_coins = []
        for c in coins:
            if psman.prob_denominate_tx_coin(c):
                denom_coins.append(c)
        assert len(denom_coins) == 78
        coro = psman.find_untracked_ps_txs(log=False)
        found_txs = asyncio.get_event_loop().run_until_complete(coro)
        for c in denom_coins:
            utxos = w.get_utxos([c.address])
            assert len(utxos) == 1
            assert utxos[0].ps_rounds in [1, 2]

    def test_make_unsigned_transaction_ps_coins_no_ps_data(self):
        """PS tx with probable denom coins"""
        w = self.wallet
        psman = w.psman
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'

        # check different amounts and resulting fees
        test_amounts = [0.00001000]
        test_amounts += [0.00009640, 0.00005314, 0.00002269, 0.00005597,
                         0.00008291, 0.00009520, 0.00004102, 0.00009167,
                         0.00005735, 0.00001904, 0.00009245, 0.00002641,
                         0.00009115, 0.00003185, 0.00004162, 0.00003386,
                         0.00007656, 0.00006820, 0.00005044, 0.00006789]
        test_amounts += [0.00010000]
        test_amounts += [0.00839115, 0.00372971, 0.00654267, 0.00014316,
                         0.00491488, 0.00522527, 0.00627107, 0.00189861,
                         0.00092579, 0.00324560, 0.00032433, 0.00707310,
                         0.00737818, 0.00022760, 0.00235986, 0.00365554,
                         0.00975527, 0.00558680, 0.00506627, 0.00390911]
        test_amounts += [0.01000000]
        test_amounts += [0.74088413, 0.51044833, 0.81502578, 0.63804620,
                         0.38508255, 0.38838208, 0.20597175, 0.61405212,
                         0.23782970, 0.67059459, 0.29112021, 0.01425332,
                         0.44445507, 0.47530820, 0.04363325, 0.86807901,
                         0.82236638, 0.38637845, 0.04937359, 0.77029427]
        test_amounts += [1.00000000]
        test_amounts += [3.15592994, 1.51850574, 3.35457853, 1.20958635,
                         3.14494582, 3.43228624, 2.14182061, 1.30301733,
                         3.40340773, 1.21422826, 2.99683531, 1.3497565,
                         1.56368795, 2.60851955, 3.62983949, 3.13599564,
                         3.30433324, 2.67731925, 2.75157724, 1.48492533]

        test_fees = [99001, 90361, 94687, 97732, 94404, 91710, 90481, 95899,
                     90834, 94266, 98097, 90756, 97360, 90886, 96816, 95839,
                     96615, 92345, 93181, 94957, 93212, 90001, 60894, 27033,
                     45740, 85685, 8517, 77479, 72900, 10141, 7422, 75444,
                     67568, 92698, 62190, 77241, 64017, 34450, 24483, 41326,
                     93379, 9093, 100011, 12328, 55678, 98238, 96019, 92131,
                     62181, 3031, 95403, 17268, 41212, 88271, 74683, 54938,
                     69656, 36719, 92968, 64185, 62542, 62691, 71344, 1000,
                     10162, 50945, 45502, 42575, 8563, 74809, 20081, 99571,
                     62631, 78389, 19466, 25700, 32769, 50654, 19681, 3572,
                     69981, 70753, 45028, 8952]
        coins = w.get_spendable_coins(domain=None,
                                      min_rounds=2, no_ps_data=True)
        for i in range(len(test_amounts)):
            amount_duffs = to_duffs(test_amounts[i])
            outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             min_rounds=2, no_ps_data=True)
            self._check_tx_io(tx, spend_to, amount_duffs,  # no change
                              test_fees[i],
                              min_rounds=0)
        assert min(test_fees) == 1000
        assert max(test_fees) == 100011

    def test_make_unsigned_transaction_no_ps_data(self):
        """Regular tx with probable denom coins, must spent denoms last"""
        w = self.wallet
        psman = w.psman
        spend_to = 'yiXJV2PodX4uuadFtt6e7wMTNkydHpp8ns'

        # check different amounts and resulting fees
        coins = w.get_spendable_coins(domain=None,
                                      include_ps=True, no_ps_data=True)
        test_amounts = [3.0, 5.0, 7.0, 10.98, 11.0, 13.0, 14.8]
        for i in range(len(test_amounts)):
            amount_duffs = to_duffs(test_amounts[i])
            outputs = [PartialTxOutput.from_address_and_value(spend_to, amount_duffs)]
            tx = w.make_unsigned_transaction(coins=coins, outputs=outputs,
                                             no_ps_data=True)
            if amount_duffs < 1098000000:
                for txin in tx.inputs():
                    assert txin.value_sats() not in PS_DENOMS_VALS
            else:
                found = 0
                for txin in tx.inputs():
                    found += 1 if txin.value_sats() in PS_DENOMS_VALS else 0
                assert found > 0

    def test_PSKsInternalAddressCorruption(self):
        e = PSKsInternalAddressCorruption()
        assert len(str(e)) > 0

    def test_on_wallet_password_set(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing

        async def test_coro():
            psman.on_wallet_password_set()
        asyncio.get_event_loop().run_until_complete(test_coro())

    def test_clean_keypairs_on_timeout(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing
        psman._cache_keypairs(password=None)
        psman.state = PSStates.Ready
        psman.keypairs_state = KPStates.Unused
        psman.last_mix_stop_time = time.time()
        coro = psman.clean_keypairs_on_timeout()
        asyncio.get_event_loop().run_until_complete(coro)

    def test_make_keypairs_cache(self):
        w = self.wallet
        psman = w.psman
        psman.state = PSStates.Mixing
        psman.keypairs_state = KPStates.NeedCache
        coro = psman._make_keypairs_cache(None)
        asyncio.get_event_loop().run_until_complete(coro)
        coro = psman._make_keypairs_cache('')
        asyncio.get_event_loop().run_until_complete(coro)

    @enable_ps_ks
    @synchronize_ps_ks
    def test_get_address_path_str(self):
        w = self.wallet
        psman = w.psman
        addr = psman.get_unused_addresses()[0]
        assert psman.get_address_path_str(addr) == r'm/2/0'
        assert psman.get_address_path_str('unknownaddr') is None

    @enable_ps_ks
    @synchronize_ps_ks
    def test_get_public_key(self):
        w = self.wallet
        psman = w.psman
        addr = psman.get_unused_addresses()[0]
        assert psman.get_public_key(addr).startswith('03248abb6109f7')

    @enable_ps_ks
    @synchronize_ps_ks
    def test_get_public_keys(self):
        w = self.wallet
        psman = w.psman
        addr = psman.get_unused_addresses()[0]
        assert psman.get_public_keys(addr)[0].startswith('03248abb6109f7')

    @enable_ps_ks
    @synchronize_ps_ks
    def test_check_address(self):
        w = self.wallet
        psman = w.psman
        addr = psman.get_unused_addresses()[0]
        psman.check_address(addr)

    @enable_ps_ks
    @synchronize_ps_ks
    def test_get_all_known_addresses_beyond_gap_limit(self):
        w = self.wallet
        psman = w.psman
        psman.get_all_known_addresses_beyond_gap_limit()
