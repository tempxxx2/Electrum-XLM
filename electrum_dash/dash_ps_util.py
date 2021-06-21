# -*- coding: utf-8 -*-

import asyncio
import copy
import re
import time
import logging
from decimal import Decimal
from enum import IntEnum

import attr

from . import constants, util
from .bitcoin import is_address, COIN
from .dash_ps_net import PSDenoms
from .dash_tx import PSTxTypes, SPEC_TX_NAMES
from .i18n import _
from .transaction import Transaction
from .util import IntEnumWithCheck, profiler


TXID_PATTERN = re.compile('([0123456789ABCDEFabcdef]{64})')
ADDR_PATTERN = re.compile(
    '([123456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    'abcdefghijkmnopqrstuvwxyz]{20,80})')
FILTERED_TXID = '<filtered txid>'
FILTERED_ADDR = '<filtered address>'


def to_duffs(amount):
    return round(Decimal(str(amount))*COIN)


PS_DENOMS_DICT = {
    to_duffs(10.0001): PSDenoms.D10,
    to_duffs(1.00001): PSDenoms.D1,
    to_duffs(0.100001): PSDenoms.D0_1,
    to_duffs(0.0100001): PSDenoms.D0_01,
    to_duffs(0.00100001): PSDenoms.D0_001,
}

COLLATERAL_VAL = to_duffs(0.0001)
CREATE_COLLATERAL_VAL = COLLATERAL_VAL*4
CREATE_COLLATERAL_VALS = [COLLATERAL_VAL*i for i in range(1,11)]
PS_DENOMS_VALS = sorted(PS_DENOMS_DICT.keys())
MIN_DENOM_VAL = PS_DENOMS_VALS[0]
PS_VALS = PS_DENOMS_VALS + CREATE_COLLATERAL_VALS

PS_MIXING_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                  PSTxTypes.NEW_COLLATERAL,
                                                  PSTxTypes.PAY_COLLATERAL,
                                                  PSTxTypes.DENOMINATE]))

PS_SAVED_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                 PSTxTypes.NEW_COLLATERAL,
                                                 PSTxTypes.PAY_COLLATERAL,
                                                 PSTxTypes.DENOMINATE,
                                                 PSTxTypes.PRIVATESEND,
                                                 PSTxTypes.SPEND_PS_COINS,
                                                 PSTxTypes.OTHER_PS_COINS]))


def filter_log_line(line):
    '''Filter out txids/addresses from log lines'''
    pos = 0
    output_line = ''
    while pos < len(line):
        m = TXID_PATTERN.search(line, pos)
        if m:
            output_line += line[pos:m.start()]
            output_line += FILTERED_TXID
            pos = m.end()
            continue

        m = ADDR_PATTERN.search(line, pos)
        if m:
            addr = m.group()
            if is_address(addr, net=constants.net):
                output_line += line[pos:m.start()]
                output_line += FILTERED_ADDR
                pos = m.end()
                continue

        output_line += line[pos:]
        break
    return output_line


def ps_coin_rounds_str(ps_rounds):
    '''Return string repr of coin rounds or coin type for PS related coins'''
    if ps_rounds not in PS_COIN_ROUNDS_STR:
        return str(ps_rounds)
    return PS_COIN_ROUNDS_STR[int(ps_rounds)]


def sort_utxos_by_ps_rounds(x):
    '''Key fn for sorting from UI based on PS coin rounds/related coin type'''
    ps_rounds = x.ps_rounds
    if ps_rounds is None:
        return PSCoinRounds.MINUSINF
    return ps_rounds


def varint_size(val):
    '''Calc Bitcoin VarInt size in bytes'''
    if val < 253:
        return 1
    elif val < 2**16:
        return 3
    elif val < 2**32:
        return 5
    elif val < 2**64:
        return 9


def calc_tx_size(in_cnt, out_cnt, max_size=False):
    '''Calc P2PKH tx size based on inputs/outputs count (sig size can vary)'''
    # base size is 4 bytes version + 4 bytes lock_time
    max_tx_size = 4 + 4
    # in size is 36 bytes outpoint + 1b len + iscript + 4 bytes sequence_no
    # iscript is 1b varint + sig (71-73 bytes) + 1b varint + 33 bytes pubk
    # max in size is 36 + 1 + (1 + 73 + 1 + 33) + 4 = 149
    max_tx_size += varint_size(in_cnt) + in_cnt * (149 if max_size else 148)

    # out size is 8 byte value + 1b varint + 25 bytes p2phk script
    # out size is 8 + 1 + 25 = 34
    max_tx_size += varint_size(out_cnt) + out_cnt * 34
    return max_tx_size


def calc_tx_fee(in_cnt, out_cnt, fee_per_kb, max_size=False):
    '''Calc P2PKH tx fee based on inputs/outputs count and fee_per_kb rate'''
    return round(calc_tx_size(in_cnt, out_cnt, max_size) * fee_per_kb / 1000)


class PSCoinRounds(IntEnum):
    '''PS related coin types'''
    MINUSINF = -1e9     # For sorting/selecting by rounds non PS related coins
    OTHER = -3          # Not PS coins arriwed on address used by PS txs
    MIX_ORIGIN = -2     # Coins left as change from new denoms/collateral txs
    COLLATERAL = -1     # collateral amount coins


PS_COIN_ROUNDS_STR = {
    PSCoinRounds.MINUSINF: _('Unknown'),
    PSCoinRounds.OTHER: 'Other',
    PSCoinRounds.MIX_ORIGIN: 'Mix Origin',
    PSCoinRounds.COLLATERAL: 'Collateral',
}


class PSStates(IntEnum):
    '''PSManager states'''
    Unsupported = 0         # PS is unsupported on this wallet
    Disabled = 1            # PS is disabled yet
    Initializing = 2        # Check data, find untracked txs
    Ready = 3               # Ready to mixing
    StartMixing = 4         # Starting mixing process
    Mixing = 5              # Mixing is running
    StopMixing = 6          # Stopping mixing process
    FindingUntracked = 7    # Finding untracked txs
    Errored = 8             # Some error is encountered on PS data adding/check
    Cleaning = 9            # Cleaning PS data


