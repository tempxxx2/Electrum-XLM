# -*- coding: utf-8 -*-

import asyncio
import copy
import random
import threading
import time
from collections import Counter
from enum import IntEnum
from math import floor, ceil

from .bip32 import convert_bip32_intpath_to_strpath
from .bitcoin import pubkey_to_address
from .dash_tx import STANDARD_TX, PSTxTypes, SPEC_TX_NAMES
from .dash_msg import PRIVATESEND_ENTRY_MAX_SIZE
from .dash_ps_util import (PSTxWorkflow, PSDenominateWorkflow, PSStates,
                           PS_DENOMS_VALS, COLLATERAL_VAL, MIN_DENOM_VAL,
                           CREATE_COLLATERAL_VAL, CREATE_COLLATERAL_VALS,
                           PSCoinRounds, to_duffs, PS_VALS, PS_SAVED_TX_TYPES,
                           calc_tx_fee)
from .i18n import _
from .invoices import PR_EXPIRED
from .keystore import load_keystore, from_seed
from .transaction import PartialTxOutput
from .util import NotEnoughFunds, InvalidPassword, NoDynamicFeeEstimates


MAX_COLLATERAL_VAL = CREATE_COLLATERAL_VALS[-1]

# Keypairs cache types
KP_INCOMING = 'incoming'            # future incoming funds on main keystore
KP_SPENDABLE = 'spendable'          # regular utxos
KP_PS_SPENDABLE = 'ps_spendable'    # ps_denoms/ps_collateral utxos
KP_PS_COINS = 'ps_coins'            # output addressess for denominate tx
KP_PS_CHANGE = 'ps_change'          # output addressess for pay collateral tx
KP_ALL_TYPES = [KP_INCOMING, KP_SPENDABLE,
                KP_PS_SPENDABLE, KP_PS_COINS, KP_PS_CHANGE]
KP_MAX_INCOMING_TXS = 5             # max count of txs to split on denoms
                                    # need to calc keypairs count to cache


class KPStates(IntEnum):
    '''Keypairs cache states'''
    Empty = 0
    NeedCache = 1
    Caching = 2
    Ready = 3
    Unused = 4


class NotFoundInKeypairs(Exception):
    """Thrown when output address not found in keypairs cache"""


class SignWithKeypairsFailed(Exception):
    """Thrown when transaction signing with keypairs reserved failed"""


class AddPSDataError(Exception):
    """Thrown when failed _add_*_ps_data method"""


class RmPSDataError(Exception):
    """Thrown when failed _rm_*_ps_data method"""


class PSKsInternalAddressCorruption(Exception):

    def __str__(self):
        return _('PS Keystore addresses data corruption detected.'
                 ' Please restore your wallet from seed, and compare'
                 ' the addresses in both files')