class PSTxData:
    '''
    Transaction data placed to workflows

    uuid: unique workflow id for addresses reservation
    tx_type: PSTxTypes type
    txid: tx hash
    raw_tx: raw tx data
    sent: time when tx was sent to network
    next_send: minimal time when next send attempt should occur
    '''

    __slots__ = 'uuid tx_type txid raw_tx sent next_send'.split()

    def __init__(self, **kwargs):
        for k in self.__slots__:
            if k in kwargs:
                if k == 'tx_type':
                    setattr(self, k, int(kwargs[k]))
                else:
                    setattr(self, k, kwargs[k])
            else:
                setattr(self, k, None)

    def _as_dict(self):
        '''return dict txid -> (uuid, sent, next_send, tx_type, raw_tx)'''
        return {self.txid: (self.uuid, self.sent, self.next_send,
                            self.tx_type, self.raw_tx)}

    @classmethod
    def _from_txid_and_tuple(cls, txid, data_tuple):
        '''
        New instance from txid
        and (uuid, sent, next_send, tx_type, raw_tx) tuple
        '''
        uuid, sent, next_send, tx_type, raw_tx = data_tuple
        return cls(uuid=uuid, txid=txid, raw_tx=raw_tx,
                   tx_type=tx_type, sent=sent, next_send=next_send)

    def __eq__(self, other):
        if type(other) != PSTxData:
            return False
        if id(self) == id(other):
            return True
        for k in self.__slots__:
            if getattr(self, k) != getattr(other, k):
                return False
        return True

    async def send(self, psman, ignore_next_send=False):
        '''Try broadcast tx with electrum network, on fail make timeout'''
        err = ''
        if self.sent:
            return False, err
        now = time.time()
        if not ignore_next_send:
            next_send = self.next_send
            if next_send and next_send > now:
                return False, err
        try:
            tx = Transaction(self.raw_tx)
            await psman.network.broadcast_transaction(tx)
            self.sent = time.time()
            return True, err
        except Exception as e:
            err = str(e)
            self.next_send = now + 10
            return False, err


class PSTxWorkflow:
    '''
    uuid: unique id for addresses reservation
    completed: workflow creation completed
    tx_data: txid -> PSTxData
    tx_order: creation order of workflow txs
    '''

    __slots__ = 'uuid completed tx_data tx_order'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.completed = kwargs.pop('completed', False)
        self.tx_order = kwargs.pop('tx_order', [])[:]  # copy
        tx_data = kwargs.pop('tx_data', {})
        self.tx_data = {}  # copy
        for txid, v in tx_data.items():
            if type(v) in (tuple, list):
                self.tx_data[txid] = PSTxData._from_txid_and_tuple(txid, v)
            else:
                self.tx_data[txid] = v

    @property
    def lid(self):
        '''Light uuid used for logging, to lighten output for reading'''
        return self.uuid[:8] if self.uuid else self.uuid

    def _as_dict(self):
        '''return dict with keys from __slots__ and corresponding values'''
        tx_data = {}  # copy
        for v in self.tx_data.values():
            tx_data.update(v._as_dict())
        return {
            'uuid': self.uuid,
            'completed': self.completed,
            'tx_data': tx_data,
            'tx_order': self.tx_order[:],  # copy
        }

    @classmethod
    def _from_dict(cls, data_dict):
        return cls(**data_dict)

    def __eq__(self, other):
        if type(other) != PSTxWorkflow:
            return False
        elif id(self) == id(other):
            return True
        elif self.uuid != other.uuid:
            return False
        elif self.completed != other.completed:
            return False
        elif self.tx_order != other.tx_order:
            return False
        elif set(self.tx_data.keys()) != set(other.tx_data.keys()):
            return False
        for k in self.tx_data.keys():
            if self.tx_data[k] != other.tx_data[k]:
                return False
        else:
            return True

    def next_to_send(self, wallet):
        '''Determine which tx should be broadcasted'''
        for txid in self.tx_order:
            tx_data = self.tx_data[txid]
            if not tx_data.sent and wallet.is_local_tx(txid):
                return tx_data

    def add_tx(self, **kwargs):
        '''Add tx to workflow'''
        txid = kwargs.pop('txid')
        raw_tx = kwargs.pop('raw_tx', None)
        tx_type = kwargs.pop('tx_type')
        if not txid or not tx_type:
            return
        tx_data = PSTxData(uuid=self.uuid, txid=txid,
                           raw_tx=raw_tx, tx_type=tx_type)
        self.tx_data[txid] = tx_data
        self.tx_order.append(txid)
        return tx_data

    def pop_tx(self, txid):
        '''Pop tx from workflow'''
        if txid in self.tx_data:
            res = self.tx_data.pop(txid)
        else:
            res = None
        self.tx_order = [tid for tid in self.tx_order if tid != txid]
        return res


class PSDenominateWorkflow:
    '''
    uuid: unique id for spending denoms reservation
    denom: workflow denom value
    rounds: workflow inputs mix rounds (legacy field, not used)
    inputs: list of spending denoms outpoints
    outputs: list of reserved output addresses
    completed: time when dsc message received
    '''

    __slots__ = 'uuid denom rounds inputs outputs completed'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.denom = kwargs.pop('denom', 0)
        self.rounds = kwargs.pop('rounds', 0)
        self.inputs = kwargs.pop('inputs', [])[:]  # copy
        self.outputs = kwargs.pop('outputs', [])[:]  # copy
        self.completed = kwargs.pop('completed', 0)

    @property
    def lid(self):
        '''Light uuid used for logging, to lighten output for reading'''
        return self.uuid[:8] if self.uuid else self.uuid

    def _as_dict(self):
        '''return dict uuid -> (denom, rounds, inputs, outputs, completed)'''
        return {
            self.uuid: (
                self.denom,
                self.rounds,
                self.inputs[:],  # copy
                self.outputs[:],  # copy
                self.completed,
            )
        }

    @classmethod
    def _from_uuid_and_tuple(cls, uuid, data_tuple):
        '''New from uuid, (denom, rounds, inputs, outputs, completed) tuple'''
        denom, rounds, inputs, outputs, completed = data_tuple[:5]
        return cls(uuid=uuid, denom=denom, rounds=rounds,
                   inputs=inputs[:], outputs=outputs[:],  # copy
                   completed=completed)

    def __eq__(self, other):
        if type(other) != PSDenominateWorkflow:
            return False
        elif id(self) == id(other):
            return True
        return not any(getattr(self, field) != getattr(other, field)
                       for field in self.__slots__)


class PSLogSubCat(IntEnum):
    '''Logging subcategories to colorize GUI logs'''
    NoCategory = 0
    WflOk = 1
    WflErr = 2
    WflDone = 3


class PSManLogAdapter(logging.LoggerAdapter):
    '''Logging adapter with subcategories to colorize GUI logs'''

    def __init__(self, logger, extra):
        super(PSManLogAdapter, self).__init__(logger, extra)

    def process(self, msg, kwargs):
        msg, kwargs = super(PSManLogAdapter, self).process(msg, kwargs)
        subcat = kwargs.pop('subcat', None)
        if subcat:
            kwargs['extra']['subcat'] = subcat
        else:
            kwargs['extra']['subcat'] = PSLogSubCat.NoCategory
        return msg, kwargs

    def wfl_done(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflDone)

    def wfl_ok(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflOk)

    def wfl_err(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflErr)


class PSGUILogHandler(logging.Handler):
    '''Write log to maxsize limited queue'''

    def __init__(self, psman):
        super(PSGUILogHandler, self).__init__()
        self.shortcut = psman.LOGGING_SHORTCUT
        self.psman = psman
        self.psman_id = id(psman)
        self.head = 0
        self.tail = 0
        self.log = dict()
        self.setLevel(logging.INFO)
        psman.logger.addHandler(self)
        self.notify = False

    def handle(self, record):
        if record.psman_id != self.psman_id:
            return False
        self.log[self.tail] = record
        self.tail += 1
        if self.tail - self.head > 1000:
            self.clear_log(100)
        if self.notify:
            self.psman.postpone_notification('ps-log-changes', self.psman)
        return True

    def clear_log(self, count=0):
        head = self.head
        if not count:
            count = self.tail - head
        for i in range(head, head+count):
            self.log.pop(i, None)
        self.head = head + count
        if self.notify:
            self.psman.postpone_notification('ps-log-changes', self.psman)


@attr.s
class DSMsgStat():
    '''Outgoing ds message statistics'''
    msg_sent = attr.ib(type=float, default=0.0)
    sent_cnt = attr.ib(type=int, default=0)
    dssu_cnt = attr.ib(type=int, default=0)
    success_cnt = attr.ib(type=int, default=0)
    timeout_cnt = attr.ib(type=int, default=0)
    peer_closed_cnt = attr.ib(type=int, default=0)
    error_cnt = attr.ib(type=int, default=0)
    total_wait_sec = attr.ib(type=float, default=0)
    min_wait_sec = attr.ib(type=float, default=1e9)
    max_wait_sec = attr.ib(type=float, default=0.0)

    def __str__(self):
        min_wait_sec = 0.0 if self.min_wait_sec == 1e9 else self.min_wait_sec
        success_cnt = self.success_cnt
        avg_wait_sec = self.total_wait_sec / success_cnt if success_cnt else .0
        min_wait = round(min_wait_sec, 1)
        avg_wait = round(avg_wait_sec, 1)
        max_wait = round(self.max_wait_sec, 1)
        return (f'all={self.sent_cnt},'
                f' ok={self.success_cnt},'
                f' err={self.error_cnt},'
                f' timeout={self.timeout_cnt},'
                f' closed={self.peer_closed_cnt},'
                f' dssu={self.dssu_cnt},'
                f' min/avg/max={min_wait}/{avg_wait}/{max_wait}sec')

    def send_msg(self):
        '''Called before sending outgoing ds messages'''
        self.sent_cnt += 1
        self.msg_sent = time.time()

    def on_dssu(self):
        '''Called on dssu arrival before next mixing worflow message'''
        self.dssu_cnt += 1

    def on_read_msg(self):
        '''Called on arrival of next mixing worflow message'''
        wait_sec = time.time() - self.msg_sent
        self.min_wait_sec = min(self.min_wait_sec, wait_sec)
        self.total_wait_sec += wait_sec
        self.max_wait_sec = max(self.max_wait_sec, wait_sec)
        self.success_cnt += 1

    def on_timeout(self):
        '''Called if MixSessionTimeout encountered'''
        self.timeout_cnt += 1

    def on_peer_closed(self):
        '''Called if MixSessionPeerClosed encountered'''
        self.peer_closed_cnt += 1

    def on_error(self):
        '''Called if Exception encountered'''
        self.error_cnt += 1


@attr.s
class MixingStats():
    '''Outgoing ds messages statistics grouped together'''
    dsa = attr.ib(type=DSMsgStat, default=attr.Factory(DSMsgStat))
    dsi = attr.ib(type=DSMsgStat, default=attr.Factory(DSMsgStat))
    dss = attr.ib(type=DSMsgStat, default=attr.Factory(DSMsgStat))

    def __str__(self):
        return (f'Mixing sessions statistics:'
                f'\ndsa: {str(self.dsa)}'
                f'\ndsi: {str(self.dsi)}'
                f'\ndss: {str(self.dss)}')

    def __repr__(self):
        return (f'MixingStats(dsa={self.dsa}, dsi={self.dsi}, dss={self.dss}')

    def get_last_sent_msg_stat(self):
        '''Get last one sent message from dsa/dsi/dss set'''
        last_sent = 0
        last_sent_msg = None
        for msg in [self.dsa, self.dsi, self.dss]:
            if msg.msg_sent > last_sent:
                last_sent = msg.msg_sent
                last_sent_msg = msg
        return last_sent_msg

    def on_timeout(self):
        '''Called if MixSessionTimeout encountered'''
        last_sent_msg = self.get_last_sent_msg_stat()
        if last_sent_msg:
            last_sent_msg.on_timeout()

    def on_peer_closed(self):
        '''Called if MixSessionPeerClosed encountered'''
        last_sent_msg = self.get_last_sent_msg_stat()
        if last_sent_msg:
            last_sent_msg.on_peer_closed()

    def on_error(self):
        '''Called if Exception encountered'''
        last_sent_msg = self.get_last_sent_msg_stat()
        if last_sent_msg:
            last_sent_msg.on_error()