class KeyPairsMixin:
    '''PrivateSend cached keypairs for automatic tx signing'''

    NOT_FOUND_KEYS_MSG = _('Insufficient keypairs cached to continue mixing.'
                           ' You can restart mixing to reserve more keypairs')
    SIGN_WIHT_KP_FAILED_MSG = _('Sign with keypairs failed.')
    WALLET_PASSWORD_SET_MSG = _('Wallet password has set. Need to restart'
                                ' mixing for generating keypairs cache')

    def __init__(self, wallet):
        self.keypairs_state_lock = threading.Lock()
        self._keypairs_state = KPStates.Empty
        self._keypairs_cache = {}

    @property
    def keypairs_state(self):
        '''Get keypairs cache state'''
        return self._keypairs_state

    @keypairs_state.setter
    def keypairs_state(self, keypairs_state):
        '''Set keypairs cache state'''
        self._keypairs_state = keypairs_state
        self.postpone_notification('ps-keypairs-changes', self.wallet)

    def on_wallet_password_set(self):
        '''After password set: got to stop mixing as password is needed to sign txs.
        After restarting mixing keypairs will be cached'''
        if self.state == PSStates.Mixing:
            self.stop_mixing(self.WALLET_PASSWORD_SET_MSG)

    async def clean_keypairs_on_timeout(self):
        '''Async task which cleans keypairs after mixing is stopped
        and kp_timeout period is passed'''
        def _clean_kp_on_timeout():
            with self.keypairs_state_lock:
                if self.keypairs_state == KPStates.Unused:
                    self.logger.info('Cleaning Keypairs Cache'
                                     ' on inactivity timeout')
                    self._cleanup_all_keypairs_cache()
                    self.logger.info('Cleaned Keypairs Cache')
                    self.keypairs_state = KPStates.Empty
        while True:
            if self.enabled:
                if (self.state not in self.mixing_running_states
                        and self.keypairs_state == KPStates.Unused
                        and self.mix_stop_secs_ago >= self.kp_timeout * 60):
                    await self.loop.run_in_executor(None, _clean_kp_on_timeout)
            await asyncio.sleep(1)
            if getattr(self, 'is_unittest_run', False):
                break

    async def _make_keypairs_cache(self, password):
        '''Make keypairs cache after mixing is started'''
        _make_cache = self._cache_keypairs
        if password is None:
            return
        while True:
            if self.keypairs_state == KPStates.NeedCache:
                try:
                    await self.loop.run_in_executor(None, _make_cache,
                                                    password)
                except Exception as e:
                    self.logger.info(f'_make_keypairs_cache: {str(e)}')
                    self._cleanup_unfinished_keypairs_cache()
                return
            await asyncio.sleep(1)

    def calc_need_sign_cnt(self, new_denoms_cnt):
        '''Calculate the nmber of signature/keypairs needed according
        to new_denoms_count and pay collateral charges probability'''
        w = self.wallet
        # calc already presented ps_denoms
        old_denoms_cnt = len(w.db.get_ps_denoms(min_rounds=0))
        # calc need sign denoms for each round
        total_denoms_cnt = old_denoms_cnt + new_denoms_cnt
        sign_denoms_cnt = 0
        for r in range(1, self.mix_rounds):  # round 0 calculated later
            next_rounds_denoms_cnt = len(w.db.get_ps_denoms(min_rounds=r+1))
            sign_denoms_cnt += (total_denoms_cnt - next_rounds_denoms_cnt)

        # additional reserve for addrs used by denoms with rounds eq mix_rounds
        sign_denoms_cnt += (total_denoms_cnt - next_rounds_denoms_cnt)

        # Dash Core charges the collateral randomly in 1/10 mixing transactions
        # * avg denoms in mixing transactions is 5 (1-9), but real count
        #   currently is about ~1.1 on testnet, use same for mainnet
        pay_collateral_cnt = ceil(sign_denoms_cnt/10/1.1)
        # new collateral contain 4 pay collateral amounts
        new_collateral_cnt = ceil(pay_collateral_cnt*0.25)
        # * pay collateral uses change in 3/4 of cases (1/4 OP_RETURN output)
        need_sign_change_cnt = ceil(pay_collateral_cnt*0.75)

        # calc existing ps_collaterals by amounts
        old_collaterals_val = 0
        for ps_collateral in w.db.get_ps_collaterals().values():
            old_collaterals_val += ps_collateral[1]
        old_collaterals_cnt = floor(old_collaterals_val/CREATE_COLLATERAL_VAL)
        new_collateral_cnt = max(0, new_collateral_cnt - old_collaterals_cnt)

        # add round 0 denoms (no pay collaterals need to create)
        sign_denoms_cnt += (total_denoms_cnt - old_denoms_cnt)

        need_sign_cnt = sign_denoms_cnt + new_collateral_cnt
        return need_sign_cnt, need_sign_change_cnt, new_collateral_cnt

    def calc_need_new_keypairs_cnt(self):
        '''Calculate the number of keypairs needed on mixing start/keypairs caching'''
        new_denoms_amounts_real = self.calc_need_denoms_amounts()
        new_denoms_cnt_real = sum([len(a) for a in new_denoms_amounts_real])
        new_denoms_val_real = sum([sum(a) for a in new_denoms_amounts_real])
        new_denoms_amounts = self.calc_need_denoms_amounts(on_keep_amount=True)
        new_denoms_val = sum([sum(a) for a in new_denoms_amounts])
        small_mix_funds = (new_denoms_val > new_denoms_val_real)

        if self.calc_denoms_method == self.CalcDenomsMethod.ABS:
            new_denoms_cnt = sum([len(a) for a in new_denoms_amounts])
            need_sign_cnt, need_sign_change_cnt = \
                self.calc_need_sign_cnt(new_denoms_cnt)[0:2]
        elif small_mix_funds:
            part_val = ceil(new_denoms_val / KP_MAX_INCOMING_TXS)
            part_amounts = self.find_denoms_approx(part_val)
            part_amounts_cnt = sum([len(a) for a in part_amounts])
            need_sign_cnt, need_sign_change_cnt = \
                self.calc_need_sign_cnt(part_amounts_cnt)[0:2]
            need_sign_cnt *= KP_MAX_INCOMING_TXS
            need_sign_change_cnt *= KP_MAX_INCOMING_TXS
        else:
            need_sign_cnt, need_sign_change_cnt = \
                self.calc_need_sign_cnt(new_denoms_cnt_real)[0:2]
        return need_sign_cnt, need_sign_change_cnt, small_mix_funds

    def check_need_new_keypairs(self):
        '''Check if there is a need to cache new keypairs in addition
        to possibly already cached ones'''
        w = self.wallet
        if not self.need_password():
            return False, None

        with self.keypairs_state_lock:
            prev_kp_state = self.keypairs_state
            if prev_kp_state in [KPStates.NeedCache, KPStates.Caching]:
                return False, None
            self.keypairs_state = KPStates.NeedCache

        if prev_kp_state == KPStates.Empty:
            return True, prev_kp_state

        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                return True, prev_kp_state

        with w._freeze_lock:
            frozen_addresses = w._frozen_addresses.copy()
        with w.lock:
            # check spendable regular coins keys
            utxos = w.get_utxos(None,
                                excluded_addresses=frozen_addresses,
                                mature_only=True)
            utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
            utxos = self.filter_out_hw_ks_coins(utxos)
            for c in utxos:
                if c.address not in self._keypairs_cache[KP_SPENDABLE]:
                    return True, prev_kp_state

            sign_cnt, sign_change_cnt, small_mix_funds = \
                self.calc_need_new_keypairs_cnt()

            # check cache for incoming addresses on small mix funds
            if not self.is_hw_ks and small_mix_funds:
                cache_incoming = self._keypairs_cache[KP_INCOMING]
                if len(cache_incoming) < KP_MAX_INCOMING_TXS:
                    return True, prev_kp_state

            # check spendable ps coins keys (already saved denoms/collateral)
            for c in self.filter_out_hw_ks_coins(
                    w.get_utxos(None, min_rounds=PSCoinRounds.COLLATERAL)):
                ps_rounds = c.ps_rounds
                if ps_rounds >= self.mix_rounds:
                    continue
                addr = c.address
                if addr not in self._keypairs_cache[KP_PS_SPENDABLE]:
                    return True, prev_kp_state
                else:
                    if w.is_change(addr):
                        sign_change_cnt -= 1
                    else:
                        sign_cnt -= 1

            # check new denoms/collateral signing keys to future coins
            if sign_cnt - len(self._keypairs_cache[KP_PS_COINS]) > 0:
                return True, prev_kp_state
            if sign_change_cnt - len(self._keypairs_cache[KP_PS_CHANGE]) > 0:
                return True, prev_kp_state
        with self.keypairs_state_lock:
            self.keypairs_state = KPStates.Ready
        return False, None

    def _cache_keypairs(self, password):
        '''Cache keypairs on mixing start'''
        self.logger.info('Making Keypairs Cache')
        with self.keypairs_state_lock:
            self.keypairs_state = KPStates.Caching

        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                self._keypairs_cache[cache_type] = {}

        if not self._cache_kp_spendable(password):
            return

        if not self._cache_kp_ps_spendable(password):
            return

        kp_left, kp_chg_left, small_mix_funds = \
            self.calc_need_new_keypairs_cnt()

        if not self.is_hw_ks and small_mix_funds:
            self._cache_kp_incoming(password)

        kp_left, kp_chg_left = self._cache_kp_ps_reserved(password,
                                                          kp_left, kp_chg_left)
        if kp_left is None:
            return

        kp_left, kp_chg_left = self._cache_kp_ps_change(password,
                                                        kp_left, kp_chg_left)
        if kp_left is None:
            return

        kp_left, kp_chg_left = self._cache_kp_ps_coins(password,
                                                       kp_left, kp_chg_left)
        if kp_left is None:
            return

        if self._cache_kp_tmp_reserved(password):
            kp_left, kp_chg_left = self._cache_kp_ps_coins(password,
                                                           kp_left + 1,
                                                           kp_chg_left)
            if kp_left is None:
                return

        with self.keypairs_state_lock:
            self.keypairs_state = KPStates.Ready
        self.logger.info('Keypairs Cache Done')

    def _cache_kp_incoming(self, password):
        '''Cache keypairs for future incoming funds on main keystore'''
        w = self.wallet
        first_recv_index = self.first_unused_index(for_change=False,
                                                   force_main_ks=True)
        ps_incoming_cache = self._keypairs_cache[KP_INCOMING]
        cached = 0
        ri = first_recv_index
        while cached < KP_MAX_INCOMING_TXS:
            if self.state != PSStates.Mixing:
                self._cleanup_unfinished_keypairs_cache()
                return
            sequence = [0, ri]
            pubkey = w.keystore.derive_pubkey(*sequence).hex()
            addr = pubkey_to_address(w.txin_type, pubkey)
            ri += 1
            if w.is_used(addr):
                continue
            if addr in ps_incoming_cache:
                continue
            sec = w.keystore.get_private_key(sequence, password)
            ps_incoming_cache[addr] = (pubkey, sec)
            cached += 1
        self.logger.info(f'Cached {cached} keys'
                         f' of {KP_INCOMING} type')
        self.postpone_notification('ps-keypairs-changes', self.wallet)

    def _cache_kp_spendable(self, password):
        '''Cache spendable regular coins keys'''
        w = self.wallet
        cached = 0
        with w._freeze_lock:
            frozen_addresses = w._frozen_addresses.copy()
        utxos = w.get_utxos(None,
                            excluded_addresses=frozen_addresses,
                            mature_only=True)
        utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
        utxos = self.filter_out_hw_ks_coins(utxos)
        for c in utxos:
            if self.state != PSStates.Mixing:
                self._cleanup_unfinished_keypairs_cache()
                return
            addr = c.address
            if addr in self._keypairs_cache[KP_SPENDABLE]:
                continue
            sequence = None
            if self.ps_keystore:
                sequence = self.get_address_index(addr)
            if sequence:
                pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                sec = self.ps_keystore.get_private_key(sequence, password)
            else:
                sequence = w.get_address_index(addr)
                pubkey = w.keystore.derive_pubkey(*sequence).hex()
                sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[KP_SPENDABLE][addr] = (pubkey, sec)
            cached += 1
        if cached:
            self.logger.info(f'Cached {cached} keys of {KP_SPENDABLE} type')
            self.postpone_notification('ps-keypairs-changes', self.wallet)
        return True

    def _cache_kp_ps_spendable(self, password):
        '''Cache spendable ps coins keys (existing denoms/collaterals)'''
        w = self.wallet
        cached = 0
        ps_spendable_cache = self._keypairs_cache[KP_PS_SPENDABLE]
        for c in self.filter_out_hw_ks_coins(
                w.get_utxos(None, min_rounds=PSCoinRounds.COLLATERAL)):
            if self.state != PSStates.Mixing:
                self._cleanup_unfinished_keypairs_cache()
                return
            outpoint = c.prevout.to_str()
            ps_denom = w.db.get_ps_denom(outpoint)
            if ps_denom and ps_denom[2] >= self.mix_rounds:
                continue
            addr = c.address
            if self.is_hw_ks and not self.is_ps_ks(addr):
                continue  # skip denoms on hw keystore
            if addr in ps_spendable_cache:
                continue
            sequence = None
            if self.ps_keystore:
                sequence = self.get_address_index(addr)
            if sequence:
                pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                sec = self.ps_keystore.get_private_key(sequence, password)
            else:
                sequence = w.get_address_index(addr)
                pubkey = w.keystore.derive_pubkey(*sequence).hex()
                sec = w.keystore.get_private_key(sequence, password)
            ps_spendable_cache[addr] = (pubkey, sec)
            cached += 1
        if cached:
            self.logger.info(f'Cached {cached} keys of {KP_PS_SPENDABLE} type')
            self.postpone_notification('ps-keypairs-changes', self.wallet)
        return True

    def _cache_kp_ps_reserved(self, password, sign_cnt, sign_change_cnt):
        '''Cache keypairs for PS reserved addresses'''
        w = self.wallet
        ps_change_cache = self._keypairs_cache[KP_PS_CHANGE]
        ps_coins_cache = self._keypairs_cache[KP_PS_COINS]
        cached = 0
        for addr, data in self.wallet.db.get_ps_reserved().items():
            if self.state != PSStates.Mixing:
                self._cleanup_unfinished_keypairs_cache()
                return None, None
            if w.is_used(addr):
                continue
            if self.is_hw_ks and not self.is_ps_ks(addr):
                continue  # skip denoms on hw keystore
            if w.is_change(addr):
                sign_change_cnt -= 1
                if addr in ps_change_cache:
                    continue
                sequence = None
                if self.ps_keystore:
                    sequence = self.get_address_index(addr)
                if sequence:
                    pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                    sec = self.ps_keystore.get_private_key(sequence, password)
                else:
                    sequence = w.get_address_index(addr)
                    pubkey = w.keystore.derive_pubkey(*sequence).hex()
                    sec = w.keystore.get_private_key(sequence, password)
                ps_change_cache[addr] = (pubkey, sec)
                cached += 1
            else:
                sign_cnt -= 1
                if addr in ps_coins_cache:
                    continue
                sequence = None
                if self.ps_keystore:
                    sequence = self.get_address_index(addr)
                if sequence:
                    pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                    sec = self.ps_keystore.get_private_key(sequence, password)
                else:
                    sequence = w.get_address_index(addr)
                    pubkey = w.keystore.derive_pubkey(*sequence).hex()
                    sec = w.keystore.get_private_key(sequence, password)
                ps_coins_cache[addr] = (pubkey, sec)
                cached += 1
        if cached:
            self.logger.info(f'Cached {cached} keys for ps_reserved addresses')
            self.postpone_notification('ps-keypairs-changes', self.wallet)
        return sign_cnt, sign_change_cnt

    def _cache_kp_ps_change(self, password, sign_cnt, sign_change_cnt):
        '''Cache keypairs for PS change addresses'''
        if sign_change_cnt > 0:
            w = self.wallet
            first_change_index = self.first_unused_index(for_change=True)
            ps_change_cache = self._keypairs_cache[KP_PS_CHANGE]
            cached = 0
            ci = first_change_index
            while sign_change_cnt > 0:
                if self.state != PSStates.Mixing:
                    self._cleanup_unfinished_keypairs_cache()
                    return None, None
                sequence = [1, ci]
                if self.ps_keystore:
                    pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                    addr = pubkey_to_address(self.ps_ks_txin_type, pubkey)
                else:
                    pubkey = w.keystore.derive_pubkey(*sequence).hex()
                    addr = pubkey_to_address(w.txin_type, pubkey)
                ci += 1
                if w.is_used(addr):
                    continue
                sign_change_cnt -= 1
                if addr in ps_change_cache:
                    continue
                if self.ps_keystore:
                    sec = self.ps_keystore.get_private_key(sequence, password)
                else:
                    sec = w.keystore.get_private_key(sequence, password)
                ps_change_cache[addr] = (pubkey, sec)
                cached += 1
                if not cached % 100:
                    self.logger.info(f'Cached {cached} keys'
                                     f' of {KP_PS_CHANGE} type')
            if cached:
                self.logger.info(f'Cached {cached} keys'
                                 f' of {KP_PS_CHANGE} type')
                self.postpone_notification('ps-keypairs-changes', self.wallet)
        return sign_cnt, sign_change_cnt

    def _cache_kp_ps_coins(self, password, sign_cnt, sign_change_cnt):
        '''Cache keypairs for PS receiving addresses (denoms/collaterals)'''
        if sign_cnt > 0:
            w = self.wallet
            first_recv_index = self.first_unused_index(for_change=False)
            ps_coins_cache = self._keypairs_cache[KP_PS_COINS]
            cached = 0
            ri = first_recv_index
            while sign_cnt > 0:
                if self.state != PSStates.Mixing:
                    self._cleanup_unfinished_keypairs_cache()
                    return None, None
                sequence = [0, ri]
                if self.ps_keystore:
                    pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                    addr = pubkey_to_address(self.ps_ks_txin_type, pubkey)
                else:
                    pubkey = w.keystore.derive_pubkey(*sequence).hex()
                    addr = pubkey_to_address(w.txin_type, pubkey)
                ri += 1
                if w.is_used(addr):
                    continue
                sign_cnt -= 1
                if addr in ps_coins_cache:
                    continue
                if self.ps_keystore:
                    sec = self.ps_keystore.get_private_key(sequence, password)
                else:
                    sec = w.keystore.get_private_key(sequence, password)
                ps_coins_cache[addr] = (pubkey, sec)
                cached += 1
                if not cached % 100:
                    self.logger.info(f'Cached {cached} keys'
                                     f' of {KP_PS_COINS} type')
            if cached:
                self.logger.info(f'Cached {cached} keys'
                                 f' of {KP_PS_COINS} type')
                self.postpone_notification('ps-keypairs-changes', self.wallet)
        return sign_cnt, sign_change_cnt

    def _cache_kp_tmp_reserved(self, password):
        '''Cache keypairs for PS Keystore addresses funded from HW keystore.
        Addresses must not be reserved for PS use but tmp reserved instead'''
        w = self.wallet
        addr = self.get_tmp_reserved_address()
        if not addr:
            return False
        sequence = None
        if self.ps_keystore:
            sequence = self.get_address_index(addr)
        if sequence:
            pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
            sec = self.ps_keystore.get_private_key(sequence, password)
        else:
            sequence = w.get_address_index(addr)
            pubkey = w.keystore.derive_pubkey(*sequence).hex()
            sec = w.keystore.get_private_key(sequence, password)
        spendable_cache = self._keypairs_cache[KP_SPENDABLE]
        spendable_cache[addr] = (pubkey, sec)
        self.logger.info(f'Cached key of {KP_SPENDABLE} type'
                         f' for tmp reserved address')
        self.postpone_notification('ps-keypairs-changes', self.wallet)
        ps_coins_cache = self._keypairs_cache[KP_PS_COINS]
        if addr in ps_coins_cache:
            ps_coins_cache.pop(addr, None)
            return True
        else:
            return False

    def _find_addrs_not_in_keypairs(self, addrs):
        '''Calculate set of addresses not found in keypairs cache'''
        addrs = set(addrs)
        keypairs_addrs = set()
        for cache_type in KP_ALL_TYPES:
            if cache_type in self._keypairs_cache:
                keypairs_addrs |= self._keypairs_cache[cache_type].keys()
        return addrs - keypairs_addrs

    def unpack_mine_input_addrs(func):
        '''Decorator to prepare tx inputs addresses'''
        def func_wrapper(self, txid, tx, tx_type):
            w = self.wallet
            inputs = []
            for i in tx.inputs():
                prev_h = i.prevout.txid.hex()
                prev_n = i.prevout.out_idx
                outpoint = i.prevout.to_str()
                prev_tx = w.db.get_transaction(prev_h)
                if prev_tx:
                    o = prev_tx.outputs()[prev_n]
                    if w.is_mine(o.address):
                        inputs.append((outpoint, o.address))
            return func(self, txid, tx_type, inputs, tx.outputs())
        return func_wrapper

    @unpack_mine_input_addrs
    def _cleanup_spendable_keypairs(self, txid, tx_type, inputs, outputs):
        '''Cleanup spendable keypairs after coin used in confirmed PS txs'''
        spendable_cache = self._keypairs_cache.get(KP_SPENDABLE, {})
        # first input addr used for change in new denoms/collateral txs
        first_input_addr = inputs[0][1]
        if first_input_addr in [o.address for o in outputs]:
            change_addr = first_input_addr
        else:
            change_addr = None
        # cleanup spendable keypairs excluding change address
        for outpoint, addr in inputs:
            if change_addr and change_addr == addr:
                continue
            spendable_cache.pop(addr, None)

        # move ps coins keypairs to ps spendable cache
        ps_coins_cache = self._keypairs_cache.get(KP_PS_COINS, {})
        ps_spendable_cache = self._keypairs_cache.get(KP_PS_SPENDABLE, {})
        for o in outputs:
            addr = o.address
            if addr in ps_coins_cache:
                keypair = ps_coins_cache.pop(addr, None)
                if keypair is not None:
                    ps_spendable_cache[addr] = keypair

    @unpack_mine_input_addrs
    def _cleanup_ps_keypairs(self, txid, tx_type, inputs, outputs):
        '''Cleanup PS keypairs after coin used in confirmed PS txs'''
        ps_spendable_cache = self._keypairs_cache.get(KP_PS_SPENDABLE, {})
        ps_coins_cache = self._keypairs_cache.get(KP_PS_COINS, {})
        ps_change_cache = self._keypairs_cache.get(KP_PS_CHANGE, {})

        # cleanup ps spendable keypairs
        for outpoint, addr in inputs:
            if addr in ps_spendable_cache:
                ps_spendable_cache.pop(addr, None)

        # move ps change, ps coins keypairs to ps spendable cache
        w = self.wallet
        for i, o in enumerate(outputs):
            addr = o.address
            if addr in ps_change_cache:
                keypair = ps_change_cache.pop(addr, None)
                if keypair is not None and tx_type == PSTxTypes.PAY_COLLATERAL:
                    ps_spendable_cache[addr] = keypair
            elif addr in ps_coins_cache:
                keypair = ps_coins_cache.pop(addr, None)
                if keypair is not None and tx_type == PSTxTypes.DENOMINATE:
                    outpoint = f'{txid}:{i}'
                    ps_denom = w.db.get_ps_denom(outpoint)
                    if ps_denom and ps_denom[2] < self.mix_rounds:
                        ps_spendable_cache[addr] = keypair

    def _cleanup_unfinished_keypairs_cache(self):
        '''Cleanup keypairs cache in unfinished state if mixing stopped'''
        with self.keypairs_state_lock:
            self.logger.info('Cleaning unfinished Keypairs Cache')
            self._cleanup_all_keypairs_cache()
            self.keypairs_state = KPStates.Empty
            self.logger.info('Cleaned Keypairs Cache')

    def _cleanup_all_keypairs_cache(self):
        '''Cleanup keypairs cache'''
        if not self._keypairs_cache:
            return
        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                continue
            for addr in list(self._keypairs_cache[cache_type].keys()):
                self._keypairs_cache[cache_type].pop(addr)
            self._keypairs_cache.pop(cache_type)

    def get_keypairs(self):
        '''Transform keypairs cache to dict suitable for Transaction.sign'''
        keypairs = {}
        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                continue
            for pubkey, sec in self._keypairs_cache[cache_type].values():
                keypairs[pubkey] = sec
        return keypairs

    def get_keypairs_for_denominate_tx(self, tx, password):
        '''Derive keypairs for denominate tx to fix add_info_from_wallet
        problem on transactions with unknown addresses (post 4.1.х fix)'''
        w = self.wallet
        keypairs = {}
        for txin in tx.inputs():
            addr = txin.address
            if addr is None:
                continue
            sequence = None
            if self.ps_keystore:
                sequence = self.get_address_index(addr)
            if sequence:
                pubkey = self.ps_keystore.derive_pubkey(*sequence).hex()
                sec = self.ps_keystore.get_private_key(sequence, password)
            else:
                sequence = w.get_address_index(addr)
                pubkey = w.keystore.derive_pubkey(*sequence).hex()
                sec = w.keystore.get_private_key(sequence, password)
            keypairs[pubkey] = sec
        return keypairs

    def sign_transaction(self, tx, password, mine_txins_cnt=None):
        '''Sign PS transactions in automatic mode (with keypairs if cached).
        Sign denominate transactions if mine_txins_cnt set, due to problems
        with add_info_from_wallet with unknown addresses (post 4.1.x fix)'''
        if self._keypairs_cache or mine_txins_cnt:
            if mine_txins_cnt is None:
                tx.add_info_from_wallet(self.wallet)
            if self._keypairs_cache:
                keypairs = self.get_keypairs()
            else:
                keypairs = self.get_keypairs_for_denominate_tx(tx, password)
            signed_txins_cnt = tx.sign(keypairs)
            tx.finalize_psbt()
            keypairs.clear()
            if mine_txins_cnt is None:
                mine_txins_cnt = len(tx.inputs())
            if signed_txins_cnt < mine_txins_cnt:
                self.logger.debug(f'mine txins cnt: {mine_txins_cnt},'
                                  f' signed txins cnt: {signed_txins_cnt}')
                raise SignWithKeypairsFailed('Tx signing failed')
        else:
            self.wallet.sign_transaction(tx, password)
        return tx