class PSOptsMixin:
    '''PrivateSend user options functionality'''

    DEFAULT_KEEP_AMOUNT = 2
    MIN_KEEP_AMOUNT = 2
    MAX_KEEP_AMOUNT = 21000000

    DEFAULT_MIX_ROUNDS = 4
    MIN_MIX_ROUNDS = 2
    MAX_MIX_ROUNDS = 16
    MAX_MIX_ROUNDS_TESTNET = 256

    DEFAULT_PRIVATESEND_SESSIONS = 4    # Number of concurrent mixing sessions
    MIN_PRIVATESEND_SESSIONS = 1
    MAX_PRIVATESEND_SESSIONS = 10

    DEFAULT_GROUP_HISTORY = True        # Group txs in history views
    DEFAULT_NOTIFY_PS_TXS = False       # GUI notify on PS txs arrival
    DEFAULT_SUBSCRIBE_SPENT = False     # on server subscribe to spent ps addrs
    DEFAULT_ALLOW_OTHERS = False        # Allow spend other ps coins as regular

    POOL_MIN_PARTICIPANTS = 3           # mixing pool participants
    POOL_MIN_PARTICIPANTS_TESTNET = 2
    POOL_MAX_PARTICIPANTS = 20

    # Keypairs cleanup timeout when mixing is stopped
    DEFAULT_KP_TIMEOUT = 0
    MIN_KP_TIMEOUT = 0
    MAX_KP_TIMEOUT = 5

    WAIT_FOR_MN_TXS_TIME_SEC = 120  # await tx from MN (collateral, denominate)

    MIN_NEW_DENOMS_DELAY = 30       # minimum delay betweeen new denoms txs
    MAX_NEW_DENOMS_DELAY = 300      # maximum delay betweeen new denoms txs

    CALC_DENOMS_METHOD_STR = [
        _('Use default denoms count'),
        _('Use absolute denoms count'),
    ]

    class CalcDenomsMethod(IntEnumWithCheck):
        '''Method of need denoms count calculation'''
        DEF = 0  # use keep amount to calc need denoms
        ABS = 1  # use absolute denoms count set from UI

    def __init__(self, wallet):
        self._allow_others = self.DEFAULT_ALLOW_OTHERS
        self.mix_stat = MixingStats()

    @property
    def keep_amount(self):
        '''Get current keep amount, on reaching which mixing is stopped'''
        if self.calc_denoms_method != self.CalcDenomsMethod.ABS:
            return self.wallet.db.get_ps_data('keep_amount',
                                              self.DEFAULT_KEEP_AMOUNT)
        return sum(v * self.abs_denoms_cnt[v] for v in PS_DENOMS_VALS)/COIN

    @keep_amount.setter
    def keep_amount(self, amount):
        '''Set keep amount'''
        if self.state in self.mixing_running_states:
            return
        if self.calc_denoms_method == self.CalcDenomsMethod.ABS:
            return
        if self.keep_amount == amount:
            return
        amount = max(self.min_keep_amount, int(amount))
        amount = min(self.max_keep_amount, int(amount))
        self.wallet.db.set_ps_data('keep_amount', amount)

    @property
    def min_keep_amount(self):
        '''Minimal possible keep_amount'''
        return self.MIN_KEEP_AMOUNT

    @property
    def max_keep_amount(self):
        '''Maximal possible keep_amount'''
        return self.MAX_KEEP_AMOUNT

    def keep_amount_data(self, full_txt=False):
        '''Str data for UI keep_amount preference'''
        if full_txt:
            return _('This amount acts as a threshold to turn off'
                     " PrivateSend mixing once it's reached.")
        else:
            return _('Amount of Dash to keep anonymized')

    @property
    def mix_rounds(self):
        '''Get current mix rounds for denoms'''
        return self.wallet.db.get_ps_data('mix_rounds',
                                          self.DEFAULT_MIX_ROUNDS)

    @mix_rounds.setter
    def mix_rounds(self, rounds):
        '''Set need mix rounds for denoms'''
        if self.state in self.mixing_running_states:
            return
        if self.mix_rounds == rounds:
            return
        rounds = max(self.min_mix_rounds, int(rounds))
        rounds = min(self.max_mix_rounds, int(rounds))
        self.wallet.db.set_ps_data('mix_rounds', rounds)
        with self.denoms_lock:
            self._denoms_to_mix_cache = self.denoms_to_mix()

    @property
    def min_mix_rounds(self):
        '''Minimal possible mix_rounds'''
        return self.MIN_MIX_ROUNDS

    @property
    def max_mix_rounds(self):
        '''Maximal possible mix_rounds'''
        if constants.net.TESTNET:
            return self.MAX_MIX_ROUNDS_TESTNET
        else:
            return self.MAX_MIX_ROUNDS

    @property
    def pool_min_participants(self):
        '''Minimal possible mixing pool participants'''
        if constants.net.TESTNET:
            return self.POOL_MIN_PARTICIPANTS_TESTNET
        else:
            return self.POOL_MIN_PARTICIPANTS

    @property
    def pool_max_participants(self):
        '''Maximal possible mixing pool participants'''
        return self.POOL_MAX_PARTICIPANTS

    def mix_rounds_data(self, full_txt=False):
        '''Str data for UI mix_rounds preference'''
        if full_txt:
            return _('This setting determines the amount of individual'
                     ' masternodes that an input will be anonymized through.'
                     ' More rounds of anonymization gives a higher degree'
                     ' of privacy, but also costs more in fees.')
        else:
            return _('PrivateSend rounds to use')

    def create_sm_denoms_data(self, full_txt=False, enough_txt=False,
                              no_denoms_txt=False, confirm_txt=False):
        '''Str data for UI create small denoms functionality'''

        confirm_str_end = _('Do you want to create small denoms from one big'
                            ' denom utxo? No change value will be created'
                            ' for privacy reasons.')
        if full_txt:
            return _('Create small denominations from big one')
        elif enough_txt:
            return '%s %s' % (_('There is enough small denoms.'),
                              confirm_str_end)
        elif confirm_txt:
            return '%s %s' % (_('There is not enough small denoms to make'
                                ' PrivateSend transactions with reasonable'
                                ' fees.'),
                              confirm_str_end)
        elif no_denoms_txt:
            return _('There is no denoms to create small denoms from big one.')
        else:
            return _('Create small denominations')

    @property
    def group_history(self):
        '''Check if PS txs is grouped in tx history'''
        if self.unsupported:
            return False
        return self.wallet.db.get_ps_data('group_history',
                                          self.DEFAULT_GROUP_HISTORY)

    @group_history.setter
    def group_history(self, group_history):
        '''Set if PS txs should be grouped in tx history'''
        if self.group_history == group_history:
            return
        self.wallet.db.set_ps_data('group_history', bool(group_history))

    def group_history_data(self, full_txt=False):
        '''Str data for UI group_history preference'''
        if full_txt:
            return _('Group PrivateSend mixing transactions in wallet history')
        else:
            return _('Group PrivateSend transactions')

    @property
    def notify_ps_txs(self):
        '''Check if arrival of new PS txs is notified in UI'''
        return self.wallet.db.get_ps_data('notify_ps_txs',
                                          self.DEFAULT_NOTIFY_PS_TXS)

    @notify_ps_txs.setter
    def notify_ps_txs(self, notify_ps_txs):
        '''Set if arrival of new PS txs should be notified in UI'''
        if self.notify_ps_txs == notify_ps_txs:
            return
        self.wallet.db.set_ps_data('notify_ps_txs', bool(notify_ps_txs))

    def notify_ps_txs_data(self, full_txt=False):
        '''Str data for UI notify_ps_txs preference'''
        if full_txt:
            return _('Notify when PrivateSend mixing transactions'
                     ' have arrived')
        else:
            return _('Notify on PrivateSend transactions')

    def need_notify(self, txid):
        '''Check if new tx with txid should be notified in UI'''
        if self.notify_ps_txs:
            return True
        tx_type, completed = self.wallet.db.get_ps_tx(txid)
        if tx_type not in PS_MIXING_TX_TYPES:
            return True
        else:
            return False

    @property
    def max_sessions(self):
        '''Get maximal possible concurrent mixing sessions'''
        return self.wallet.db.get_ps_data('max_sessions',
                                          self.DEFAULT_PRIVATESEND_SESSIONS)

    @max_sessions.setter
    def max_sessions(self, max_sessions):
        '''Set maximal possible concurrent mixing sessions'''
        if self.max_sessions == max_sessions:
            return
        self.wallet.db.set_ps_data('max_sessions', int(max_sessions))

    @property
    def min_max_sessions(self):
        '''Minimal possible mix_sessions'''
        return self.MIN_PRIVATESEND_SESSIONS

    @property
    def max_max_sessions(self):
        '''Maximal possible mix_sessions'''
        return self.MAX_PRIVATESEND_SESSIONS

    def max_sessions_data(self, full_txt=False):
        '''Str data for UI max_sessions preference'''
        if full_txt:
            return _('Count of PrivateSend mixing session')
        else:
            return _('PrivateSend sessions')

    @property
    def kp_timeout(self):
        '''Get timeout for keypairs cleaning after mixing stopped'''
        return self.wallet.db.get_ps_data('kp_timeout',
                                          self.DEFAULT_KP_TIMEOUT)

    @kp_timeout.setter
    def kp_timeout(self, kp_timeout):
        '''Set timeout for keypairs cleaning after mixing stopped'''
        if self.kp_timeout == kp_timeout:
            return
        kp_timeout = min(int(kp_timeout), self.MAX_KP_TIMEOUT)
        kp_timeout = max(kp_timeout, self.MIN_KP_TIMEOUT)
        self.wallet.db.set_ps_data('kp_timeout', kp_timeout)

    @property
    def min_kp_timeout(self):
        '''Minimal possible kp_timeout'''
        return self.MIN_KP_TIMEOUT

    @property
    def max_kp_timeout(self):
        '''Maximal possible kp_timeout'''
        return self.MAX_KP_TIMEOUT

    def kp_timeout_data(self, full_txt=False):
        '''Str data for UI kp_timeout preference'''
        if full_txt:
            return _('Time in minutes to keep keypairs after mixing stopped.'
                     ' Keypairs is cached before mixing starts on wallets with'
                     ' encrypted keystore.')
        else:
            return _('Keypairs cache timeout')

    @property
    def subscribe_spent(self):
        '''Check if on server subscriptions for spent PS addresses done'''
        return self.wallet.db.get_ps_data('subscribe_spent',
                                          self.DEFAULT_SUBSCRIBE_SPENT)

    @subscribe_spent.setter
    def subscribe_spent(self, subscribe_spent):
        '''Set if on server subscriptions for spent PS addresses done'''
        if self.subscribe_spent == subscribe_spent:
            return
        self.wallet.db.set_ps_data('subscribe_spent', bool(subscribe_spent))
        w = self.wallet
        if subscribe_spent:
            for addr in self.spent_addrs:
                self.subscribe_spent_addr(addr)
        else:
            for addr in self.spent_addrs:
                hist = w.db.get_addr_history(addr)
                self.unsubscribe_spent_addr(addr, hist)

    def subscribe_spent_data(self, full_txt=False):
        '''Str data for UI subscribe_spent preference'''
        if full_txt:
            return _('Subscribe to spent PS addresses'
                     ' on electrum servers')
        else:
            return _('Subscribe to spent PS addresses')

    @property
    def allow_others(self):
        '''Check if other PS coins allowed to spend in regular txs'''
        return self._allow_others

    @allow_others.setter
    def allow_others(self, allow_others):
        '''Set if other PS coins allowed to spend in regular txs'''
        if self._allow_others == allow_others:
            return
        self._allow_others = allow_others

    def allow_others_data(self, full_txt=False,
                          qt_question=False, kv_question=False):
        '''Str data for UI allow_others preference'''

        expl = _('Other PS coins appears if some transaction other than'
                 ' mixing PrivateSend transactions types send funds to one'
                 ' of addresses used for PrivateSend mixing.\n\nIt is not'
                 ' recommended for privacy reasons to spend these funds'
                 ' in regular way. However, you can mix these funds manually'
                 ' with PrivateSend mixing process.')

        expl2_qt = _('You can create new denoms or new collateral from other'
                     ' PS coins on Coins tab. You can also select individual'
                     ' coin to spend and return to originating address.')

        expl2_kv = _('You can create new denoms or new collateral from other'
                     ' PS coins with Coins dialog from PrivateSend options.')

        q = _('This option allow spend other PS coins as a regular coins'
              ' without coins selection.'
              ' Are you sure to enable this option?')
        if full_txt:
            return _('Allow spend other PS coins in regular transactions')
        elif qt_question:
            return '%s\n\n%s\n\n%s' % (expl, expl2_qt, q)
        elif kv_question:
            return '%s\n\n%s\n\n%s' % (expl, expl2_kv, q)
        else:
            return _('Allow spend other PS coins')

    @property
    def group_origin_coins_by_addr(self):
        '''Check if inputs is grouped by address for new denoms/collateral'''
        return self.wallet.db.get_ps_data('group_origin_coins_by_addr', False)

    @group_origin_coins_by_addr.setter
    def group_origin_coins_by_addr(self, group):
        '''Set if inputs is grouped by address for new denoms/collateral'''
        self.wallet.db.set_ps_data('group_origin_coins_by_addr', bool(group))

    def group_origin_coins_by_addr_data(self, full_txt=False):
        '''Str data for UI group_origin_coins_by_addr preference'''
        if full_txt:
            return _('In new mix transactions group origin coins by address')
        else:
            return _('Group origin coins by address')

    @property
    def gather_mix_stat(self):
        '''Check if mixing sessions statistics should be gathered'''
        if self.unsupported:
            return False
        return self.wallet.db.get_ps_data('gather_mix_stat', False)

    @gather_mix_stat.setter
    def gather_mix_stat(self, gather_mix_stat):
        '''Change gathering mixing statistics option'''
        if self.state in self.mixing_running_states:
            return
        if self.gather_mix_stat == gather_mix_stat:
            return
        self.clear_mix_stat()
        self.wallet.db.set_ps_data('gather_mix_stat', bool(gather_mix_stat))

    def clear_mix_stat(self):
        '''Clear psman.mix_stat dict'''
        if self.state in self.mixing_running_states:
            return
        self.mix_stat = MixingStats()

    def gather_mix_stat_data(self, full_txt=False):
        '''Str data for UI gather_mix_stat preference'''
        if full_txt:
            return _('Gather mixing sessions statistics (Reset on toggle)')
        else:
            return _('Gather mixing statistics')

    def mixing_control_data(self, full_txt=False):
        '''Str data for UI mixing control'''
        if full_txt:
            return _('Control PrivateSend mixing process')
        else:
            if self.state == PSStates.Ready:
                return _('Start Mixing')
            elif self.state == PSStates.Mixing:
                return _('Stop Mixing')
            elif self.state == PSStates.StartMixing:
                return _('Starting Mixing ...')
            elif self.state == PSStates.StopMixing:
                return _('Stopping Mixing ...')
            elif self.state == PSStates.FindingUntracked:
                return _('Finding PS Data ...')
            elif self.state == PSStates.Disabled:
                return _('Enable PrivateSend')
            elif self.state == PSStates.Initializing:
                return _('Initializing ...')
            elif self.state == PSStates.Cleaning:
                return _('Cleaning PS Data ...')
            else:
                return _('Check Log For Errors')

    @property
    def last_mix_start_time(self):
        '''Get last mixing starting time'''
        return self.wallet.db.get_ps_data('last_mix_start_time', 0)  # Jan 1970

    @last_mix_start_time.setter
    def last_mix_start_time(self, time):
        '''Set last mixing starting time'''
        self.wallet.db.set_ps_data('last_mix_start_time', time)

    @property
    def last_mix_stop_time(self):
        '''Get last mixing stopped time'''
        return self.wallet.db.get_ps_data('last_mix_stop_time', 0)  # Jan 1970

    @last_mix_stop_time.setter
    def last_mix_stop_time(self, time):
        '''Set last mixing stopped time'''
        self.wallet.db.set_ps_data('last_mix_stop_time', time)

    @property
    def last_denoms_tx_time(self):
        '''Get time when last new denoms tx was created'''
        return self.wallet.db.get_ps_data('last_denoms_tx_time', 0)  # Jan 1970

    @last_denoms_tx_time.setter
    def last_denoms_tx_time(self, time):
        '''Set time when last new denoms tx was created'''
        self.wallet.db.set_ps_data('last_denoms_tx_time', time)

    @property
    def last_mixed_tx_time(self):
        '''Get time when last denominate tx arrived'''
        return self.wallet.db.get_ps_data('last_mixed_tx_time', 0)  # Jan 1970

    @last_mixed_tx_time.setter
    def last_mixed_tx_time(self, time):
        '''Set time when last denominate tx arrived'''
        self.wallet.db.set_ps_data('last_mixed_tx_time', time)

    @property
    def wait_for_mn_txs_time(self):
        '''Get time to wait for denominate or pay collateral tx from MNs'''
        return self.WAIT_FOR_MN_TXS_TIME_SEC

    @property
    def mix_stop_secs_ago(self):
        '''Get time in secs from moment when mixing was stopped'''
        return round(time.time() - self.last_mix_stop_time)

    @property
    def mix_recently_run(self):
        '''Check mix recently run and denominate/pay collateral can arrive'''
        return self.mix_stop_secs_ago < self.wait_for_mn_txs_time

    @property
    def double_spend_warn(self):
        '''Str data for UI on possible double spending if mix recently run'''
        if self.state in self.mixing_running_states:
            wait_time = self.wait_for_mn_txs_time
            return _('PrivateSend mixing is currently run. To prevent'
                     ' double spending it is recommended to stop mixing'
                     ' and wait {} seconds before spending PrivateSend'
                     ' coins.'.format(wait_time))
        if self.mix_recently_run:
            wait_secs = self.wait_for_mn_txs_time - self.mix_stop_secs_ago
            if wait_secs > 0:
                return _('PrivateSend mixing is recently run. To prevent'
                         ' double spending It is recommended to wait'
                         ' {} seconds before spending PrivateSend'
                         ' coins.'.format(wait_secs))
        return ''

    def dn_balance_data(self, full_txt=False):
        '''Str for UI denominated balance data'''
        if full_txt:
            return _('Currently available denominated balance')
        else:
            return _('Denominated Balance')

    def ps_balance_data(self, full_txt=False):
        '''Str for UI PS balance data'''
        if full_txt:
            return _('Currently available anonymized balance')
        else:
            return _('PrivateSend Balance')

    @property
    def show_warn_electrumx(self):
        '''Check if warning about PS specific on electrum should be shown'''
        return self.wallet.db.get_ps_data('show_warn_electrumx', True)

    @show_warn_electrumx.setter
    def show_warn_electrumx(self, show):
        '''Set if warning about PS specific on electrum should be shown'''
        self.wallet.db.set_ps_data('show_warn_electrumx', show)

    def warn_electrumx_data(self, full_txt=False, help_txt=False):
        '''Str data for UI warning/preferences about PS specific on electrum'''
        if full_txt:
            return _('Privacy Warning: ElectrumX is a weak spot'
                     ' in PrivateSend privacy and knows all your'
                     ' wallet UTXO including PrivateSend mixed denoms.'
                     ' You should use trusted ElectrumX server'
                     ' for PrivateSend operation.')
        elif help_txt:
            return _('Show privacy warning about ElectrumX servers usage')
        else:
            return _('Privacy Warning ...')

    @property
    def show_warn_ps_ks(self):
        '''Check if warning on PS keystore left funds should be shown'''
        return self.wallet.db.get_ps_data('show_warn_ps_ks', True)

    @show_warn_ps_ks.setter
    def show_warn_ps_ks(self, show):
        '''Set if warning on PS keystore left funds should be shown'''
        self.wallet.db.set_ps_data('show_warn_ps_ks', show)

    def warn_ps_ks_data(self):
        '''Str data for UI warning on PS keystore left funds, for HW wallets'''
        return _('Show warning on exit if PS Keystore contain funds')

    def mixing_progress(self, count_on_rounds=None):
        '''Get mixing progress in percents'''
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return 0
        r = self.mix_rounds if count_on_rounds is None else count_on_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        if dn_balance == ps_balance:
            return 100
        res = 0
        for i in range(1, r+1):
            ri_balance = sum(w.get_balance(include_ps=False, min_rounds=i))
            res += ri_balance/dn_balance/r
        res = round(res*100)
        if res < 100:  # on small amount differences show 100 percents to early
            return res
        else:
            return 99

    def mixing_progress_data(self, full_txt=False):
        '''Str data for UI mixing progress'''
        if full_txt:
            return _('Mixing Progress in percents')
        else:
            return _('Mixing Progress')

    @property
    def calc_denoms_method(self):
        '''Get denoms calculation method'''
        return self.wallet.db.get_ps_data('calc_denoms_method',
                                          self.CalcDenomsMethod.DEF)

    @calc_denoms_method.setter
    def calc_denoms_method(self, method):
        '''Set denoms calculation method'''
        if self.state in self.mixing_running_states:
            return
        assert self.CalcDenomsMethod.has_value(method), 'wrong method'
        self.wallet.db.set_ps_data('calc_denoms_method', int(method))

    def calc_denoms_method_str(self, method):
        '''Get str repr of denoms calculation method'''
        assert self.CalcDenomsMethod.has_value(method), 'wrong method'
        return self.CALC_DENOMS_METHOD_STR[method]

    def calc_denoms_method_data(self, full_txt=False):
        '''Str data for UI on denoms calculation method'''
        if full_txt:
            return _('Denoms calculate method determines'
                     ' count of denoms created for mixing')
        else:
            return  _('Denoms calculate method')

    @property
    def abs_denoms_cnt(self):
        '''Get dict containing absolute denoms count by denom value key'''
        res = self.wallet.db.get_ps_data('abs_denoms_cnt', {})
        if res:
            return {v: res[str(v)] for v in PS_DENOMS_VALS}
        return {v: 0 for v in PS_DENOMS_VALS}

    @abs_denoms_cnt.setter
    def abs_denoms_cnt(self, abs_denoms_cnt):
        '''Set absolute denoms count dict: denom value => count'''
        if self.state in self.mixing_running_states:
            return
        assert type(abs_denoms_cnt) == dict, 'wrong type'
        assert set(abs_denoms_cnt.keys()) == set(PS_DENOMS_VALS), 'wrong keys'
        assert all([v >= 0 for v in abs_denoms_cnt.values()]), 'wrong values'
        self.wallet.db.set_ps_data('abs_denoms_cnt', abs_denoms_cnt)