class PSDataMixin:
    '''PrivateSend wallet stored data funtcionality'''

    def __init__(self, wallet):
        self.wallet = wallet
        # _ps_denoms_amount_cache recalculated in add_ps_denom/pop_ps_denom
        self._ps_denoms_amount_cache = 0
        denoms = wallet.db.get_ps_denoms()
        for addr, value, rounds in denoms.values():
            self._ps_denoms_amount_cache += value

        # _denoms_to_mix_cache recalculated on mix_rounds change and
        # in add[_mixing]_denom/pop[_mixing]_denom methods
        self._denoms_to_mix_cache = self.denoms_to_mix()

        # sycnhronizer unsubsribed addresses
        self.spent_addrs = set()
        self.unsubscribed_addrs = set()

    def load_and_cleanup(self):
        '''Start on wallet load_and_cleanup if PS enabled
        or when PS enabled first time'''
        if not self.enabled:
            return
        w = self.wallet
        # enable ps_keystore and syncronize addresses
        if not self.ps_keystore:
            self.enable_ps_keystore()
        # set ps_keystore_has_history flag
        if w.db.get_ps_txs() or w.db.get_ps_txs_removed():
            if w.db.get_history(main_ks=False):
                self.ps_keystore_has_history = True
        # check last_mix_stop_time if it was not saved on wallet crash
        last_mix_start_time = self.last_mix_start_time
        last_mix_stop_time = self.last_mix_stop_time
        if last_mix_stop_time < last_mix_start_time:
            last_mixed_tx_time = self.last_mixed_tx_time
            wait_time = self.wait_for_mn_txs_time
            if last_mixed_tx_time > last_mix_start_time:
                self.last_mix_stop_time = last_mixed_tx_time + wait_time
            else:
                self.last_mix_stop_time = last_mix_start_time + wait_time
        # load and unsubscribe spent ps addresses
        unspent = w.db.get_unspent_ps_addresses()
        for addr in w.db.get_ps_addresses():
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)
            if self.subscribe_spent:
                continue
            hist = w.db.get_addr_history(addr)
            self.unsubscribe_spent_addr(addr, hist)
        self._fix_uncompleted_ps_txs()

    @property
    def ps_collateral_cnt(self):
        '''Get count of collateral coins'''
        return len(self.wallet.db.get_ps_collaterals())

    def add_ps_spending_collateral(self, outpoint, wfl_uuid):
        '''Add collateral outpoint as prepared for spending
        in pay collateral workflow'''
        self.wallet.db._add_ps_spending_collateral(outpoint, wfl_uuid)

    def pop_ps_spending_collateral(self, outpoint):
        '''Pop collateral outpoint from prepared for spending
        in pay collateral workflow'''
        return self.wallet.db._pop_ps_spending_collateral(outpoint)

    def add_ps_reserved(self, addr, data):
        '''Add address to PS reserved, usually with workflow uuid as data'''
        self.wallet.db._add_ps_reserved(addr, data)
        self.postpone_notification('ps-reserved-changes', self.wallet)

    def pop_ps_reserved(self, addr):
        '''Pop address from PS reserved'''
        data = self.wallet.db._pop_ps_reserved(addr)
        self.postpone_notification('ps-reserved-changes', self.wallet)
        return data

    def add_ps_denom(self, outpoint, denom):
        '''Add outpoint as ps_denom, denom data is (addr, value, rounds)'''
        self.wallet.db._add_ps_denom(outpoint, denom)
        self._ps_denoms_amount_cache += denom[1]
        if denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom

    def pop_ps_denom(self, outpoint):
        '''Pop outpoint from ps_denom'''
        denom = self.wallet.db._pop_ps_denom(outpoint)
        if denom:
            self._ps_denoms_amount_cache -= denom[1]
            self._denoms_to_mix_cache.pop(outpoint, None)
        return denom

    def calc_denoms_by_values(self):
        '''Calc and return dict: denom value => denoms count'''
        denoms_values = [denom[1]
                         for denom in self.wallet.db.get_ps_denoms().values()]
        if not denoms_values:
            return {}
        denoms_by_values = {denom_val: 0 for denom_val in PS_DENOMS_VALS}
        denoms_by_values.update(Counter(denoms_values))
        return denoms_by_values

    def add_ps_spending_denom(self, outpoint, wfl_uuid):
        '''Add outpoint as ps_spending_denom,
        prepared to spending in denominate wfl'''
        self.wallet.db._add_ps_spending_denom(outpoint, wfl_uuid)
        self._denoms_to_mix_cache.pop(outpoint, None)

    def pop_ps_spending_denom(self, outpoint):
        '''Pop outpoint as ps_spending_denom'''
        db = self.wallet.db
        denom = db.get_ps_denom(outpoint)
        if denom and denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom
        return db._pop_ps_spending_denom(outpoint)

    @property
    def pay_collateral_wfl(self):
        '''Get current pay collateral workflow'''
        d = self.wallet.db.get_ps_data('pay_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_pay_collateral_wfl(self, workflow):
        '''Set workflow as current pay collateral workflow'''
        self.wallet.db.set_ps_data('pay_collateral_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_pay_collateral_wfl(self):
        '''Clear current pay collateral workflow'''
        self.wallet.db.set_ps_data('pay_collateral_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def new_collateral_wfl(self):
        '''Get current new collateral workflow'''
        d = self.wallet.db.get_ps_data('new_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_collateral_wfl(self, workflow):
        '''Set workflow as current new collateral workflow'''
        self.wallet.db.set_ps_data('new_collateral_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_new_collateral_wfl(self):
        '''Clear current new collateral workflow'''
        self.wallet.db.set_ps_data('new_collateral_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def new_denoms_wfl(self):
        '''Get current new denoms workflow'''
        d = self.wallet.db.get_ps_data('new_denoms_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_denoms_wfl(self, workflow):
        '''Set workflow as current new denoms workflow'''
        self.wallet.db.set_ps_data('new_denoms_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_new_denoms_wfl(self):
        '''Clear current new denoms workflow'''
        self.wallet.db.set_ps_data('new_denoms_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def denominate_wfl_list(self):
        '''Get list of denominate workflows uuids'''
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        return list(wfls.keys())

    @property
    def active_denominate_wfl_cnt(self):
        '''Get count of denominate workflows in progress'''
        cnt = 0
        for uuid in self.denominate_wfl_list:
            wfl = self.get_denominate_wfl(uuid)
            if wfl and not wfl.completed:
                cnt += 1
        return cnt

    def get_denominate_wfl(self, uuid):
        '''Get denominate workflow identified by uuid'''
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        wfl = wfls.get(uuid)
        if wfl:
            return PSDenominateWorkflow._from_uuid_and_tuple(uuid, wfl)

    def clear_denominate_wfl(self, uuid):
        '''Clear denominate workflow identified by uuid'''
        self.wallet.db.pop_ps_data('denominate_workflows', uuid)
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def set_denominate_wfl(self, workflow):
        '''Add denominate wfl to denominate_workflows dict: uuid => wfl'''
        wfl_dict = workflow._as_dict()
        self.wallet.db.update_ps_data('denominate_workflows', wfl_dict)
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def set_tmp_reserved_address(self, address):
        '''Used to reserve address to not be used in ps reservation'''
        self.wallet.db.set_ps_data('tmp_reserved_address', address)

    def get_tmp_reserved_address(self):
        '''Get address currently set not to be used in PS reservation'''
        return self.wallet.db.get_ps_data('tmp_reserved_address', '')

    def filter_out_hw_ks_coins(self, coins):
        '''Filter out hardware keystore coins from list of coins'''
        if self.is_hw_ks:
            coins = [c for c in coins if c.is_ps_ks]
        return coins

    def get_ps_data_info(self):
        '''Gather info on PS data for UI'''
        res = []
        w = self.wallet
        data = w.db.get_ps_txs()
        res.append(f'PrivateSend transactions count: {len(data)}')
        data = w.db.get_ps_txs_removed()
        res.append(f'Removed PrivateSend transactions count: {len(data)}')

        data = w.db.get_ps_denoms()
        res.append(f'ps_denoms count: {len(data)}')
        data = w.db.get_ps_spent_denoms()
        res.append(f'ps_spent_denoms count: {len(data)}')
        data = w.db.get_ps_spending_denoms()
        res.append(f'ps_spending_denoms count: {len(data)}')

        data = w.db.get_ps_collaterals()
        res.append(f'ps_collaterals count: {len(data)}')
        data = w.db.get_ps_spent_collaterals()
        res.append(f'ps_spent_collaterals count: {len(data)}')
        data = w.db.get_ps_spending_collaterals()
        res.append(f'ps_spending_collaterals count: {len(data)}')

        data = w.db.get_ps_others()
        res.append(f'ps_others count: {len(data)}')
        data = w.db.get_ps_spent_others()
        res.append(f'ps_spent_others count: {len(data)}')

        data = w.db.get_ps_reserved()
        res.append(f'Reserved addresses count: {len(data)}')

        if self.pay_collateral_wfl:
            res.append('Pay collateral workflow data exists')

        if self.new_collateral_wfl:
            res.append('New collateral workflow data exists')

        if self.new_denoms_wfl:
            res.append('New denoms workflow data exists')

        completed_dwfl_cnt = 0
        dwfl_list = self.denominate_wfl_list
        dwfl_cnt = len(dwfl_list)
        for uuid in dwfl_list:
            wfl = self.get_denominate_wfl(uuid)
            if wfl and wfl.completed:
                completed_dwfl_cnt += 1
        if dwfl_cnt:
            res.append(f'Denominate workflow count: {dwfl_cnt},'
                       f' completed: {completed_dwfl_cnt}')

        if self._keypairs_cache:
            for cache_type in KP_ALL_TYPES:
                if cache_type in self._keypairs_cache:
                    cnt = len(self._keypairs_cache[cache_type])
                    res.append(f'Keypairs cache type: {cache_type}'
                               f' cached keys: {cnt}')
        if self.gather_mix_stat:
            res.append(str(self.mix_stat))
        return res

    def first_unused_index(self, for_change=False, force_main_ks=False):
        '''Get first unused address index'''
        w = self.wallet
        ps_ks = self.ps_keystore and not force_main_ks
        with w.lock:
            if for_change:
                unused = (self.get_unused_addresses(for_change) if ps_ks
                          else w.calc_unused_change_addresses())
            else:
                unused = (self.get_unused_addresses() if ps_ks
                          else w.get_unused_addresses())
            if unused:
                return (self.get_address_index(unused[0])[1] if ps_ks
                        else w.get_address_index(unused[0])[1])
            # no unused, return first index beyond last address in db
            if for_change:
                return w.db.num_change_addresses(ps_ks=ps_ks)
            else:
                return w.db.num_receiving_addresses(ps_ks=ps_ks)

    def add_spent_addrs(self, addrs):
        '''Save addresses as spent, to minimize electrum server
        usage for spent denoms'''
        w = self.wallet
        unspent = w.db.get_unspent_ps_addresses()
        for addr in addrs:
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)

    def restore_spent_addrs(self, addrs):
        '''Remove addresses from spent and subscribe on server again'''
        for addr in addrs:
            self.spent_addrs.remove(addr)
            self.subscribe_spent_addr(addr)

    def subscribe_spent_addr(self, addr):
        '''Return previously unsubscribed address to synchronizer'''
        w = self.wallet
        if addr in self.unsubscribed_addrs:
            self.unsubscribed_addrs.remove(addr)
            if w.synchronizer:
                self.logger.debug(f'Add {addr} to synchronizer')
                w.synchronizer.add(addr)

    def unsubscribe_spent_addr(self, addr, hist):
        '''Unsubscribe spent address from synchronizer/electrum server'''
        if (self.subscribe_spent
                or addr not in self.spent_addrs
                or addr in self.unsubscribed_addrs
                or not hist):
            return
        w = self.wallet
        local_height = w.get_local_height()
        for hist_item in hist:
            txid = hist_item[0]
            verified_tx = w.db.verified_tx.get(txid)
            if not verified_tx:
                return
            height = verified_tx[0]
            conf = local_height - height + 1
            if conf < 6:
                return
        self.unsubscribed_addrs.add(addr)
        if w.synchronizer:
            self.logger.debug(f'Remove {addr} from synchronizer')
            w.synchronizer.remove_addr(addr)

    @property
    def all_mixed(self):
        '''Check all denominations have needed rounds
        and keep amount is reached'''
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return False

        r = self.mix_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        if ps_balance < dn_balance:
            return False

        need_val = to_duffs(self.keep_amount) + CREATE_COLLATERAL_VAL
        approx_val = need_val - dn_balance
        outputs_amounts = self.find_denoms_approx(approx_val)
        if outputs_amounts:
            return False
        return True

    def reserve_addresses(self, addrs_count, for_change=False,
                          data=None, force_main_ks=False, tmp=False):
        '''Reserve addresses for PS use or if tmp is True reserve one
           receiving address temporarily to not be reserved for ps
           during funds are sent to it'''
        if tmp and addrs_count > 1:
            raise Exception('tmp can be used only for one address reservation')
        if tmp and for_change:
            raise Exception('tmp param can not be used with for_change param')
        if tmp and data is not None:
            raise Exception('tmp param can not be used with data param')

        result = []
        w = self.wallet
        ps_ks = self.ps_keystore and not force_main_ks
        with w.lock:
            while len(result) < addrs_count:
                if for_change:
                    unused = (self.get_unused_addresses(for_change) if ps_ks
                              else w.calc_unused_change_addresses())
                else:
                    unused = (self.get_unused_addresses() if ps_ks
                              else w.get_unused_addresses())
                if unused:
                    addr = unused[0]
                else:
                    addr = (self.create_new_address(for_change) if ps_ks
                            else w.create_new_address(for_change))
                if tmp:
                    self.set_tmp_reserved_address(addr)
                else:
                    self.add_ps_reserved(addr, data)
                result.append(addr)
        return result

    async def get_next_coins_for_mixing(self, for_denoms=True):
        '''Async: get next suitable coins for new denoms/collateral txs,
        depending of group_origin_coins_by_addr preference.
        Add delay when group_origin_coins_by_addr active'''
        if self.group_origin_coins_by_addr:  # delay between new transactions
            rand_interval = random.randint(self.MIN_NEW_DENOMS_DELAY,
                                           self.MAX_NEW_DENOMS_DELAY)
            log_info = True
            while time.time() - self.last_denoms_tx_time < rand_interval:
                if log_info:
                    elapsed_time = time.time() - self.last_denoms_tx_time
                    s = max(0, rand_interval - elapsed_time)
                    self.logger.info(f'Waiting {s} seconds before starting'
                                     f' new transactions workflow')
                    log_info = False
                await asyncio.sleep(1)
        return self._get_next_coins_for_mixing(for_denoms=for_denoms)

    def _get_next_coins_for_mixing(self, for_denoms=True):
        '''Get next suitable coins for new denoms/collateral txs,
        depending of group_origin_coins_by_addr preference'''
        w = self.wallet
        with w._freeze_lock:
            frozen_addresses = w._frozen_addresses.copy()
        coins = w.get_utxos(None,
                            excluded_addresses=frozen_addresses,
                            mature_only=True, confirmed_funding_only=True,
                            consider_islocks=True, include_ps=True)
        coins = [c for c in coins
                 if c.ps_rounds in [None, PSCoinRounds.MIX_ORIGIN]]
        coins = [c for c in coins if not w.is_frozen_coin(c)]
        coins = self.filter_out_hw_ks_coins(coins)
        if not coins:
            return {'total_val': 0, 'coins': []}

        coins_by_address = {}
        for c in coins:
            if self.group_origin_coins_by_addr:
                addr = c.address
            else:
                addr = 'All'
            if addr not in coins_by_address:
                coins_by_address[addr] = {
                    'total_val': c.value_sats(),
                    'coins': [c],
                    'mix_origin': c.ps_rounds is not None}
            else:
                coins_by_address[addr]['total_val'] += c.value_sats()
                coins_by_address[addr]['coins'].append(c)
        coins = sorted(coins_by_address.values(),
                       key=lambda x: (x['mix_origin'], x['total_val']),
                       reverse=True)
        if self.group_origin_coins_by_addr:
            while coins and for_denoms:
                if self.calc_need_denoms_amounts(coins[0]['coins'],
                                                 use_cache=True,
                                                 use_all_coins=False):
                    break
                coins = coins[1:]
        if not coins:
            return {'total_val': 0, 'coins': []}
        else:
            return coins[0]

    def calc_need_denoms_amounts(self, coins=None, use_cache=False,
                                 on_keep_amount=False, use_all_coins=True):
        '''Calc new denoms transactions output denoms count and amounts

        coins: calc amounts of transactions on coins instead keep amount
        use_cache: use _ps_denoms_amount_cache instead get from db and calc
        on_keep_amount: calc amounts on keep amount even if not enough funds
        use_all_coins: make no change (to split big denoms to small)'''
        fee_per_kb = self.config.fee_per_kb()
        if fee_per_kb is None:
            raise NoDynamicFeeEstimates()

        if coins and use_all_coins:  # calc on coins selected from GUI
            return self._calc_denoms_amounts_from_coins(coins, fee_per_kb)

        if use_cache:
            old_denoms_val = self._ps_denoms_amount_cache
        else:
            old_denoms_val = sum(self.wallet.get_balance(include_ps=False,
                                                         min_rounds=0))

        need_val = to_duffs(self.keep_amount)
        calc_method = self.calc_denoms_method
        if calc_method != self.CalcDenomsMethod.ABS:
            need_val += CREATE_COLLATERAL_VAL
        if need_val < old_denoms_val:  # already have needed value of denoms
            return []

        if not coins:
            coins_data = self._get_next_coins_for_mixing()
            coins = coins_data['coins']
        coins_val = sum([c.value_sats() for c in coins])
        if coins_val < MIN_DENOM_VAL and not on_keep_amount:
            return []  # no coins to create denoms

        in_cnt = len(coins)
        if calc_method == self.CalcDenomsMethod.ABS and on_keep_amount:
            outputs_amounts = self.find_denoms_approx(need_val)
        else:
            approx_val = need_val - old_denoms_val
            outputs_amounts = self.find_denoms_approx(approx_val)
        total_need_val, outputs_amounts = \
            self._calc_total_need_val(in_cnt, outputs_amounts, fee_per_kb)
        if on_keep_amount or coins_val >= total_need_val:
            return outputs_amounts

        # not enough funds to mix keep amount, approx amount that can be mixed
        approx_val = coins_val
        while True:
            if approx_val < CREATE_COLLATERAL_VAL:
                return []
            outputs_amounts = self.find_denoms_approx(approx_val)
            total_need_val, outputs_amounts = \
                self._calc_total_need_val(in_cnt, outputs_amounts, fee_per_kb)
            if coins_val >= total_need_val:
                return outputs_amounts
            else:
                approx_val -= MIN_DENOM_VAL

    def _calc_total_need_val(self, txin_cnt, outputs_amounts, fee_per_kb):
        '''Calculate total needed value of inputs for new denoms transaction,
        including fee, based on inputs count, outputs amounts and fee_per_kb.
        Also added amount needed to create supposed future new collateral
        transactions, needed to mix all denoms to required number of rounds'''
        res_outputs_amounts = copy.deepcopy(outputs_amounts)
        new_denoms_val = sum([sum(a) for a in res_outputs_amounts])
        new_denoms_cnt = sum([len(a) for a in res_outputs_amounts])

        # calc future new collaterals count and value
        new_collateral_cnt = self.calc_need_sign_cnt(new_denoms_cnt)[2]
        if not self.ps_collateral_cnt and res_outputs_amounts:
            new_collateral_cnt -= 1
            res_outputs_amounts[0].insert(0, CREATE_COLLATERAL_VAL)
            new_denoms_val += CREATE_COLLATERAL_VAL
        new_collaterals_val = CREATE_COLLATERAL_VAL * new_collateral_cnt

        # calc new denoms fee
        new_denoms_fee = 0
        for i, amounts in enumerate(res_outputs_amounts):
            if i == 0:  # use all coins as inputs, add change output
                new_denoms_fee += calc_tx_fee(txin_cnt, len(amounts) + 1,
                                              fee_per_kb, max_size=True)
            else:  # use change from prev txs as input
                new_denoms_fee += calc_tx_fee(1, len(amounts) + 1,
                                              fee_per_kb, max_size=True)

        # calc future new collaterals fee
        new_collateral_fee = calc_tx_fee(1, 2, fee_per_kb, max_size=True)
        new_collaterals_fee = new_collateral_cnt * new_collateral_fee

        # have coins enough to create new denoms and future new collaterals
        total_need_val = (new_denoms_val + new_denoms_fee +
                          new_collaterals_val + new_collaterals_fee)
        return total_need_val, res_outputs_amounts

    def _calc_denoms_amounts_fee(self, coins_cnt, denoms_amounts, fee_per_kb):
        '''Calculate new denoms transactions minimal needed fee, based on input coins
        count, denoms amounts list and fee_per_kb'''
        txs_fee = 0
        tx_cnt = len(denoms_amounts)
        for i in range(tx_cnt):
            amounts = denoms_amounts[i]
            if i == 0:
                # inputs: coins
                # outputs: denoms + new denom + collateral + change
                out_cnt = len(amounts) + 3
                txs_fee += calc_tx_fee(coins_cnt, out_cnt,
                                       fee_per_kb, max_size=True)
            elif i == tx_cnt - 1:
                # inputs: one change amount
                # outputs: denoms + new denom
                out_cnt = len(amounts) + 1
                txs_fee += calc_tx_fee(1, out_cnt,
                                       fee_per_kb, max_size=True)
            else:
                # inputs: one change amount
                # outputs: is denoms + denom + change
                out_cnt = len(amounts) + 2
                txs_fee += calc_tx_fee(1, out_cnt,
                                       fee_per_kb, max_size=True)

        return txs_fee

    def _calc_denoms_amounts_from_coins(self, coins, fee_per_kb):
        '''Calc new denoms transactions output amounts for given coins'''
        coins_val = sum([c.value_sats() for c in coins])
        coins_cnt = len(coins)
        denoms_amounts = []
        denoms_val = 0
        approx_found = False

        while not approx_found:
            cur_approx_amounts = []

            for dval in PS_DENOMS_VALS:
                for dn in range(11):  # max 11 values of same denom
                    all_denoms_amounts = denoms_amounts + [cur_approx_amounts]
                    txs_fee = self._calc_denoms_amounts_fee(coins_cnt,
                                                            all_denoms_amounts,
                                                            fee_per_kb)
                    min_total = denoms_val + dval + COLLATERAL_VAL + txs_fee
                    max_total = min_total - COLLATERAL_VAL + MAX_COLLATERAL_VAL
                    if min_total < coins_val:
                        denoms_val += dval
                        cur_approx_amounts.append(dval)
                        if max_total > coins_val:
                            approx_found = True
                            break
                    else:
                        if dval == MIN_DENOM_VAL:
                            approx_found = True
                        break
                if approx_found:
                    break
            if cur_approx_amounts:
                denoms_amounts.append(cur_approx_amounts)
        if denoms_amounts:
            for collateral_val in CREATE_COLLATERAL_VALS[::-1]:
                if coins_val - denoms_val - collateral_val > txs_fee:
                    denoms_amounts[0].insert(0, collateral_val)
                    break
            real_fee = coins_val - denoms_val - collateral_val
            assert real_fee - txs_fee < COLLATERAL_VAL, 'too high fee'
        return denoms_amounts

    def find_denoms_approx(self, need_amount):
        '''Find denoms amounts approximated to need_amount
        depending on calc_denoms_method'''
        if need_amount < COLLATERAL_VAL:
            return []
        if self.calc_denoms_method == self.CalcDenomsMethod.DEF:
            return self._find_denoms_approx_def(need_amount)
        else:
            return self._find_denoms_approx_abs(need_amount)

    def _find_denoms_approx_def(self, need_amount):
        '''Default method of calculating approximated denoms amounts.
        Produce max 11 denoms of same value to one new denoms transaction'''
        denoms_amounts = []
        denoms_total = 0
        approx_found = False

        while not approx_found:
            cur_approx_amounts = []

            for dval in PS_DENOMS_VALS:
                for dn in range(11):  # max 11 values of same denom
                    if denoms_total + dval > need_amount:
                        if dval == MIN_DENOM_VAL:
                            approx_found = True
                            denoms_total += dval
                            cur_approx_amounts.append(dval)
                        break
                    else:
                        denoms_total += dval
                        cur_approx_amounts.append(dval)
                if approx_found:
                    break

            denoms_amounts.append(cur_approx_amounts)
        return denoms_amounts

    def _find_denoms_approx_abs(self, need_amount):
        '''Absolute method of calculating denoms amounts.
        Produce all yet needed denoms amounts in one transaction'''
        if need_amount < MIN_DENOM_VAL:
            return []
        denoms_amounts = []
        cur_cnt = self.calc_denoms_by_values()
        abs_cnt = self.abs_denoms_cnt
        for d in PS_DENOMS_VALS:
            d_cur_cnt = cur_cnt.get(d, 0)
            d_abs_cnt = abs_cnt[d]
            if d_abs_cnt > d_cur_cnt:
                for i in range(d_abs_cnt-d_cur_cnt):
                    if need_amount >= d:
                        need_amount -= d
                        denoms_amounts.append(d)
        if not denoms_amounts:
            return []
        return [denoms_amounts]

    def denoms_to_mix(self, mix_rounds=None, denom_value=None):
        '''Return dict: outpoint => denom for denoms needed for 
        mixing to reach mix_rounds'''
        res = {}
        w = self.wallet
        if mix_rounds is not None:
            denoms = w.db.get_ps_denoms(min_rounds=mix_rounds,
                                        max_rounds=mix_rounds)
        else:
            denoms = w.db.get_ps_denoms(max_rounds=self.mix_rounds-1)
        for outpoint, denom in denoms.items():
            if denom_value is not None and denom_value != denom[1]:
                continue
            if not w.db.get_ps_spending_denom(outpoint):
                res.update({outpoint: denom})
        return res

    @property
    def min_new_denoms_from_coins_val(self):
        '''Get minimal funds value needed to create minimal new denoms tx'''
        fee_per_kb = self.config.fee_per_kb()
        # no change, one coin input, one 100001 out and 10000 collateral out
        new_denoms_fee = calc_tx_fee(1, 2, fee_per_kb, max_size=True)
        return new_denoms_fee + MIN_DENOM_VAL + COLLATERAL_VAL

    @property
    def min_new_collateral_from_coins_val(self):
        '''Get minimal funds value needed to create minimal new collateral tx'''
        fee_per_kb = self.config.fee_per_kb()
        # no change, one coin input, one 10000 output
        new_collateral_fee = calc_tx_fee(1, 1, fee_per_kb, max_size=True)
        return new_collateral_fee + COLLATERAL_VAL

    # Methods to check different tx types, add/rm ps data on these types
    def unpack_io_values(func):
        '''Decorator to prepare tx inputs/outputs info'''
        def func_wrapper(self, txid, tx, full_check=True):
            w = self.wallet
            inputs = []
            outputs = []
            icnt = mine_icnt = others_icnt = 0
            ocnt = op_return_ocnt = 0
            for i in tx.inputs():
                icnt += 1
                prev_h = i.prevout.txid.hex()
                prev_n = i.prevout.out_idx
                prev_tx = w.db.get_transaction(prev_h)
                tx_type = w.db.get_ps_tx(prev_h)[0]
                if prev_tx:
                    o = prev_tx.outputs()[prev_n]
                    if w.is_mine(o.address):  # mine
                        inputs.append((o, prev_h, prev_n, True, tx_type))
                        mine_icnt += 1
                    else:  # others
                        inputs.append((o, prev_h, prev_n, False, tx_type))
                        others_icnt += 1
                else:  # possible others
                    inputs.append((None, prev_h, prev_n, False, tx_type))
                    others_icnt += 1
            for idx, o in enumerate(tx.outputs()):
                ocnt += 1
                if o.address is None and o.scriptpubkey.hex() == '6a':
                    op_return_ocnt += 1
                outputs.append((o, txid, idx))
            io_values = (inputs, outputs,
                         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt)
            return func(self, txid, tx, io_values, full_check)
        return func_wrapper

    def _add_spent_ps_outpoints_ps_data(self, txid, tx):
        '''Add spent_denoms/spent_collateral/spent_others to ps data
        on PS transaction adding to wallet'''
        w = self.wallet
        spent_ps_addrs = set()
        spent_outpoints = []
        for txin in tx.inputs():
            spent_outpoint = txin.prevout.to_str()
            spent_outpoints.append(spent_outpoint)

            with self.denoms_lock:
                spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
                if not spent_denom:
                    spent_denom = w.db.get_ps_denom(spent_outpoint)
                    if spent_denom:
                        w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                        spent_ps_addrs.add(spent_denom[0])
                self.pop_ps_denom(spent_outpoint)
            # cleanup of denominate wfl will be done on timeout

            with self.collateral_lock:
                spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
                if not spent_collateral:
                    spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                    if spent_collateral:
                        w.db.add_ps_spent_collateral(spent_outpoint,
                                                     spent_collateral)
                        spent_ps_addrs.add(spent_collateral[0])
                w.db.pop_ps_collateral(spent_outpoint)
            # cleanup of pay collateral wfl
            uuid = w.db.get_ps_spending_collateral(spent_outpoint)
            if uuid:
                self._cleanup_pay_collateral_wfl(force=True)

            with self.others_lock:
                spent_other = w.db.get_ps_spent_other(spent_outpoint)
                if not spent_other:
                    spent_other = w.db.get_ps_other(spent_outpoint)
                    if spent_other:
                        w.db.add_ps_spent_other(spent_outpoint, spent_other)
                        spent_ps_addrs.add(spent_other[0])
                w.db.pop_ps_other(spent_outpoint)

        self.add_spent_addrs(spent_ps_addrs)
        for addr, data in list(w.db.get_ps_reserved().items()):
            if data in spent_outpoints:
                self.pop_ps_reserved(addr)

    def _rm_spent_ps_outpoints_ps_data(self, txid, tx):
        '''Remove spent_denoms/spent_collateral/spent_others to ps data
        on PS transaction removing from wallet'''
        w = self.wallet
        restored_ps_addrs = set()
        for txin in tx.inputs():
            restore_prev_h = txin.prevout.txid.hex()
            restore_outpoint = txin.prevout.to_str()
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            with self.denoms_lock:
                if not tx_type:
                    restore_denom = w.db.get_ps_denom(restore_outpoint)
                    if not restore_denom:
                        restore_denom = \
                            w.db.get_ps_spent_denom(restore_outpoint)
                        if restore_denom:
                            self.add_ps_denom(restore_outpoint, restore_denom)
                            restored_ps_addrs.add(restore_denom[0])
                w.db.pop_ps_spent_denom(restore_outpoint)

            with self.collateral_lock:
                if not tx_type:
                    restore_collateral = \
                        w.db.get_ps_collateral(restore_outpoint)
                    if not restore_collateral:
                        restore_collateral = \
                            w.db.get_ps_spent_collateral(restore_outpoint)
                        if restore_collateral:
                            w.db.add_ps_collateral(restore_outpoint,
                                                   restore_collateral)
                            restored_ps_addrs.add(restore_collateral[0])
                w.db.pop_ps_spent_collateral(restore_outpoint)

            with self.others_lock:
                if not tx_type:
                    restore_other = w.db.get_ps_other(restore_outpoint)
                    if not restore_other:
                        restore_other = \
                            w.db.get_ps_spent_other(restore_outpoint)
                        if restore_other:
                            w.db.add_ps_other(restore_outpoint, restore_other)
                            restored_ps_addrs.add(restore_other[0])
                w.db.pop_ps_spent_other(restore_outpoint)
        self.restore_spent_addrs(restored_ps_addrs)

    @unpack_io_values
    def _check_new_denoms_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with new denoms transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        if not full_check:
            return

        collateral_cnt = 0
        denoms_cnt = 0
        last_denom_val = MIN_DENOM_VAL  # must start with minimal denom

        txin0_addr = inputs[0][0].address
        txin0_tx_type = inputs[0][4]
        change_cnt = sum([1 if o.address == txin0_addr else 0
                          for o, prev_h, prev_n in outputs])
        change_cnt2 = sum([1 if o.value not in PS_VALS else 0
                           for o, prev_h, prev_n in outputs])
        change_cnt = max(change_cnt, change_cnt2)
        if change_cnt > 1:
            return 'Excess change outputs'

        for i, (o, prev_h, prev_n) in enumerate(outputs):
            addr = o.address
            if addr == txin0_addr:
                continue
            val = o.value
            if val in CREATE_COLLATERAL_VALS:
                if self.ps_keystore_has_history and not self.is_ps_ks(addr):
                    return 'Destination is not on ps_keystore'
                if collateral_cnt > 0:
                    return f'Excess collateral output i={i}'
                else:
                    if val == CREATE_COLLATERAL_VAL:
                        collateral_cnt += 1
                    elif icnt > 1:
                        return 'This type of tx must have one input'
                    elif txin0_tx_type not in [PSTxTypes.OTHER_PS_COINS,
                                               PSTxTypes.NEW_DENOMS,
                                               PSTxTypes.DENOMINATE]:
                        return ('This type of tx must have input from'
                                ' ps other coins/new denoms/denominate txs')
                    else:
                        collateral_cnt += 1
            elif val in PS_DENOMS_VALS:
                if self.ps_keystore_has_history and not self.is_ps_ks(addr):
                    return 'Destination is not on ps_keystore'
                if val < last_denom_val:  # must increase or be the same
                    return (f'Unsuitable denom value={val}, must be'
                            f' {last_denom_val} or greater')
                elif val > last_denom_val:
                    last_denom_val = val
                denoms_cnt += 1
            else:
                return f'Unsuitable output value={val}'
        if denoms_cnt < 1:
            return 'Transaction has no denom outputs'

    def _add_new_denoms_ps_data(self, txid, tx):
        '''Save PS data when adding new denoms tx to the wallet'''
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        new_outpoints = []
        new_others_outpoints = []
        txin0 = copy.deepcopy(tx.inputs()[0])
        txin0_addr = w.get_txin_address(txin0)
        origin_addrs = {txin0_addr}
        for txin in tx.inputs()[1:]:
            origin_addrs.add(w.get_txin_address(txin))
        origin_addrs = list(origin_addrs)
        for i, o in enumerate(outputs):
            addr = o.address
            val = o.value
            new_outpoint = f'{txid}:{i}'
            if addr == txin0_addr:
                txin0_outpoint = txin0.prevout.to_str()
                if (w.db.get_ps_spent_denom(txin0_outpoint)
                        or w.db.get_ps_spent_collateral(txin0_outpoint)
                        or w.db.get_ps_spent_other(txin0_outpoint)):
                    new_others_outpoints.append((new_outpoint, addr, val))
            elif val in PS_VALS:
                new_outpoints.append((new_outpoint, addr, val))
            else:
                raise AddPSDataError(f'Illegal value: {val}'
                                     f' in new denoms tx')
        with self.denoms_lock, self.collateral_lock:
            for new_outpoint, addr, val in new_outpoints:
                if val in CREATE_COLLATERAL_VALS:  # collaterral
                    new_collateral = (addr, val)
                    w.db.add_ps_collateral(new_outpoint, new_collateral)
                else:  # denom round 0
                    new_denom = (addr, val, 0)
                    self.add_ps_denom(new_outpoint, new_denom)
            w.db.add_ps_origin_addrs(txid, origin_addrs)
        with self.others_lock:
            for new_outpoint, addr, val in new_others_outpoints:
                w.db.add_ps_other(new_outpoint, (addr, val))

    def _rm_new_denoms_ps_data(self, txid, tx):
        '''Remove PS data when new denoms tx is removed from the wallet'''
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        rm_outpoints = []
        rm_others_outpoints = []
        txin0 = copy.deepcopy(tx.inputs()[0])
        txin0_addr = w.get_txin_address(txin0)
        for i, o in enumerate(outputs):
            addr = o.address
            val = o.value
            rm_outpoint = f'{txid}:{i}'
            if addr == txin0_addr:
                txin0_outpoint = txin0.prevout.to_str()
                if (w.db.get_ps_spent_denom(txin0_outpoint)
                        or w.db.get_ps_spent_collateral(txin0_outpoint)
                        or w.db.get_ps_spent_other(txin0_outpoint)):
                    rm_others_outpoints.append(rm_outpoint)
            elif val in PS_VALS:
                rm_outpoints.append((rm_outpoint, val))
        with self.denoms_lock, self.collateral_lock:
            for rm_outpoint, val in rm_outpoints:
                if val in CREATE_COLLATERAL_VALS:  # collaterral
                    w.db.pop_ps_collateral(rm_outpoint)
                else:  # denom round 0
                    self.pop_ps_denom(rm_outpoint)
            w.db.pop_ps_origin_addrs(txid)
        with self.others_lock:
            for rm_outpoint in rm_others_outpoints:
                w.db.pop_ps_other(rm_outpoint)

    @unpack_io_values
    def _check_new_collateral_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with new collateral transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'
        if ocnt > 2:
            return 'Transaction has wrong outputs count'

        collateral_cnt = 0

        txin0_addr = inputs[0][0].address
        change_cnt = sum([1 if o.address == txin0_addr else 0
                          for o, prev_h, prev_n in outputs])
        change_cnt2 = sum([1 if o.value not in CREATE_COLLATERAL_VALS else 0
                           for o, prev_h, prev_n in outputs])
        change_cnt = max(change_cnt, change_cnt2)
        if change_cnt > 1:
            return 'Excess change outputs'

        for i, (o, prev_h, prev_n) in enumerate(outputs):
            addr = o.address
            if addr == txin0_addr:
                continue
            val = o.value
            if val in CREATE_COLLATERAL_VALS:
                if self.ps_keystore_has_history and not self.is_ps_ks(addr):
                    return 'Destination is not on ps_keystore'
                if collateral_cnt > 0:
                    return f'Excess collateral output i={i}'
                else:
                    if val == CREATE_COLLATERAL_VAL:
                        collateral_cnt += 1
                    elif change_cnt > 0:
                        return 'This type of tx must have no change'
                    elif icnt > 1:
                        return 'This type of tx must have one input'
                    else:
                        collateral_cnt += 1
            else:
                return f'Unsuitable output value={val}'
        if collateral_cnt < 1:
            return 'Transaction has no collateral outputs'

    def _add_new_collateral_ps_data(self, txid, tx):
        '''Save PS data when adding new collateral tx to the wallet'''
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        new_outpoints = []
        new_others_outpoints = []
        txin0 = copy.deepcopy(tx.inputs()[0])
        txin0_addr = w.get_txin_address(txin0)
        origin_addrs = {txin0_addr}
        for txin in tx.inputs()[1:]:
            origin_addrs.add(w.get_txin_address(txin))
        origin_addrs = list(origin_addrs)
        for i, o in enumerate(outputs):
            addr = o.address
            val = o.value
            new_outpoint = f'{txid}:{i}'
            if addr == txin0_addr:
                txin0_outpoint = txin0.prevout.to_str()
                if (w.db.get_ps_spent_denom(txin0_outpoint)
                        or w.db.get_ps_spent_collateral(txin0_outpoint)
                        or w.db.get_ps_spent_other(txin0_outpoint)):
                    new_others_outpoints.append((new_outpoint, addr, val))
            elif val in CREATE_COLLATERAL_VALS:
                new_outpoints.append((new_outpoint, addr, val))
            else:
                raise AddPSDataError(f'Illegal value: {val}'
                                     f' in new collateral tx')
        with self.collateral_lock:
            for new_outpoint, addr, val in new_outpoints:
                new_collateral = (addr, val)
                w.db.add_ps_collateral(new_outpoint, new_collateral)
            w.db.add_ps_origin_addrs(txid, origin_addrs)
        with self.others_lock:
            for new_outpoint, addr, val in new_others_outpoints:
                w.db.add_ps_other(new_outpoint, (addr, val))

    def _rm_new_collateral_ps_data(self, txid, tx):
        '''Remove PS data when new collateral tx is removed from the wallet'''
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        rm_outpoints = []
        rm_others_outpoints = []
        txin0 = copy.deepcopy(tx.inputs()[0])
        txin0_addr = w.get_txin_address(txin0)
        for i, o in enumerate(outputs):
            addr = o.address
            val = o.value
            rm_outpoint = f'{txid}:{i}'
            if addr == txin0_addr:
                txin0_outpoint = txin0.prevout.to_str()
                if (w.db.get_ps_spent_denom(txin0_outpoint)
                        or w.db.get_ps_spent_collateral(txin0_outpoint)
                        or w.db.get_ps_spent_other(txin0_outpoint)):
                    rm_others_outpoints.append(rm_outpoint)
            elif val in CREATE_COLLATERAL_VALS:
                rm_outpoints.append(rm_outpoint)
        with self.collateral_lock:
            for rm_outpoint in rm_outpoints:
                w.db.pop_ps_collateral(rm_outpoint)
            w.db.pop_ps_origin_addrs(txid)
        with self.others_lock:
            for rm_outpoint in rm_others_outpoints:
                w.db.pop_ps_other(rm_outpoint)

    @unpack_io_values
    def _check_pay_collateral_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with pay collateral transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt != 1:
            return 'Transaction has wrong inputs count'
        if ocnt != 1:
            return 'Transaction has wrong outputs count'

        i, i_prev_h, i_prev_n, is_mine, tx_type = inputs[0]
        if i.value not in CREATE_COLLATERAL_VALS:
            return 'Wrong collateral amount'

        o, o_prev_h, o_prev_n = outputs[0]
        if o.address is None and o.scriptpubkey.hex() == '6a':
            if o.value != 0:
                return 'Wrong output collateral amount'
        else:
            if o.value not in CREATE_COLLATERAL_VALS[:-1]:
                return 'Wrong output collateral amount'
        if o.value != i.value - COLLATERAL_VAL:
            return 'Wrong output collateral amount'

        if not full_check:
            return

        w = self.wallet
        if not self.ps_collateral_cnt:
            return 'Collateral amount not ready'
        outpoint = f'{i_prev_h}:{i_prev_n}'
        ps_collateral = w.db.get_ps_collateral(outpoint)
        if not ps_collateral:
            return 'Collateral amount not found'

    def _add_pay_collateral_ps_data(self, txid, tx):
        '''Save PS data when adding pay collateral tx to the wallet'''
        w = self.wallet
        in0 = tx.inputs()[0]
        spent_outpoint = in0.prevout.to_str()
        spent_ps_addrs = set()
        with self.collateral_lock:
            spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
            if not spent_collateral:
                spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                if not spent_collateral:
                    raise AddPSDataError(f'ps_collateral {spent_outpoint}'
                                         f' not found')
            w.db.add_ps_spent_collateral(spent_outpoint, spent_collateral)
            spent_ps_addrs.add(spent_collateral[0])
            w.db.pop_ps_collateral(spent_outpoint)
            self.add_spent_addrs(spent_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if out0.scriptpubkey.hex() != '6a' and addr is not None:
                new_outpoint = f'{txid}:{0}'
                new_collateral = (addr, out0.value)
                w.db.add_ps_collateral(new_outpoint, new_collateral)
                self.pop_ps_reserved(addr)
                # add change address to not wait on wallet.synchronize_sequence
                if self.ps_keystore:
                    limit = self.gap_limit_for_change
                    addrs = self.get_change_addresses()
                    last_few_addrs = addrs[-limit:]
                    found_hist = False
                    for ch_addr in last_few_addrs:
                        if w.db.get_addr_history(ch_addr):
                            found_hist = True
                            break
                    if found_hist:
                        self.create_new_address(for_change=True)
                elif hasattr(w, '_unused_change_addresses'):
                    # _unused_change_addresses absent on wallet startup and
                    # wallet.create_new_address fails in that case
                    limit = w.gap_limit_for_change
                    addrs = w.get_change_addresses()
                    last_few_addrs = addrs[-limit:]
                    if any(map(w.db.get_addr_history, last_few_addrs)):
                        w.create_new_address(for_change=True)

    def _rm_pay_collateral_ps_data(self, txid, tx):
        '''Remove PS data when pay collateral tx is removed from the wallet'''
        w = self.wallet
        in0 = tx.inputs()[0]
        restore_prev_h = in0.prevout.txid.hex()
        restore_outpoint = in0.prevout.to_str()
        restored_ps_addrs = set()
        with self.collateral_lock:
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            if not tx_type:
                restore_collateral = w.db.get_ps_collateral(restore_outpoint)
                if not restore_collateral:
                    restore_collateral = \
                        w.db.get_ps_spent_collateral(restore_outpoint)
                    if not restore_collateral:
                        raise RmPSDataError(f'ps_spent_collateral'
                                            f' {restore_outpoint} not found')
                w.db.add_ps_collateral(restore_outpoint, restore_collateral)
                restored_ps_addrs.add(restore_collateral[0])
            w.db.pop_ps_spent_collateral(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if out0.scriptpubkey.hex() != '6a' and addr is not None:
                rm_outpoint = f'{txid}:{0}'
                self.add_ps_reserved(addr, restore_outpoint)
                w.db.pop_ps_collateral(rm_outpoint)

    @unpack_io_values
    def _check_denominate_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with denominate transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if icnt != ocnt:
            return 'Transaction has different count of inputs/outputs'
        if icnt < self.pool_min_participants:
            return 'Transaction has too small count of inputs/outputs'
        if icnt > self.pool_max_participants * PRIVATESEND_ENTRY_MAX_SIZE:
            return 'Transaction has too many count of inputs/outputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'

        denom_val = None
        for i, prev_h, prev_n, is_mine, tx_type in inputs:
            if not is_mine:
                continue
            if denom_val is None:
                denom_val = i.value
                if denom_val not in PS_DENOMS_VALS:
                    return f'Unsuitable input value={denom_val}'
            elif i.value != denom_val:
                return f'Unsuitable input value={i.value}'
        for o, prev_h, prev_n in outputs:
            if o.value != denom_val:
                return f'Unsuitable output value={o.value}'

        if not full_check:
            return

        w = self.wallet
        # additional check with is_mine for find untracked
        if self.state not in self.mixing_running_states:
            mine_icnt = mine_ocnt = 0
            for txin in tx.inputs():
                addr = w.get_txin_address(txin)
                if w.is_mine(addr):
                    mine_icnt += 1
            for o in tx.outputs():
                addr = o.address
                if w.is_mine(addr):
                    mine_ocnt += 1
            if mine_icnt != mine_ocnt:
                return f'Differ mine_icnt/mine_ocnt: {mine_icnt}/{mine_ocnt}'

        for i, prev_h, prev_n, is_mine, tx_type in inputs:
            if not is_mine:
                continue
            denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
            if not denom:
                return 'Transaction input not found in ps_denoms'

    def _check_denominate_tx_io_on_wfl(self, txid, tx, wfl):
        '''Check tx with txid is matched again denominate workflow wfl'''
        w = self.wallet
        icnt = 0
        ocnt = 0
        for i, txin in enumerate(tx.inputs()):
            txin = copy.deepcopy(txin)
            addr = w.get_txin_address(txin)
            if not w.is_mine(addr):
                continue
            outpoint = txin.prevout.to_str()
            if outpoint in wfl.inputs:
                icnt += 1
        for i, o in enumerate(tx.outputs()):
            if o.value != wfl.denom:
                return False
            if o.address in wfl.outputs:
                ocnt += 1
        if icnt > 0 and ocnt == icnt:
            return True
        else:
            return False

    def _calc_rounds_for_denominate_tx(self, new_outpoints, input_rounds):
        '''Calculate output rounds for PS denoms data for denominate tx.
        If wallet have HW keystore then need to check output addresses from
        that keystore and if presented put max rounds to that denoms,
        as mixed round denom is placed on hw keystore'''
        output_rounds = list(map(lambda x: x+1, input_rounds[:]))
        if self.is_hw_ks:
            max_round = max(output_rounds)
            min_round = min(output_rounds)
            if min_round < max_round:
                hw_addrs_idxs = []
                for i, (new_outpoint, addr, value) in enumerate(new_outpoints):
                    if not self.is_ps_ks(addr):
                        hw_addrs_idxs.append(i)
                if hw_addrs_idxs:
                    max_round_idxs = []
                    for i, r in enumerate(output_rounds):
                        if r == max_round:
                            max_round_idxs.append(i)
                    res_rounds = [r for r in output_rounds if r < max_round]
                    while max_round_idxs:
                        r = output_rounds[max_round_idxs.pop(0)]
                        if hw_addrs_idxs:
                            i = hw_addrs_idxs.pop(0)
                            res_rounds.insert(i, r)
                        else:
                            res_rounds.append(r)
                    output_rounds = res_rounds[:]
        return output_rounds

    def _add_denominate_ps_data(self, txid, tx):
        '''Save PS data when adding denominate tx to the wallet'''
        w = self.wallet
        spent_outpoints = []
        for txin in tx.inputs():
            txin = copy.deepcopy(txin)
            addr = w.get_txin_address(txin)
            if not w.is_mine(addr):
                continue
            spent_outpoint = txin.prevout.to_str()
            spent_outpoints.append(spent_outpoint)

        new_outpoints = []
        for i, o in enumerate(tx.outputs()):
            addr = o.address
            if not w.is_mine(addr):
                continue
            new_outpoints.append((f'{txid}:{i}', addr, o.value))

        input_rounds = []
        spent_ps_addrs = set()
        with self.denoms_lock:
            for spent_outpoint in spent_outpoints:
                spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
                if not spent_denom:
                    spent_denom = w.db.get_ps_denom(spent_outpoint)
                    if not spent_denom:
                        raise AddPSDataError(f'ps_denom {spent_outpoint}'
                                             f' not found')
                w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                spent_ps_addrs.add(spent_denom[0])
                self.pop_ps_denom(spent_outpoint)
                input_rounds.append(spent_denom[2])
            self.add_spent_addrs(spent_ps_addrs)

            output_rounds = self._calc_rounds_for_denominate_tx(new_outpoints,
                                                                input_rounds)
            for i, (new_outpoint, addr, value) in enumerate(new_outpoints):
                new_denom = (addr, value, output_rounds[i])
                self.add_ps_denom(new_outpoint, new_denom)
                self.pop_ps_reserved(addr)

    def _rm_denominate_ps_data(self, txid, tx):
        '''Remove PS data when denominate tx is removed from the wallet'''
        w = self.wallet
        restore_outpoints = []
        for txin in tx.inputs():
            txin = copy.deepcopy(txin)
            addr = w.get_txin_address(txin)
            if not w.is_mine(addr):
                continue
            restore_prev_h = txin.prevout.txid.hex()
            restore_outpoint = txin.prevout.to_str()
            restore_outpoints.append((restore_outpoint, restore_prev_h))

        rm_outpoints = []
        for i, o in enumerate(tx.outputs()):
            addr = o.address
            if not w.is_mine(addr):
                continue
            rm_outpoints.append((f'{txid}:{i}', addr))

        restored_ps_addrs = set()
        with self.denoms_lock:
            for restore_outpoint, restore_prev_h in restore_outpoints:
                tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
                if not tx_type:
                    restore_denom = w.db.get_ps_denom(restore_outpoint)
                    if not restore_denom:
                        restore_denom = \
                            w.db.get_ps_spent_denom(restore_outpoint)
                        if not restore_denom:
                            raise RmPSDataError(f'ps_denom {restore_outpoint}'
                                                f' not found')
                    self.add_ps_denom(restore_outpoint, restore_denom)
                    restored_ps_addrs.add(restore_denom[0])
                w.db.pop_ps_spent_denom(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            for i, (rm_outpoint, addr) in enumerate(rm_outpoints):
                self.add_ps_reserved(addr, restore_outpoints[i][0])
                self.pop_ps_denom(rm_outpoint)

    @unpack_io_values
    def _check_other_ps_coins_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with others PS coins transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values

        w = self.wallet
        for o, prev_h, prev_n in outputs:
            addr = o.address
            if addr in w.db.get_ps_addresses():
                return
        return 'Transaction has no outputs with ps denoms/collateral addresses'

    @unpack_io_values
    def _check_privatesend_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with others PrivateSend transactions pattern'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if ocnt != 1:
            return 'Transaction has wrong count of outputs'

        w = self.wallet
        for i, prev_h, prev_n, is_mine, tx_type in inputs:
            if i.value not in PS_DENOMS_VALS:
                return f'Unsuitable input value={i.value}'
            denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
            if not denom:
                return 'Transaction input not found in ps_denoms'
            if denom[2] < self.min_mix_rounds:
                return 'Transaction input mix_rounds too small'

    @unpack_io_values
    def _check_spend_ps_coins_tx_err(self, txid, tx, io_values, full_check):
        '''Check tx with txid and unpacked io_values is comply
        with Spend ps coins transactions pattern (mixed PS and regular coins
        or needed number of rounds is not reached in PS coins'''
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        w = self.wallet
        for i, prev_h, prev_n, is_mine, tx_type in inputs:
            spent_outpoint = f'{prev_h}:{prev_n}'
            if w.db.get_ps_denom(spent_outpoint):
                return
            if w.db.get_ps_collateral(spent_outpoint):
                return
            if w.db.get_ps_other(spent_outpoint):
                return
        return 'Transaction has no inputs from ps denoms/collaterals/others'

    def _add_spend_ps_coins_ps_data(self, txid, tx):
        '''Save PS data when adding Spend PS coins tx to the wallet'''
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        ps_addrs = w.db.get_ps_addresses()
        new_others = []
        for i, o in enumerate(tx.outputs()):  # check to add ps_others
            addr = o.address
            if addr in ps_addrs:
                new_others.append((f'{txid}:{i}', addr, o.value))
        with self.others_lock:
            for new_outpoint, addr, value in new_others:
                new_other = (addr, value)
                w.db.add_ps_other(new_outpoint, new_other)

    def _rm_spend_ps_coins_ps_data(self, txid, tx):
        '''Remove PS data when new denoms tx is removed from the wallet'''
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        ps_addrs = w.db.get_ps_addresses()
        rm_others = []
        for i, o in enumerate(tx.outputs()):  # check to rm ps_others
            addr = o.address
            if addr in ps_addrs:
                rm_others.append(f'{txid}:{i}')
        with self.others_lock:
            for rm_outpoint in rm_others:
                w.db.pop_ps_other(rm_outpoint)

    def _check_ps_tx_type(self, txid, tx,
                          find_untracked=False, last_iteration=False):
        '''Check tx with txid to known PS tx types and return found type'''
        if find_untracked and last_iteration:
            err = self._check_other_ps_coins_tx_err(txid, tx)
            if not err:
                return PSTxTypes.OTHER_PS_COINS
            else:
                return STANDARD_TX

        if self._check_on_denominate_wfl(txid, tx):
            return PSTxTypes.DENOMINATE
        if self._check_on_pay_collateral_wfl(txid, tx):
            return PSTxTypes.PAY_COLLATERAL
        if self._check_on_new_collateral_wfl(txid, tx):
            return PSTxTypes.NEW_COLLATERAL
        if self._check_on_new_denoms_wfl(txid, tx):
            return PSTxTypes.NEW_DENOMS

        # OTHER_PS_COINS before PRIVATESEND and SPEND_PS_COINS
        # to prevent spending ps coins to ps addresses
        # Do not must happen if blocked in PSManager.broadcast_transaction
        err = self._check_other_ps_coins_tx_err(txid, tx)
        if not err:
            return PSTxTypes.OTHER_PS_COINS
        # PRIVATESEND before SPEND_PS_COINS as second pattern more relaxed
        err = self._check_privatesend_tx_err(txid, tx)
        if not err:
            return PSTxTypes.PRIVATESEND
        # SPEND_PS_COINS will be allowed when mixing is stopped
        err = self._check_spend_ps_coins_tx_err(txid, tx)
        if not err:
            return PSTxTypes.SPEND_PS_COINS

        return STANDARD_TX

    def _add_ps_data(self, txid, tx, tx_type):
        '''Save PS data of tx with txid/tx_type to the wallet.
        Do additional postprocessing depending of tx_type.'''
        w = self.wallet
        w.db.add_ps_tx(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._add_new_denoms_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_spendable_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._add_new_collateral_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_spendable_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._add_pay_collateral_ps_data(txid, tx)
            self._process_by_pay_collateral_wfl(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._add_denominate_ps_data(txid, tx)
            self._process_by_denominate_wfl(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
            # notify ui on ps other coins arrived
            self.postpone_notification('ps-other-coins-arrived', w, txid)
        else:
            raise AddPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx_removed(txid)
        w.db.add_ps_tx(txid, tx_type, completed=True)

        self._check_enough_sm_denoms(tx, tx_type)

    def _check_enough_sm_denoms(self, tx, tx_type):
        '''Check if the number of small denoms is enough to make PS transaction
        with a reasonable fee. If not then send a notification to GUI'''
        if self.calc_denoms_method == self.CalcDenomsMethod.ABS:
            return
        w = self.wallet
        check_denoms_by_vals = False
        if tx_type == PSTxTypes.NEW_DENOMS:
            txin0 = copy.deepcopy(tx.inputs()[0])
            txin0_addr = w.get_txin_address(txin0)
            if txin0_addr not in [o.address for o in tx.outputs()]:
                check_denoms_by_vals = True
        elif tx_type in [PSTxTypes.SPEND_PS_COINS, PSTxTypes.PRIVATESEND]:
            check_denoms_by_vals = True
        if check_denoms_by_vals:
            denoms_by_vals = self.calc_denoms_by_values()
            if denoms_by_vals:
                if not self.check_enough_sm_denoms(denoms_by_vals):
                    self.postpone_notification('ps-not-enough-sm-denoms',
                                               w, denoms_by_vals)

    def _add_tx_ps_data(self, txid, tx):
        '''Save PS data when adding tx with txid to the wallet.
        Used from AddressSynchronizer.add_transaction'''
        if self.state not in [PSStates.Mixing, PSStates.StopMixing]:
            return
        w = self.wallet
        tx_type, completed = w.db.get_ps_tx(txid)
        if tx_type and completed:  # ps data already exists
            return
        if not tx_type:  # try to find type in removed ps txs
            tx_type, completed = w.db.get_ps_tx_removed(txid)
            if tx_type:
                self.logger.info(f'_add_tx_ps_data: matched removed tx {txid}')
        if not tx_type:  # check possible types from workflows and patterns
            tx_type = self._check_ps_tx_type(txid, tx)
        if not tx_type:
            return
        self._add_tx_type_ps_data(txid, tx, tx_type)

    def _add_tx_type_ps_data(self, txid, tx, tx_type):
        '''Save PS data when adding tx with txid
        and known tx_type to the wallet.'''
        w = self.wallet
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                type_name = SPEC_TX_NAMES[tx_type]
                self._add_ps_data(txid, tx, tx_type)
                self.last_mixed_tx_time = time.time()
                self.logger.debug(f'_add_tx_type_ps_data {txid}, {type_name}')
                self.postpone_notification('ps-data-changes', w)
            except Exception as e:
                self.logger.info(f'_add_ps_data {txid} failed: {str(e)}')
                if tx_type in [PSTxTypes.NEW_COLLATERAL, PSTxTypes.NEW_DENOMS]:
                    # this two tx types added during wfl creation process
                    raise
                if tx_type in [PSTxTypes.PAY_COLLATERAL, PSTxTypes.DENOMINATE]:
                    # this two tx types added from network
                    msg = self.ADD_PS_DATA_ERR_MSG
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                    self.stop_mixing(msg)
        else:
            self.logger.info(f'_add_tx_type_ps_data: {txid}'
                             f' unknown type {tx_type}')

    def _rm_ps_data(self, txid, tx, tx_type):
        '''Remove PS data from wallet for removed tx with txid
        and known tx type.'''
        w = self.wallet
        w.db.add_ps_tx_removed(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._rm_new_denoms_ps_data(txid, tx)
            self._cleanup_new_denoms_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._rm_new_collateral_ps_data(txid, tx)
            self._cleanup_new_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._rm_pay_collateral_ps_data(txid, tx)
            self._cleanup_pay_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._rm_denominate_ps_data(txid, tx)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        else:
            raise RmPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx(txid)
        w.db.add_ps_tx_removed(txid, tx_type, completed=True)

    def _rm_tx_ps_data(self, txid):
        '''Remove PS data from the wallet.
        Used from AddressSynchronizer.remove_transaction'''
        w = self.wallet
        tx = w.db.get_transaction(txid)
        if not tx:
            self.logger.info(f'_rm_tx_ps_data: {txid} not found')
            return

        tx_type, completed = w.db.get_ps_tx(txid)
        if not tx_type:
            return
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                self._rm_ps_data(txid, tx, tx_type)
                self.postpone_notification('ps-data-changes', w)
            except Exception as e:
                self.logger.info(f'_rm_ps_data {txid} failed: {str(e)}')
        else:
            self.logger.info(f'_rm_tx_ps_data: {txid} unknown type {tx_type}')


class PSKeystoreMixin:
    '''PrivateSend keystore functionality'''

    gap_limit = 20
    gap_limit_for_change = 10

    def __init__(self, wallet):
        self.ps_keystore = None
        self.ps_ks_txin_type = 'p2pkh'

    def copy_standard_bip32_keystore(self):
        '''Copy standard wallet bip32 keystore data to ps_keystore data.
        Place additional info and change type of keystore to ps_bip32'''
        w = self.wallet
        main_ks_copy = copy.deepcopy(dict(w.db.get('keystore')))
        main_ks_copy['type'] = 'ps_bip32'
        if self.ps_keystore:
            ps_ks_copy = copy.deepcopy(dict(w.db.get('ps_keystore')))
            addr_deriv_offset = ps_ks_copy.get('addr_deriv_offset', None)
            if addr_deriv_offset is not None:
                main_ks_copy['addr_deriv_offset'] = addr_deriv_offset
        w.db.put('ps_keystore', main_ks_copy)

    def load_ps_keystore(self):
        '''Load PS keystore from db and place instance to psman.ps_keystore'''
        w = self.wallet
        if 'ps_keystore' in w.db.data:
            self.ps_keystore = load_keystore(w.db, 'ps_keystore')

    def enable_ps_keystore(self):
        '''Load and synchronize PS keystore'''
        if self.w_type == 'standard':
            if self.w_ks_type == 'bip32':
                self.copy_standard_bip32_keystore()
                self.load_ps_keystore()
            elif self.is_hw_ks:
                self.load_ps_keystore()
        if self.ps_keystore:
            self.synchronize()

    def after_wallet_password_set(self, old_pw, new_pw):
        '''Copy PS keystore from standard wallet bip32 keystore
        on wallet password change'''
        if not self.ps_keystore:
            return
        if self.w_type == 'standard':
            if self.w_ks_type == 'bip32':
                self.copy_standard_bip32_keystore()
                self.load_ps_keystore()

    def create_ps_ks_from_seed_ext_password(self, seed, seed_ext, password):
        '''Create PS keystore for HW wallet from seed/see_ext/password'''
        if not self.is_hw_ks:
            raise Exception(f'can not create ps_keystore when main keystore'
                            f' type: "{self.w_ks_type}"')
        w = self.wallet
        if w.db.get('ps_keystore', {}):
            raise Exception('ps_keystore already exists')
        keystore = from_seed(seed, seed_ext, False)
        keystore.update_password(None, password)
        ps_keystore = keystore.dump()
        ps_keystore.update({'type': 'ps_bip32'})
        w.db.put('ps_keystore', ps_keystore)
        self.enable_ps_keystore()

    def is_ps_ks_encrypted(self):
        '''Check if PS keystore is encrypted'''
        if self.ps_keystore:
            try:
                self.ps_keystore.check_password(None)
                return False
            except:
                return True

    def need_password(self):
        '''Check if password is needed to sign wallet transactions.
        For HW wallets check PS Keystore needed password to sign txs.'''
        return (self.wallet.has_keystore_encryption()
                or self.is_hw_ks and self.is_ps_ks_encrypted())

    def update_ps_ks_password(self, old_pw, new_pw):
        '''Update PS keystore password for HW wallets'''
        if not self.is_hw_ks:
            raise Exception(f'can not create ps_keystore for main keystore'
                            f' type: "{self.w_ks_type}"')
        if old_pw is None and self.is_ps_ks_encrypted():
            raise InvalidPassword()
        self.ps_keystore.check_password(old_pw)

        if old_pw is None and new_pw:
            self.on_wallet_password_set()

        self.ps_keystore.update_password(old_pw, new_pw)
        self.wallet.db.put('ps_keystore', self.ps_keystore.dump())
        self.wallet.save_db()

    def is_ps_ks_inputs_in_tx(self, tx):
        '''Check inputs has at least one address from PS keystore'''
        for txin in tx.inputs():
            if self.is_ps_ks(txin.address):
                return True

    # Standard wallet methods on PS keystore
    def pubkeys_to_address(self, pubkeys):
        pubkey = pubkeys[0]
        return pubkey_to_address(self.ps_ks_txin_type, pubkey)

    def derive_pubkeys(self, c, i):
        return [self.ps_keystore.derive_pubkey(c, i).hex()]

    def derive_address(self, for_change, n):
        for_change = int(for_change)
        pubkeys = self.derive_pubkeys(for_change, n)
        return self.pubkeys_to_address(pubkeys)

    def get_address_index(self, address):
        return self.wallet.db.get_address_index(address, ps_ks=True)

    def is_ps_ks(self, address):
        return bool(self.wallet.db.get_address_index(address, ps_ks=True))

    def get_public_key(self, address):
        sequence = self.get_address_index(address)
        pubkeys = self.derive_pubkeys(*sequence)
        return pubkeys[0]

    def get_public_keys(self, address):
        return [self.get_public_key(address)]

    def check_address(self, addr):
        idx = self.get_address_index(addr)
        if addr and bool(idx):
            if addr != self.derive_address(*idx):
                raise PSKsInternalAddressCorruption()

    def get_address_path_str(self, address):
        intpath = self.get_address_index(address)
        if intpath is None:
            return None
        if self.ps_keystore:
            addr_deriv_offset = self.ps_keystore.addr_deriv_offset
            intpath = (addr_deriv_offset*2 + intpath[0], intpath[1])
        return convert_bip32_intpath_to_strpath(intpath)

    def create_new_address(self, for_change=False):
        assert type(for_change) is bool
        with self.wallet.lock:
            if for_change:
                n = self.wallet.db.num_change_addresses(ps_ks=True)
            else:
                n = self.wallet.db.num_receiving_addresses(ps_ks=True)
            address = self.derive_address(int(for_change), n)
            if for_change:
                self.wallet.db.add_change_address(address, ps_ks=True)
            else:
                self.wallet.db.add_receiving_address(address, ps_ks=True)
            self.wallet.add_address(address, ps_ks=True)  # addr synchronizer
            return address

    def synchronize_sequence(self, for_change):
        limit = self.gap_limit_for_change if for_change else self.gap_limit
        while True:
            if for_change:
                addrs = self.get_change_addresses()
            else:
                addrs = self.get_receiving_addresses()
            num_addrs = len(addrs)
            if num_addrs < limit:
                self.create_new_address(for_change)
                continue
            last_few_addresses = addrs[-limit:]
            if any(map(self.wallet.address_is_old, last_few_addresses)):
                self.create_new_address(for_change)
            else:
                break

    def synchronize(self):
        with self.wallet.lock:
            self.synchronize_sequence(False)
            self.synchronize_sequence(True)

    def get_all_known_addresses_beyond_gap_limit(self):
        # note that we don't stop at first large gap
        found = set()

        def process_addresses(addrs, gap_limit):
            rolling_num_unused = 0
            for addr in addrs:
                if self.wallet.db.get_addr_history(addr):
                    rolling_num_unused = 0
                else:
                    if rolling_num_unused >= gap_limit:
                        found.add(addr)
                    rolling_num_unused += 1

        process_addresses(self.get_receiving_addresses(), self.gap_limit)
        process_addresses(self.get_change_addresses(), self.gap_limit_for_change)
        return found

    def get_receiving_addresses(self, *, slice_start=None, slice_stop=None):
        return self.wallet.db.get_receiving_addresses(slice_start=slice_start,
                                                      slice_stop=slice_stop,
                                                      ps_ks=True)

    def get_change_addresses(self, *, slice_start=None, slice_stop=None):
        return self.wallet.db.get_change_addresses(slice_start=slice_start,
                                                   slice_stop=slice_stop,
                                                   ps_ks=True)

    def get_addresses(self):
        return self.get_receiving_addresses() + self.get_change_addresses()

    def get_unused_addresses(self, for_change=False):
        w = self.wallet
        if for_change:
            domain = self.get_change_addresses()
        else:
            domain = self.get_receiving_addresses()
        # TODO we should index receive_requests by id
        in_use_by_request = [k for k in w.receive_requests.keys()
                             if w.get_request_status(k) != PR_EXPIRED]
        in_use_by_request = set(in_use_by_request)
        ps_reserved = w.db.get_ps_reserved()
        tmp_reserved_addr = self.get_tmp_reserved_address()
        tmp_reserved_addrs = [tmp_reserved_addr] if tmp_reserved_addr else []
        return [addr for addr in domain if not w.is_used(addr)
                and addr not in in_use_by_request
                and addr not in ps_reserved
                and addr not in tmp_reserved_addrs]

    # Methods related to mixing on hw wallets
    def prepare_funds_from_hw_wallet(self):
        '''Calculate funds needed to transfer to PS keystore to mix,
        based on keep_amount/mix_rounds. Prepare and return signed
        transaction to send funds.'''
        try:
            w = self.wallet
            fee_per_kb = self.config.fee_per_kb()
            # calc amount need to be sent to ps_keystore
            with w._freeze_lock:
                frozen_addresses = w._frozen_addresses.copy()
            coins = w.get_utxos(None, excluded_addresses=frozen_addresses,
                                mature_only=True)
            coins = [c for c in coins if not w.is_frozen_coin(c)]
            coins_val = sum([c.value_sats() for c in coins])
            main_ks_coins = [c for c in coins if not c.is_ps_ks]
            main_ks_coins_val = sum([c.value_sats() for c in main_ks_coins])
            ps_ks_coins_val = sum([c.value_sats() for c in coins if c.is_ps_ks])

            outputs_amounts = self.calc_need_denoms_amounts(on_keep_amount=True)
            in_cnt = len(coins)
            total_need_val, outputs_amounts = \
                self._calc_total_need_val(in_cnt, outputs_amounts, fee_per_kb)
            transfer_tx_fee = calc_tx_fee(len(main_ks_coins), 1,
                                          fee_per_kb, max_size=True)
            if coins_val < total_need_val + transfer_tx_fee:  # transfer all
                need_transfer_val = main_ks_coins_val - transfer_tx_fee
            else:
                need_transfer_val = total_need_val - ps_ks_coins_val
            if need_transfer_val < PS_DENOMS_VALS[0]:
                return
            # prepare and send transaction to ps_keystore unused address
            unused = self.reserve_addresses(1, tmp=True)
            ps_ks_oaddr = unused[0]
            outputs = [PartialTxOutput.from_address_and_value(ps_ks_oaddr, need_transfer_val)]
            tx = w.make_unsigned_transaction(coins=main_ks_coins, outputs=outputs)
            tx = self.wallet.sign_transaction(tx, None)
            if tx and tx.is_complete():
                return tx
        except BaseException as e:
            self.logger.wfl_err(f'prepare_funds_from_hw_wallet: {str(e)}')

    async def _prepare_funds_from_hw_wallet(self):
        '''Async task which prepares funds from HW wallet to PS keystore'''
        while True:
            while self.new_denoms_wfl:
                await asyncio.sleep(5)  # wait for prev new denoms wfl finish
            tx = self.prepare_funds_from_hw_wallet()
            if tx:
                await self.broadcast_transaction(tx)
                self.logger.info(f'Broadcasted PS Keystore'
                                 f' fund tx {tx.txid()}')
                await asyncio.sleep(10)  # wait for new denoms wfl start
            await asyncio.sleep(5)  # wait for new coins on hw wallet

    def prepare_funds_from_ps_keystore(self, password):
        '''Prepare and sign transactions to send funds
        from PS keystore to HW wallet keystore'''
        w = self.wallet
        coins_ps = w.get_utxos(None, mature_only=True,
                               min_rounds=PSCoinRounds.MINUSINF)
        ps_ks_coins_ps = [c for c in coins_ps if c.is_ps_ks]
        coins_regular = w.get_utxos(None, mature_only=True)
        ps_ks_coins_regular = [c for c in coins_regular if c.is_ps_ks]
        if not ps_ks_coins_ps and not ps_ks_coins_regular:
            raise NotEnoughFunds('No funds found on PS Keystore')
        unused = w.get_unused_addresses()
        if not unused:
            raise NotEnoughFunds('No unused addresses to prepare transaction')
        res = []
        outputs_ps = [PartialTxOutput.from_address_and_value(unused[0], '!')]
        outputs_regular = [PartialTxOutput.from_address_and_value(unused[1], '!')]
        if ps_ks_coins_ps:
            tx = w.make_unsigned_transaction(coins=ps_ks_coins_ps, outputs=outputs_ps)
            tx = self.wallet.sign_transaction(tx, password)
            if tx and tx.is_complete():
                res.append(tx)
            else:
                raise Exception('Sign transaction failed')
        if ps_ks_coins_regular:
            tx = w.make_unsigned_transaction(coins=ps_ks_coins_regular,
                                             outputs=outputs_regular)
            tx = self.wallet.sign_transaction(tx, password)
            if tx and tx.is_complete():
                res.append(tx)
            else:
                raise Exception('Sign transaction failed')
        return res

    def check_funds_on_ps_keystore(self):
        '''Check funds left on PS keystore and can be returned
        to HW wallet keystore'''
        w = self.wallet
        coins = w.get_utxos(None, mature_only=True, include_ps=True)
        ps_ks_coins = [c for c in coins if c.is_ps_ks]
        if ps_ks_coins:
            return True
        else:
            return False