class PSPossibleDoubleSpendError(Exception):
    """Thrown when trying to broadcast recently used ps denoms/collateral"""


class PSSpendToPSAddressesError(Exception):
    """Thrown when trying to broadcast tx with ps coins spent to ps addrs"""


class PSMinRoundsCheckFailed(Exception):
    """Thrown when check for coins minimum mixing rounds failed"""


class PSUtilsMixin:
    '''PrivateSend misc utils'''

    def __init__(self, wallet):
        # postponed notification sent by trigger_postponed_notifications
        self.postponed_notifications = {}

    def postpone_notification(self, event, *args):
        '''Postpone notification to send many analogous notifications as one'''
        self.postponed_notifications[event] = args

    async def trigger_postponed_notifications(self):
        '''Trigger postponed notification'''
        while True:
            await asyncio.sleep(0.5)
            if self.enabled:
                for event in list(self.postponed_notifications.keys()):
                    args = self.postponed_notifications.pop(event, None)
                    if args is not None:
                        util.trigger_callback(event, *args)

    async def broadcast_transaction(self, tx, *, timeout=None) -> None:
        '''Broadcast transaction with additional checks'''
        if self.enabled:
            w = self.wallet

            def check_spend_to_ps_addresses():
                for o in tx.outputs():
                    addr = o.address
                    if addr in w.db.get_ps_addresses():
                        msg = self.SPEND_TO_PS_ADDRS_MSG
                        raise PSSpendToPSAddressesError(msg)
            await self.loop.run_in_executor(None, check_spend_to_ps_addresses)

            def check_possible_dspend():
                with self.denoms_lock, self.collateral_lock:
                    warn = self.double_spend_warn
                    if not warn:
                        return
                    for txin in tx.inputs():
                        outpoint = txin.prevout.to_str()
                        if (w.db.get_ps_spending_collateral(outpoint)
                                or w.db.get_ps_spending_denom(outpoint)):
                            raise PSPossibleDoubleSpendError(warn)
            await self.loop.run_in_executor(None, check_possible_dspend)
        await self.network.broadcast_transaction(tx, timeout=timeout)

    def clear_ps_data(self):
        '''Clear all wallet.db PS data'''
        if self.loop:
            coro = self._clear_ps_data()
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _clear_ps_data(self):
        '''Async clear all wallet.db PS data'''
        w = self.wallet

        def _do_clear_ps_data():
            msg = None
            with self.state_lock:
                if self.state in self.mixing_running_states:
                    msg = _('To clear PrivateSend data'
                            ' stop PrivateSend mixing')
                elif self.state == PSStates.FindingUntracked:
                    msg = _('Can not clear PrivateSend data. Process'
                            ' of finding untracked PS transactions'
                            ' is currently run')
                elif self.state == PSStates.Cleaning:
                    return
                else:
                    self.state = PSStates.Cleaning
                    util.trigger_callback('ps-state-changes', w, None, None)
                    self.logger.info('Clearing PrivateSend wallet data')
                    w.db.clear_ps_data()
                    self.ps_keystore_has_history = False
                    self.state = PSStates.Ready
                    self.logger.info('All PrivateSend wallet data cleared')
            return msg
        msg = await self.loop.run_in_executor(None, _do_clear_ps_data)
        if msg:
            util.trigger_callback('ps-state-changes', w, msg, None)
        else:
            util.trigger_callback('ps-state-changes', w, None, None)
            self.postpone_notification('ps-data-changes', w)
            w.save_db()

    def check_min_rounds(self, coins, min_rounds):
        '''Check all coins have minimum rounds mixed'''
        for c in coins:
            ps_rounds = c.ps_rounds
            if ps_rounds is None or ps_rounds < min_rounds:
                raise PSMinRoundsCheckFailed(f'Check for mininum {min_rounds}'
                                             f' PrivateSend mixing rounds'
                                             f' failed')

    def check_enough_sm_denoms(self, denoms_by_values):
        '''Check is enough small denoms in denominated coins'''
        if not denoms_by_values:
            return False
        for dval in PS_DENOMS_VALS[:-1]:
            if denoms_by_values[dval] < denoms_by_values[dval*10]:
                return False
        return True

    def check_big_denoms_presented(self, denoms_by_values):
        '''Check if non minimal denoms is presented'''
        if not denoms_by_values:
            return False
        for dval in PS_DENOMS_VALS[1:]:
            if denoms_by_values[dval] > 0:
                return True
        return False

    def get_biggest_denoms_by_min_round(self):
        '''Select non minimal denoms sorted by minimum rounds/maximum value'''
        w = self.wallet
        coins = w.get_utxos(None,
                            mature_only=True, confirmed_funding_only=True,
                            consider_islocks=True, min_rounds=0)
        coins = [c for c in coins if c.value_sats() > MIN_DENOM_VAL]
        coins = self.filter_out_hw_ks_coins(coins)
        return sorted(coins, key=lambda x: (x.ps_rounds, -x.value_sats()))

    def check_protx_info_completeness(self):
        '''Check ProTx diffs data is ready and mixing can be run'''
        if not self.network:
            return False
        mn_list = self.network.mn_list
        if mn_list.protx_info_completeness < 0.75:
            return False
        else:
            return True

    def check_llmq_ready(self):
        '''Check LLMQ data is ready and mixing can be run'''
        if not self.network:
            return False
        mn_list = self.network.mn_list
        return mn_list.llmq_ready

    def find_untracked_ps_txs_from_gui(self):
        '''Run find untracked transactions functionality from UI'''
        if self.loop:
            coro = self.find_untracked_ps_txs()
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def find_untracked_ps_txs(self, log=True):
        '''Async run find untracked transactions functionality'''
        w = self.wallet
        found = 0
        with self.state_lock:
            if self.state in [PSStates.Ready, PSStates.Initializing]:
                self.state = PSStates.FindingUntracked
        if not self.state == PSStates.FindingUntracked:
            return found
        else:
            util.trigger_callback('ps-state-changes', w, None, None)
        try:
            logged_awaiting = False
            while not self.can_find_untracked():
                if not logged_awaiting:
                    logged_awaiting = True
                    self.logger.info('awaiting wallet sync')
                await asyncio.sleep(1)
            if logged_awaiting:
                self.logger.info('wallet synced')
            _find = self._find_untracked_ps_txs
            found = await self.loop.run_in_executor(None, _find, log)
            if found:
                w.save_db()
                self.postpone_notification('ps-data-changes', w)
        except Exception as e:
            with self.state_lock:
                self.state = PSStates.Errored
            self.logger.info(f'Error during loading of untracked'
                             f' PS transactions: {str(e)}')
        finally:
            _find_uncompleted = self._fix_uncompleted_ps_txs
            await self.loop.run_in_executor(None, _find_uncompleted)
            with self.state_lock:
                if self.state != PSStates.Errored:
                    self.state = PSStates.Ready
            util.trigger_callback('ps-state-changes', w, None, None)
        return found

    def _fix_uncompleted_ps_txs(self):
        '''Try to fix uncompleted PS data if adding it previously is failed'''
        w = self.wallet
        ps_txs = w.db.get_ps_txs()
        ps_txs_removed = w.db.get_ps_txs_removed()
        found = 0
        failed = 0
        for txid, (tx_type, completed) in ps_txs.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'_fix_uncompleted_ps_txs:'
                                     f' add {txid} ps data')
                    self._add_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_add_ps_data {txid} failed: {str(e)}'
                    failed += 1
                    self.logger.info(str_err)
        for txid, (tx_type, completed) in ps_txs_removed.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'_fix_uncompleted_ps_txs:'
                                     f' rm {txid} ps data')
                    self._rm_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_rm_ps_data {txid} failed: {str(e)}'
                    failed += 1
                    self.logger.info(str_err)
        if failed != 0:
            with self.state_lock:
                self.state = PSStates.Errored
        if found:
            self.postpone_notification('ps-data-changes', w)

    def _get_simplified_history(self):
        '''Get light version of tx history for find untracked functionality'''
        w = self.wallet
        history = []
        for txid in w.db.list_transactions():
            tx = w.db.get_transaction(txid)
            tx_type, completed = w.db.get_ps_tx(txid)
            islock = w.db.get_islock(txid)
            if islock:
                tx_mined_status = w.get_tx_height(txid)
                islock_sort = txid if not tx_mined_status.conf else ''
            else:
                islock_sort = ''
            history.append((txid, tx, tx_type, islock, islock_sort))
        history.sort(key=lambda x: (w.get_txpos(x[0], x[3]), x[4]))
        return history

    @profiler
    def _find_untracked_ps_txs(self, log):
        '''Run find untracked txs functionality'''
        if log:
            self.logger.info('Finding untracked PrivateSend transactions')
        history = self._get_simplified_history()
        all_detected_txs = set()
        found = 0
        while True:
            detected_txs = set()
            not_detected_parents = set()
            for txid, tx, tx_type, islock, islock_sort in history:
                if tx_type or txid in all_detected_txs:  # already found
                    continue
                if not self.ps_keystore_has_history:
                    ps_ks_addrs = self.wallet.psman.get_addresses()
                    for o in tx.outputs():
                        if o.address in ps_ks_addrs:
                            self.ps_keystore_has_history = True
                            break
                tx_type = self._check_ps_tx_type(txid, tx, find_untracked=True)
                if tx_type:
                    self._add_ps_data(txid, tx, tx_type)
                    type_name = SPEC_TX_NAMES[tx_type]
                    if log:
                        self.logger.info(f'Found {type_name} {txid}')
                    found += 1
                    detected_txs.add(txid)
                else:
                    parents = set([i.prevout.txid.hex() for i in tx.inputs()])
                    not_detected_parents |= parents
            all_detected_txs |= detected_txs
            if not detected_txs & not_detected_parents:
                break
        # last iteration to detect PS Other Coins not found before other ps txs
        for txid, tx, tx_type, islock, islock_sort in history:
            if tx_type or txid in all_detected_txs:  # already found
                continue
            tx_type = self._check_ps_tx_type(txid, tx, find_untracked=True,
                                             last_iteration=True)
            if tx_type:
                self._add_ps_data(txid, tx, tx_type)
                type_name = SPEC_TX_NAMES[tx_type]
                if log:
                    self.logger.info(f'Found {type_name} {txid}')
                found += 1
        if not found and log:
            self.logger.info('No untracked PrivateSend transactions found')
        return found

    def prob_denominate_tx_coin(self, c, check_inputs_vals=False):
        '''Try to detect possible outputs of denominate tx
        to send PS transactions on HW wallet with no PS Keystore/PS data'''
        w = self.wallet
        val = c.value_sats()
        if val not in PS_DENOMS_VALS:
            return

        prev_txid = c.prevout.txid.hex()
        prev_tx = w.db.get_transaction(prev_txid)
        if not prev_tx:
            return

        inputs = prev_tx.inputs()
        outputs = prev_tx.outputs()
        inputs_cnt = len(inputs)
        outputs_cnt = len(outputs)
        if inputs_cnt != outputs_cnt:
            return

        dval_outputs_cnt = 0
        mine_outputs_cnt = 0
        for o in outputs:
            if o.value != val:
                break
            dval_outputs_cnt += 1
            mine_outputs_cnt += 1 if w.is_mine(o.address) else 0
        if dval_outputs_cnt != outputs_cnt:
            return
        if mine_outputs_cnt == outputs_cnt:
            return

        if not check_inputs_vals:
            return True

        dval_inputs_cnt = 0
        for prev_txin in prev_tx.inputs():
            is_denominate_input = False
            try:
                prev_txin_txid = prev_txin.prevout.txid.hex()
                prev_txin_tx = w.get_input_tx(prev_txin_txid)
                if not prev_txin_tx:
                    return
                prev_txin_tx_outputs = prev_txin_tx.outputs()
                prev_txin_tx_outputs_cnt = len(prev_txin_tx_outputs)
                prev_txin_tx_dval_out_cnt = 0
                for o in prev_txin_tx_outputs:
                    if o.value == val:
                        prev_txin_tx_dval_out_cnt +=1
                if (prev_txin_tx_outputs_cnt == prev_txin_tx_dval_out_cnt):
                    is_denominate_input = True
            except:
                continue
            if is_denominate_input:
                dval_inputs_cnt += 1
        if dval_inputs_cnt != inputs_cnt:
            return
        return True

    def find_common_ancestor(self, utxo_a, utxo_b, search_depth=5):
        '''Try find common ancestor of utxo_a, utxo_b'''
        w = self.wallet
        min_common_depth = 1e9
        cur_depth = 0
        cur_utxos_a = [(utxo_a, ())]
        cur_utxos_b = [(utxo_b, ())]
        txids_a = {}
        txids_b = {}
        while cur_depth <= search_depth:
            next_utxos_a = []
            for utxo, path in cur_utxos_a:
                txid = utxo.prevout.txid.hex()
                txid_path = path + (txid, )
                txids_a[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        addr = w.get_txin_address(txin)
                        if addr and w.is_mine(addr):
                            next_utxos_a.append((txin, txid_path))
            cur_utxos_a = next_utxos_a[:]

            next_utxos_b = []
            for utxo, path in cur_utxos_b:
                txid = utxo.prevout.txid.hex()
                txid_path = path + (txid, )
                txids_b[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        addr = w.get_txin_address(txin)
                        if addr and w.is_mine(addr):
                            next_utxos_b.append((txin, txid_path))
            cur_utxos_b = next_utxos_b[:]

            common_txids = set(txids_a).intersection(txids_b)
            if common_txids:
                res = {'paths_a': [], 'paths_b': []}
                for txid in common_txids:
                    path_a = txids_a[txid]
                    path_b = txids_b[txid]
                    min_common_depth = min(min_common_depth, len(path_a) - 1)
                    min_common_depth = min(min_common_depth, len(path_b) - 1)
                    res['paths_a'].append(path_a)
                    res['paths_b'].append(path_b)
                res['min_common_depth'] = min_common_depth
                return res

            cur_utxos_a = next_utxos_a[:]
            cur_depth += 1
