# -*- coding: utf-8 -*-

import os
import ipaddress
import json
from bls_py import bls

from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot, QFileInfo
from PyQt5.QtWidgets import (QLineEdit, QComboBox, QListWidget, QDoubleSpinBox,
                             QAbstractItemView, QListWidgetItem, QWizardPage,
                             QRadioButton, QButtonGroup, QVBoxLayout, QLabel,
                             QGroupBox, QCheckBox, QPushButton, QGridLayout,
                             QFileDialog, QWizard)

from electrum_dash import constants
from electrum_dash import dash_tx
from electrum_dash.bitcoin import COIN, is_b58_address, b58_address_to_hash160
from electrum_dash.dash_tx import TxOutPoint, service_to_ip_port
from electrum_dash.protx import ProTxMN, ProTxService, ProRegTxExc
from electrum_dash.util import bfh, bh2u, FILE_OWNER_MODE
from electrum_dash.i18n import _

from .util import MONOSPACE_FONT, icon_path, read_QIcon, ButtonsLineEdit


def is_p2pkh_address(addr):
    if is_b58_address(addr):
        addrtype = b58_address_to_hash160(addr)[0]
        if addrtype == constants.net.ADDRTYPE_P2PKH:
            return True
    return False


class ValidationError(Exception): pass


class HwWarnError(Exception): pass


class UsedInWallet(Exception): pass


class SLineEdit(QLineEdit):
    '''QLineEdit with strip on text() method'''
    def text(self):
        return super().text().strip()

class ButtonsSLineEdit(SLineEdit, ButtonsLineEdit):
    '''QLineEdit with strip on text() method and buttons'''


class SComboBox(QComboBox):
    '''QComboBox with strip on currentText() method'''
    def currentText(self):
        return super().currentText().strip()


class OutputsList(QListWidget):
    '''Widget that displays available 1000 DASH outputs.'''
    outputSelected = pyqtSignal(object, name='outputSelected')
    def __init__(self, parent=None):
        super(OutputsList, self).__init__(parent)
        self.outputs = {}
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        sel_model = self.selectionModel()
        sel_model.selectionChanged.connect(self.on_selection_changed)

    def add_output(self, d):
        '''Add a valid output.'''
        label = d.prevout.to_str()
        self.outputs[label] = d
        item = QListWidgetItem(label)
        item.setFont(QFont(MONOSPACE_FONT))
        self.addItem(item)

    def add_outputs(self, outputs):
        list(map(self.add_output, outputs))

    def clear(self):
        super(OutputsList, self).clear()
        self.outputs.clear()

    def on_selection_changed(self, selected, deselected):
        '''Emit the selected output.'''
        items = self.selectedItems()
        if not items:
            return
        if not self.outputs:
            return
        self.outputSelected.emit(self.outputs[str(items[0].text())])


class OperationTypeWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(OperationTypeWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle(_('Operation type'))
        self.setSubTitle(_('Select operation type and ownership properties.'))

        self.rb_import = QRadioButton(_('Import and register legacy '
                                        'masternode.conf as DIP3 Masternode'))
        self.rb_create = QRadioButton(_('Create and register DIP3 Masternode'))
        self.rb_connect = QRadioButton(_('Connect to registered DIP3 '
                                         'Masternode'))
        self.rb_create.setChecked(True)
        self.rb_connect.setEnabled(False)
        self.button_group = QButtonGroup()
        self.button_group.buttonClicked.connect(self.on_operation_type_change)
        self.button_group.addButton(self.rb_import)
        self.button_group.addButton(self.rb_create)
        self.button_group.addButton(self.rb_connect)
        gb_vbox = QVBoxLayout()
        gb_vbox.addWidget(self.rb_create)
        gb_vbox.addWidget(self.rb_connect)
        gb_vbox.addWidget(self.rb_import)
        self.gb_create = QGroupBox(_('Select operation type'))
        self.gb_create.setLayout(gb_vbox)

        self.cb_owner = QCheckBox(_('I am an owner of this Masternode'))
        self.cb_operator = QCheckBox(_('I am an operator of this Masternode'))
        self.cb_owner.setChecked(True)
        self.cb_owner.stateChanged.connect(self.cb_state_changed)
        self.cb_operator.setChecked(True)
        self.cb_operator.stateChanged.connect(self.cb_state_changed)
        gb_vbox = QVBoxLayout()
        gb_vbox.addWidget(self.cb_owner)
        gb_vbox.addWidget(self.cb_operator)
        self.gb_owner = QGroupBox(_('Set ownership type'))
        self.gb_owner.setLayout(gb_vbox)

        layout = QVBoxLayout()
        layout.addWidget(self.gb_create)
        layout.addStretch(1)
        layout.addWidget(self.gb_owner)
        self.setLayout(layout)

    def on_operation_type_change(self, op_btn):
        if op_btn == self.rb_import:
            self.cb_owner.setChecked(True)
            self.cb_owner.setEnabled(False)
        else:
            self.cb_owner.setEnabled(True)

    def nextId(self):
        if self.rb_import.isChecked():
            return self.parent.IMPORT_LEGACY_PAGE
        elif not self.cb_owner.isChecked():
            return self.parent.SERVICE_PAGE
        else:
            return self.parent.COLLATERAL_PAGE

    @pyqtSlot()
    def cb_state_changed(self):
        self.completeChanged.emit()

    def isComplete(self):
        return self.cb_operator.isChecked() or self.cb_owner.isChecked()

    def validatePage(self):
        self.parent.new_mn = ProTxMN()
        self.parent.new_mn.alias = 'default'
        self.parent.new_mn.is_operated = self.cb_operator.isChecked()
        self.parent.new_mn.is_owned = self.cb_owner.isChecked()
        return True


class ImportLegacyWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(ImportLegacyWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle(_('Import Legacy masternode.conf'))
        self.setSubTitle(_('Select legacy masternode.conf to import.'))

        lmns = []
        self.lmns_cbox = SComboBox(self)
        self.lmns_dict = {}
        for i, lmn in enumerate(lmns):
            alias = lmn.get('alias', 'Unknown alias %s' % i)
            self.lmns_cbox.addItem(alias)
            self.lmns_dict[alias] = lmn
        self.lmns_cbox.currentIndexChanged.connect(self.on_change_lmn)
        self.imp_btn = QPushButton(_('Load from masternode.conf'))
        self.imp_btn.clicked.connect(self.load_masternode_conf)

        service_label = QLabel(_('Service:'))
        self.service = QLabel()
        collateral_val_label = QLabel(_('Collateral Outpoint Value:'))
        self.collateral_val = QLabel()
        self.collateral_value = None
        collateral_label = QLabel(_('Collateral Outpoint:'))
        self.collateral = QLabel()
        collateral_addr_label = QLabel(_('Collateral Address:'))
        self.collateral_addr = QLabel()
        self.err_label = QLabel(_('Error:'))
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()

        layout = QGridLayout()
        layout.addWidget(self.imp_btn, 0, 0, 1, -1)
        layout.addWidget(self.lmns_cbox, 1, 0, 1, -1)
        layout.setColumnStretch(2, 1)
        layout.addWidget(service_label, 3, 0)
        layout.addWidget(self.service, 3, 1)
        layout.addWidget(collateral_addr_label, 4, 0)
        layout.addWidget(self.collateral_addr, 4, 1)
        layout.addWidget(collateral_val_label, 5, 0)
        layout.addWidget(self.collateral_val, 5, 1)
        layout.addWidget(collateral_label, 6, 0)
        layout.addWidget(self.collateral, 6, 1)
        layout.addWidget(self.err_label, 8, 0)
        layout.addWidget(self.err, 8, 1)
        self.setLayout(layout)

    def initializePage(self):
        self.update_lmn_data(self.lmns_cbox.currentText())

    @pyqtSlot()
    def on_change_lmn(self):
        self.update_lmn_data(self.lmns_cbox.currentText())

    @pyqtSlot()
    def load_masternode_conf(self):
        dlg = QFileDialog
        conf_fname = dlg.getOpenFileName(self, _('Open masternode.conf'),
                                         '', 'Conf Files (*.conf)')[0]
        if not conf_fname:
            return

        try:
            with open(conf_fname, 'r') as f:
                conflines = f.readlines()
        except Exception:
            conflines = []
        if not conflines:
            return

        conflines = filter(lambda x: not x.startswith('#'),
                           [line.strip() for line in conflines])

        conflines = filter(lambda x: len(x.split()) == 5, conflines)
        res = []
        for line in conflines:
            res_d = {}
            alias, service, delegate, c_hash, c_index = line.split()

            res_d['alias'] = 'masternode.conf:%s' % alias
            try:
                ip, port = self.parent.validate_str_service(service)
                res_d['addr'] = {'ip': ip, 'port': int(port)}
                c_index = int(c_index)
            except Exception:
                continue
            res_d['vin'] = {
                'prevout_hash': c_hash,
                'prevout_n': c_index,
            }
            res.append(res_d)

        if not res:
            return
        else:
            res = sorted(res, key=lambda x: x.get('alias'))

        while True:
            idx = self.lmns_cbox.findText('masternode.conf:',
                                          Qt.MatchStartsWith)
            if idx < 0:
                break
            self.lmns_cbox.removeItem(idx)

        for i, r in enumerate(res):
            alias = r.get('alias')
            self.lmns_cbox.addItem(alias)
            if not i:
                first_alias = alias
            self.lmns_dict[alias] = r
        self.lmns_cbox.setFocus()
        first_alias_idx = self.lmns_cbox.findText(first_alias)
        self.lmns_cbox.setCurrentIndex(first_alias_idx)
        self.update_lmn_data(self.lmns_cbox.currentText())

    def update_lmn_data(self, current):
        if not current:
            return
        self.alias = current
        lmn = self.lmns_dict.get(current)
        if not lmn:
            return

        addr = lmn.get('addr', {})
        ip = addr.get('ip')
        port = addr.get('port')
        if addr and port:
            try:
                ip_check = ipaddress.ip_address(ip)
                if ip_check.version == 4:
                    service = '%s:%s' % (ip, port)
                else:
                    service = '[%s]:%s' % (ip, port)
            except ValueError:
                service = ''
        else:
            service = ''
        self.service.setText(service)

        vin = lmn.get('vin', {})
        address = vin.get('address')
        prevout_hash = vin.get('prevout_hash')
        prevout_n = vin.get('prevout_n')
        value = vin.get('value')

        if not address:
            wallet = self.parent.wallet
            coins = wallet.get_utxos(domain=None, excluded_addresses=None,
                                     mature_only=True,
                                     confirmed_funding_only=True)
            coins = filter(lambda x: (x.prevout.txid.hex() == prevout_hash
                                      and x.prevout.out_idx == prevout_n),
                           coins)
            coins = list(coins)
            if coins:
                address = coins[0].address
                value = coins[0].value_sats()
            else:
                address = ''
                value = 0

        if prevout_hash:
            val_dash = '%s DASH' % (value/COIN) if value else ''
            self.collateral_val.setText(val_dash)
            self.collateral_value = value
            self.collateral.setText('%s:%s' % (prevout_hash, prevout_n))
            self.collateral_addr.setText(address)
        else:
            self.collateral_val.setText('')
            self.collateral_value = None
            self.collateral.setText('')
            self.collateral_addr.setText('')
        self.completeChanged.emit()

    def isComplete(self):
        self.hide_error()
        if self.service.text() and self.collateral.text():
            return True
        return False

    def hide_error(self):
        self.err_label.hide()
        self.err.hide()

    def validatePage(self):
        try:
            ip, port = self.parent.validate_str_service(self.service.text())
            coll = self.parent.validate_collateral(self.collateral.text(),
                                                   self.collateral_addr.text(),
                                                   self.collateral_value)
        except ValidationError as e:
            self.err.setText(str(e))
            self.err_label.show()
            self.err.show()
            return False
        else:
            collateral_addr = self.collateral_addr.text()

        new_mn = self.parent.new_mn
        new_mn.alias = self.alias
        new_mn.collateral = TxOutPoint(bfh(coll[0])[::-1], coll[1])
        new_mn.service = ProTxService(ip, port)
        self.parent.collateral_addr = collateral_addr
        return True

    def nextId(self):
        return self.parent.SERVICE_PAGE


class SelectAddressesWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(SelectAddressesWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle(_('Select Addresses'))
        self.setSubTitle(_('Select Masternode owner/voting/payout addresses.'))
        wallet_is_p2sh = self.parent.wallet_is_p2sh
        layout = QGridLayout()
        self.o_addr_label = QLabel(_('Owner Address (P2PKH):'))
        if wallet_is_p2sh:
            self.o_addr = SLineEdit()
        else:
            self.o_addr_cb = SComboBox()
            self.o_addr_cb.setEditable(True)
            self.o_addr = self.o_addr_cb.lineEdit()
        self.o_addr.textChanged.connect(self.on_change_o_addr)
        self.o_err_label = QLabel(_('Error:'))
        self.o_err_label.setObjectName('err-label')
        self.o_err = QLabel()
        self.o_err.setObjectName('err-label')
        self.o_err_label.hide()
        self.o_err.hide()
        self.v_addr_label = QLabel(_('Voting Address (P2PKH):'))
        if wallet_is_p2sh:
            self.v_addr = SLineEdit()
        else:
            self.v_addr_cb = SComboBox()
            self.v_addr_cb.setEditable(True)
            self.v_addr = self.v_addr_cb.lineEdit()
        self.v_addr.textChanged.connect(self.on_change_v_addr)
        self.v_err_label = QLabel(_('Error:'))
        self.v_err_label.setObjectName('err-label')
        self.v_err = QLabel()
        self.v_err.setObjectName('err-label')
        self.v_err_label.hide()
        self.v_err.hide()
        self.p_addr_label = QLabel(_('Payout Address (must differ from '
                                     'owner/voting):'))
        self.p_addr_cb = SComboBox()
        self.p_addr_cb.setEditable(True)
        self.p_addr = self.p_addr_cb.lineEdit()
        self.p_addr.textChanged.connect(self.on_change_p_addr)
        self.p_err_label = QLabel(_('Error:'))
        self.p_err_label.setObjectName('err-label')
        self.p_err = QLabel()
        self.p_err.setObjectName('err-label')
        self.p_err_label.hide()
        self.p_err.hide()
        self.hw_err = QLabel()
        self.hw_err.setWordWrap(True)
        self.hw_err.setObjectName('err-label')
        self.hw_err.hide()
        self.cb_ignore = QCheckBox(_('Ignore and continue.'))
        self.cb_ignore.hide()

        layout.addWidget(self.o_addr_label, 0, 0, 1, -1)
        if wallet_is_p2sh:
            layout.addWidget(self.o_addr, 1, 0, 1, -1)
        else:
            layout.addWidget(self.o_addr_cb, 1, 0, 1, -1)
        layout.addWidget(self.o_err_label, 2, 0)
        layout.addWidget(self.o_err, 2, 1, 1, -1)
        layout.addWidget(self.v_addr_label, 3, 0, 1, -1)
        if wallet_is_p2sh:
            layout.addWidget(self.v_addr, 4, 0, 1, -1)
        else:
            layout.addWidget(self.v_addr_cb, 4, 0, 1, -1)
        layout.addWidget(self.v_err_label, 5, 0)
        layout.addWidget(self.v_err, 5, 1, 1, -1)
        layout.addWidget(self.p_addr_label, 6, 0, 1, -1)
        layout.addWidget(self.p_addr_cb, 7, 0, 1, -1)
        layout.addWidget(self.p_err_label, 8, 0)
        layout.addWidget(self.p_err, 8, 1, 1, -1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(9, 1)
        layout.addWidget(self.hw_err, 10, 0, 1, -1)
        layout.addWidget(self.cb_ignore, 11, 0, 1, -1)
        self.setLayout(layout)
        self.first_run = True

    def initializePage(self):
        new_mn = self.parent.new_mn
        wallet_is_p2sh = self.parent.wallet_is_p2sh
        if self.first_run:
            self.first_run = False
            start_id = self.parent.startId()
            manager = self.parent.manager
            skip_alias = None
            if start_id in self.parent.UPD_ENTER_PAGES:
                skip_alias = new_mn.alias

            for addr in self.parent.wallet.get_unused_addresses():
                self.p_addr_cb.addItem(addr)
                if not wallet_is_p2sh:
                    self.v_addr_cb.addItem(addr)
                    used = manager.find_owner_addr_use(addr,
                                                       skip_alias=skip_alias)
                    if not used:
                        self.o_addr_cb.addItem(addr)
            self.o_addr.setText('')
            self.v_addr.setText('')
            self.p_addr.setText('')

        i = 0
        first_o_addr = '' if wallet_is_p2sh else self.o_addr_cb.itemText(i)
        first_v_addr = '' if wallet_is_p2sh else self.v_addr_cb.itemText(i)
        first_p_addr = self.p_addr_cb.itemText(i)

        if not self.o_addr.text():
            owner_addr = new_mn.owner_addr
            if owner_addr:
                self.o_addr.setText(owner_addr)
            elif first_o_addr:
                self.o_addr.setText(first_o_addr)

        if not self.v_addr.text():
            voting_addr = new_mn.voting_addr
            if voting_addr:
                self.v_addr.setText(voting_addr)
            elif first_v_addr:
                self.v_addr.setText(first_v_addr)

        if not self.p_addr.text():
            payout_address = new_mn.payout_address
            if payout_address:
                self.p_addr.setText(payout_address)
            else:
                while first_p_addr:
                    if first_p_addr in [first_o_addr, first_v_addr]:
                        i += 1
                        first_p_addr = self.p_addr_cb.itemText(i)
                    else:
                        self.p_addr.setText(first_p_addr)
                        break

    @pyqtSlot()
    def on_change_o_addr(self):
        self.hide_o_error()
        self.completeChanged.emit()

    @pyqtSlot()
    def on_change_v_addr(self):
        self.hide_v_error()
        self.completeChanged.emit()

    @pyqtSlot()
    def on_change_p_addr(self):
        self.hide_p_error()
        self.completeChanged.emit()

    def isComplete(self):
        if self.o_addr.text() and self.v_addr.text() and self.p_addr.text():
            return True
        return False

    def hide_o_error(self):
        self.o_err_label.hide()
        self.o_err.hide()

    def hide_v_error(self):
        self.v_err_label.hide()
        self.v_err.hide()

    def hide_p_error(self):
        self.p_err_label.hide()
        self.p_err.hide()

    def validatePage(self):
        o_addr = self.o_addr.text()
        v_addr = self.v_addr.text()
        p_addr = self.p_addr.text()
        ignore_hw_warn = self.cb_ignore.isChecked()
        try:
            self.parent.validate_owner_addr(o_addr)
        except ValidationError as e:
            self.o_err.setText(str(e))
            self.o_err_label.show()
            self.o_err.show()
            return False

        try:
            self.parent.validate_voting_addr(v_addr)
        except ValidationError as e:
            self.v_err.setText(str(e))
            self.v_err_label.show()
            self.v_err.show()
            return False

        try:
            self.parent.validate_payout_addr(p_addr, o_addr, v_addr)
        except ValidationError as e:
            self.p_err.setText(str(e))
            self.p_err_label.show()
            self.p_err.show()
            return False

        try:
            self.parent.validate_sign_digest(o_addr, ignore_hw_warn)
        except HwWarnError as e:
            self.hw_err.setText(str(e))
            self.hw_err.show()
            self.cb_ignore.show()
            return False

        new_mn = self.parent.new_mn
        new_mn.owner_addr = o_addr
        new_mn.voting_addr = v_addr
        new_mn.payout_address = p_addr
        return True

    def nextId(self):
        return self.parent.BLS_KEYS_PAGE


class BlsKeysWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(BlsKeysWizardPage, self).__init__(parent)
        self.parent = parent
        layout = QGridLayout()

        self.bls_pub_label = QLabel(_('BLS Public key:'))
        self.bls_pub = ButtonsSLineEdit()
        self.bls_pub.textChanged.connect(self.on_pub_changed)

        self.op_reward_label = QLabel(_('Operator Reward:'))
        self.op_reward = QDoubleSpinBox()
        self.op_reward.setRange(0.0, 100.0)
        self.op_reward.setSingleStep(0.01)
        self.op_reward.setSuffix('%')
        self.op_reward_label.hide()
        self.op_reward.hide()

        self.bls_priv_label = QLabel(_('BLS Private key:'))
        self.bls_priv_label.hide()
        self.bls_priv = SLineEdit()
        self.bls_priv.setReadOnly(True)
        self.bls_priv.hide()
        self.gen_btn = QPushButton(_('Generate new BLS keypair'))
        self.gen_btn.clicked.connect(self.generate_bls_keypair)
        self.gen_btn.hide()
        self.bls_info_label = QLabel()
        self.bls_info_label.setWordWrap(True)
        self.bls_info_label.setObjectName('info-label')
        self.bls_info_label.hide()
        self.bls_info_edit = ButtonsSLineEdit()
        self.bls_info_edit.addCopyButton(self.parent.gui.app)
        self.bls_info_edit.setReadOnly(True)
        self.bls_info_edit.hide()

        self.err_label = QLabel(_('Error:'))
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()
        self.cb_ignore = QCheckBox(_('Ignore and continue.'))
        self.cb_ignore.hide()

        layout.addWidget(self.bls_pub_label, 0, 0, 1, -1)
        layout.addWidget(self.bls_pub, 1, 0, 1, -1)

        layout.addWidget(self.op_reward_label, 3, 0, 1, -1)
        layout.addWidget(self.op_reward, 4, 0, 1, -1)
        layout.addWidget(self.bls_priv_label, 3, 0, 1, -1)
        layout.addWidget(self.bls_priv, 4, 0, 1, -1)

        layout.addWidget(self.gen_btn, 6, 0, 1, -1)

        layout.addWidget(self.bls_info_label, 8, 0, 1, -1)
        layout.addWidget(self.bls_info_edit, 9, 0, 1, -1)

        layout.addWidget(self.err_label, 10, 0)
        layout.addWidget(self.err, 10, 1)
        layout.addWidget(self.cb_ignore, 12, 0, 1, -1)

        layout.setColumnStretch(1, 1)
        layout.setRowStretch(2, 1)
        layout.setRowStretch(5, 1)
        layout.setRowStretch(7, 1)
        self.setLayout(layout)
        self.first_run = True

    def hide_error(self):
        self.err_label.hide()
        self.err.hide()

    def show_error(self, err):
        self.err.setText(err)
        self.err_label.show()
        self.err.show()

    def initializePage(self):
        parent = self.parent
        new_mn = parent.new_mn
        start_id = parent.startId()
        self.op_reward_label.hide()
        self.op_reward.hide()
        if not new_mn.is_operated:
            self.bls_priv_label.hide()
            self.bls_priv.hide()
            self.gen_btn.hide()
            self.bls_info_label.hide()
            self.bls_info_edit.hide()
            self.bls_pub.setReadOnly(False)
            if self.bls_priv.text():
                self.bls_pub.setText('')
                self.bls_priv.setText('')
            if not self.bls_pub.text() and new_mn.pubkey_operator:
                self.bls_pub.setText(new_mn.pubkey_operator)
            if start_id == parent.UPD_REG_PAGE:
                self.setTitle(_('Operator BLS key setup'))
                self.setSubTitle(_('Update operator BLS public key'))
            else:
                self.op_reward_label.show()
                self.op_reward.show()
                self.setTitle(_('Operator BLS key and reward'))
                self.setSubTitle(_('Enter operator BLS public key and '
                                   'operator reward percent'))
                if not self.op_reward.value() and new_mn.op_reward:
                    self.op_reward.setValue(round(new_mn.op_reward/100, 2))
            return

        self.setTitle(_('BLS keys setup'))
        if start_id in parent.UPD_ENTER_PAGES:
            self.setSubTitle(_('Regenerate BLS keypair, setup dashd'))
            if not self.bls_priv.text():
                self.bls_priv.setText(new_mn.bls_privk)
                self.bls_pub.setText(new_mn.pubkey_operator)
        else:
            self.setSubTitle(_('Generate BLS keypair, setup dashd'))

        if not self.bls_priv.text():
            self.generate_bls_keypair()

        self.bls_pub.setReadOnly(True)
        if self.first_run:
            self.first_run = False
            self.bls_pub.addCopyButton(self.parent.gui.app)
        self.bls_priv_label.show()
        self.bls_priv.show()
        self.gen_btn.show()

    def generate_bls_keypair(self):
        random_seed = bytes(os.urandom(32))
        bls_privk = bls.PrivateKey.from_seed(random_seed)
        bls_pubk = bls_privk.get_public_key()
        bls_privk_hex = bh2u(bls_privk.serialize())
        bls_pubk_hex = bh2u(bls_pubk.serialize())
        self.bls_info_label.setText(_('BLS keypair generated. Before '
                                      'registering new Masternode copy next '
                                      'line to ~/.dashcore/dash.conf and '
                                      'restart masternode:'))
        self.bls_info_label.show()
        self.bls_info_edit.setText('masternodeblsprivkey=%s' % bls_privk_hex)
        self.bls_info_edit.show()
        self.bls_pub.setText(bls_pubk_hex)
        self.bls_priv.setText(bls_privk_hex)

    @pyqtSlot()
    def on_pub_changed(self):
        self.hide_error()

    def validatePage(self):
        new_mn = self.parent.new_mn
        bls_pub = self.bls_pub.text()
        bls_priv = self.bls_priv.text()

        if not new_mn.is_operated:
            if len(bls_pub) == 0:  # allow set later
                return True

            if len(bls_pub) != 96:
                self.show_error(_('Wrong length of BLS public key'))
                return False
            if bls_pub.strip('01234567890abcdefABCDEF'):
                self.show_error(_('Wrong format of BLS public key'))
                return False
            try:
                bls.PublicKey.from_bytes(bfh(bls_pub))
            except BaseException as e:
                self.show_error(str(e))
                return False

            op_reward = self.op_reward.value()
            if op_reward > 0.0:
                new_mn.op_reward = round(op_reward * 100)
            bls_priv = ''

        try:
            ignore_used = self.cb_ignore.isChecked()
            self.parent.validate_bls_pub(bls_pub, ignore_used)
        except UsedInWallet as e:
            self.cb_ignore.show()
            self.show_error(str(e))
            return False
        except ValidationError as e:
            self.show_error(str(e))
            return False

        new_mn.bls_privk = bls_priv
        new_mn.pubkey_operator = bls_pub
        return True

    def nextId(self):
        return self.parent.SAVE_DIP3_PAGE


class SaveDip3WizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(SaveDip3WizardPage, self).__init__(parent)
        self.parent = parent
        self.setCommitPage(True)
        self.new_mn = None
        self.layout = QGridLayout()

        self.alias = SLineEdit()
        self.alias.textChanged.connect(self.on_alias_changed)
        self.err_label = QLabel(_('Error:'))
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()
        ownership_label = QLabel(_('Ownership:'))
        self.ownership = QLabel()
        type_label = QLabel(_('Type:'))
        self.type = QLabel()
        mode_label = QLabel(_('Mode:'))
        self.mode = QLabel()
        self.collateral_label = QLabel(_('Collateral:'))
        self.collateral_label.hide()
        self.collateral = QLabel()
        self.collateral.hide()
        service_label = QLabel(_('Service:'))
        self.service = QLabel()
        self.owner_addr_label = QLabel(_('Owner Address:'))
        self.owner_addr_label.hide()
        self.owner_addr = QLabel()
        self.owner_addr.hide()
        pubkey_op_label = QLabel(_('PubKeyOperator:'))
        self.pubkey_op = QLabel()
        self.voting_addr_label = QLabel(_('Voting Address:'))
        self.voting_addr_label.hide()
        self.voting_addr = QLabel()
        self.voting_addr.hide()

        self.payout_address_label = QLabel(_('Payout Address:'))
        self.payout_address_label.hide()
        self.payout_address = QLabel()
        self.payout_address.hide()

        self.op_reward_label = QLabel(_('Operator Reward percent:'))
        self.op_reward_label.hide()
        self.op_reward = QLabel()
        self.op_reward.hide()

        self.op_payout_address_label = QLabel(_('Operator Payout Address:'))
        self.op_payout_address_label.hide()
        self.op_payout_address = QLabel()
        self.op_payout_address.hide()

        self.cb_make_tx = QCheckBox()
        self.cb_make_tx.setChecked(False)
        self.cb_make_tx.setEnabled(False)
        self.cb_make_tx.hide()

        self.layout.addWidget(self.alias, 0, 0, 1, 2)
        self.layout.addWidget(self.err_label, 1, 0)
        self.layout.addWidget(self.err, 1, 1)

        self.layout.addWidget(ownership_label, 2, 0)
        self.layout.addWidget(self.ownership, 2, 1)
        self.layout.addWidget(type_label, 3, 0)
        self.layout.addWidget(self.type, 3, 1)
        self.layout.addWidget(mode_label, 4, 0)
        self.layout.addWidget(self.mode, 4, 1)
        self.layout.addWidget(self.collateral_label, 5, 0)
        self.layout.addWidget(self.collateral, 5, 1)
        self.layout.addWidget(service_label, 6, 0)
        self.layout.addWidget(self.service, 6, 1)

        self.layout.addWidget(self.owner_addr_label, 7, 0)
        self.layout.addWidget(self.owner_addr, 7, 1)
        self.layout.addWidget(pubkey_op_label, 8, 0)
        self.layout.addWidget(self.pubkey_op, 8, 1)
        self.layout.addWidget(self.voting_addr_label, 9, 0)
        self.layout.addWidget(self.voting_addr, 9, 1)

        self.layout.addWidget(self.payout_address_label, 10, 0)
        self.layout.addWidget(self.payout_address, 10, 1)

        self.layout.addWidget(self.op_reward_label, 11, 0)
        self.layout.addWidget(self.op_reward, 11, 1)

        self.layout.addWidget(self.op_payout_address_label, 12, 0)
        self.layout.addWidget(self.op_payout_address, 12, 1)

        self.layout.setColumnStretch(1, 1)
        self.layout.setRowStretch(13, 1)
        self.layout.addWidget(self.cb_make_tx, 14, 1, Qt.AlignRight)
        self.setLayout(self.layout)

    def initializePage(self):
        self.new_mn = new_mn = self.parent.new_mn

        self.ownership.setText('')
        self.collateral.setText('')
        self.service.setText('')
        self.owner_addr.setText('')
        self.pubkey_op.setText('')
        self.voting_addr.setText('')
        self.payout_address.setText('')
        self.payout_address.hide()
        self.payout_address_label.hide()
        self.op_reward.setText('')
        self.op_reward.hide()
        self.op_reward_label.hide()
        self.op_payout_address.setText('')
        self.op_payout_address.hide()
        self.op_payout_address_label.hide()

        if not self.alias.text():
            self.alias.setText(new_mn.alias)

        if new_mn.is_owned and new_mn.is_operated:
            ownership = _('This wallet is owns and operates on the Masternode')
        elif new_mn.is_owned:
            ownership = (_('This wallet is owns on the Masternode '
                           '(external operator)'))
        elif new_mn.is_operated:
            ownership = (_('This wallet is the operator on the Masternode'))
        else:
            ownership = _('None')
        self.ownership.setText(ownership)

        self.type.setText(str(new_mn.type))
        self.mode.setText(str(new_mn.mode))
        collateral = str(new_mn.collateral)
        self.collateral.setText(collateral)
        self.service.setText(str(new_mn.service))

        self.pubkey_op.setText(new_mn.pubkey_operator)

        if new_mn.is_owned:
            self.collateral_label.show()
            self.collateral.show()

        if new_mn.owner_addr:
            self.owner_addr.setText(new_mn.owner_addr)
            self.owner_addr.show()
            self.owner_addr_label.show()

        if new_mn.voting_addr:
            self.voting_addr.setText(new_mn.voting_addr)
            self.voting_addr.show()
            self.voting_addr_label.show()

        if new_mn.payout_address:
            self.payout_address.setText(new_mn.payout_address)
            self.payout_address.show()
            self.payout_address_label.show()

        if new_mn.op_reward:
            self.op_reward.setText('%s%%' % (new_mn.op_reward/100))
            self.op_reward.show()
            self.op_reward_label.show()

        if new_mn.op_payout_address:
            self.op_payout_address.setText(new_mn.op_payout_address)
            self.op_payout_address.show()
            self.op_payout_address_label.show()

        parent = self.parent
        start_id = parent.startId()
        op_type = 'save'
        tx_name = 'Unknown'
        if start_id == parent.OPERATION_TYPE_PAGE:
            tx_name = 'ProRegTx'
        elif start_id == parent.UPD_SRV_PAGE:
            tx_name = 'ProUpServTx'
        elif start_id == parent.UPD_REG_PAGE:
            tx_name = 'ProUpRegTx'
        elif start_id == parent.COLLATERAL_PAGE:
            tx_name = 'UnknownTx'
        elif start_id == parent.SERVICE_PAGE:
            tx_name = 'UnknownTx'
        elif start_id == parent.BLS_KEYS_PAGE:
            tx_name = 'UnknownTx'
        else:
            op_type = 'unknown'

        self.setTitle('%s DIP3 masternode' % op_type.capitalize())
        self.setSubTitle('Examine parameters and %s Masternode.' % op_type)

        if start_id != parent.OPERATION_TYPE_PAGE:
            self.alias.setReadOnly(True)

        if (start_id == parent.OPERATION_TYPE_PAGE and new_mn.is_owned
                or start_id in [parent.UPD_SRV_PAGE, parent.UPD_REG_PAGE]):
            self.cb_make_tx.setChecked(True)
            self.cb_make_tx.setEnabled(True)
            tx_cb_label_text = 'Make %s after saving Masternode data' % tx_name
            self.cb_make_tx.setText(tx_cb_label_text)
            self.cb_make_tx.show()

        self.parent.setButtonText(QWizard.CommitButton, op_type.capitalize())

    @pyqtSlot()
    def on_alias_changed(self):
        self.completeChanged.emit()

    def isComplete(self):
        if self.new_mn is not None and self.alias.text():
            return True
        return False

    def validatePage(self):
        parent = self.parent
        start_id = parent.startId()
        alias = self.alias.text()
        if start_id == parent.OPERATION_TYPE_PAGE:
            try:
                parent.validate_alias(self.alias.text())
            except ValidationError as e:
                self.err.setText(str(e))
                self.err_label.show()
                self.err.show()
                return False
        self.new_mn.alias = alias

        dip3_tab = parent.parent()
        if start_id == parent.OPERATION_TYPE_PAGE:
            parent.manager.add_mn(self.new_mn)
            dip3_tab.w_model.reload_data()
            parent.saved_mn = alias
        elif start_id in parent.UPD_ENTER_PAGES:
            parent.manager.update_mn(alias, self.new_mn)
            dip3_tab.w_model.reload_alias(alias)
            parent.saved_mn = alias
        if self.cb_make_tx.isChecked():
            manager = parent.manager
            gui = parent.gui
            try:
                if start_id == parent.OPERATION_TYPE_PAGE:
                    pro_tx = manager.prepare_pro_reg_tx(alias)
                    tx_descr = 'ProRegTx'
                    tx_type = dash_tx.SPEC_PRO_REG_TX
                elif start_id == parent.UPD_SRV_PAGE:
                    pro_tx = manager.prepare_pro_up_srv_tx(self.new_mn)
                    tx_descr = 'ProUpServTx'
                    tx_type = dash_tx.SPEC_PRO_UP_SERV_TX
                elif start_id == parent.UPD_REG_PAGE:
                    pro_tx = manager.prepare_pro_up_reg_tx(self.new_mn)
                    tx_descr = 'ProUpRegTx'
                    tx_type = dash_tx.SPEC_PRO_UP_REG_TX
            except ProRegTxExc as e:
                gui.show_error(e)
                return True
            gui.do_clear()
            mn = self.new_mn
            if mn.collateral.is_null and tx_type == dash_tx.SPEC_PRO_REG_TX:
                gui.amount_e.setText('1000')
            mn_addrs = [mn.owner_addr, mn.voting_addr, mn.payout_address]
            for addr in manager.wallet.get_unused_addresses():
                if addr not in mn_addrs:
                    gui.payto_e.setText(addr)
                    break
            gui.extra_payload.set_extra_data(tx_type, pro_tx, alias)
            gui.show_extra_payload()
            gui.tabs.setCurrentIndex(gui.tabs.indexOf(gui.send_tab))
            parent.pro_tx_prepared = tx_descr
        return True

    def nextId(self):
        return self.parent.DONE_PAGE


class DoneWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(DoneWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('All done')
        self.setSubTitle('All operations completed successfully.')

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

    def nextId(self):
        return -1

    def initializePage(self):
        parent = self.parent
        start_id = parent.startId()
        if parent.saved_mn:
            if start_id == parent.OPERATION_TYPE_PAGE:
                operation = 'Created'
            else:
                operation = 'Updated'
            new_label_text = ('%s Masternode with alias: %s.' %
                              (operation, parent.saved_mn))
            new_mn_label = QLabel(new_label_text)
            self.layout.addWidget(new_mn_label)
        if parent.pro_tx_prepared:
            new_tx_label = QLabel('Prepared %s transaction to send.' %
                                  parent.pro_tx_prepared)
            self.layout.addWidget(new_tx_label)
        self.layout.addStretch(1)


class CollateralWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(CollateralWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('Select Collateral')
        self.setSubTitle('Select collateral output for Masternode.')

        self.no_collat_cb = QCheckBox('Create collateral as ProRegTx output')
        self.no_collat_cb.setChecked(True)
        self.no_collat_cb.stateChanged.connect(self.no_collat_state_changed)
        self.frozen_cb = QCheckBox('Include frozen coins/addresses')
        self.frozen_cb.setChecked(False)
        self.frozen_cb.setEnabled(False)
        self.frozen_cb.stateChanged.connect(self.frozen_state_changed)
        self.not_found = QLabel('No 1000 DASH outputs were found.')
        self.not_found.setObjectName('err-label')
        self.not_found.hide()

        self.outputs_list = OutputsList()
        self.outputs_list.outputSelected.connect(self.on_set_output)
        self.outputs_list.setEnabled(False)

        self.hash_label = QLabel('Transaction hash:')
        self.hash = SLineEdit()
        self.hash.setReadOnly(True)
        self.index_label = QLabel('Output index:')
        self.index = SLineEdit()
        self.index.setReadOnly(True)
        self.addr_label = QLabel('Output address:')
        self.addr = SLineEdit()
        self.addr.setReadOnly(True)
        self.value = SLineEdit()
        self.value.setReadOnly(True)
        self.value.hide()
        self.err_label = QLabel('Error:')
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()

        self.layout = QGridLayout()
        self.layout.addWidget(self.no_collat_cb, 0, 0)
        self.layout.addWidget(self.frozen_cb, 1, 0)
        self.layout.addWidget(self.not_found, 1, 1, Qt.AlignRight)
        self.layout.addWidget(self.outputs_list, 2, 0, 1, -1)
        self.layout.addWidget(self.hash_label, 3, 0)
        self.layout.addWidget(self.hash, 3, 1)
        self.layout.addWidget(self.index_label, 4, 0)
        self.layout.addWidget(self.index, 4, 1)
        self.layout.addWidget(self.addr_label, 5, 0)
        self.layout.addWidget(self.addr, 5, 1)
        self.layout.addWidget(self.err_label, 6, 0)
        self.layout.addWidget(self.err, 6, 1)
        self.layout.addWidget(self.value, 7, 1)

        self.layout.setColumnStretch(1, 1)
        self.layout.setRowStretch(6, 1)
        self.setLayout(self.layout)

    def hide_error(self):
        self.err_label.hide()
        self.err.hide()

    def show_error(self, err):
        self.err.setText(err)
        self.err_label.show()
        self.err.show()

    @pyqtSlot()
    def no_collat_state_changed(self):
        if self.no_collat_cb.isChecked():
            self.hide_error()
            self.not_found.hide()
            self.frozen_cb.setEnabled(False)
            self.outputs_list.setEnabled(False)
            self.hash.setText('0'*64)
            self.index.setText('-1')
            self.addr.setText('')
        else:
            self.hash.setText('')
            self.index.setText('')
            self.frozen_cb.setEnabled(True)
            self.outputs_list.setEnabled(True)
            new_mn = self.parent.new_mn
            self.scan_for_outputs()
            if new_mn.collateral.hash and new_mn.collateral.index >= 0:
                if not self.select_collateral(new_mn.collateral):
                    self.hash.setText(bh2u(new_mn.collateral.hash[::-1]))
                    self.index.setText(str(new_mn.collateral.index))
                    self.addr.setText('')

    @pyqtSlot()
    def frozen_state_changed(self):
        self.hide_error()
        self.not_found.hide()
        new_mn = self.parent.new_mn
        self.scan_for_outputs()
        if new_mn.collateral.hash and new_mn.collateral.index >= 0:
            if not self.select_collateral(new_mn.collateral):
                self.hash.setText(bh2u(new_mn.collateral.hash[::-1]))
                self.index.setText(str(new_mn.collateral.index))
                self.addr.setText('')

    def scan_for_outputs(self):
        self.outputs_list.clear()
        wallet = self.parent.wallet
        if self.frozen_cb.isChecked():
            excluded = None
        else:
            with wallet._freeze_lock:
                excluded = wallet._frozen_addresses.copy()
        coins = wallet.get_utxos(domain=None, excluded_addresses=excluded,
                                 mature_only=True, confirmed_funding_only=True)
        if not self.frozen_cb.isChecked():
            coins = [c for c in coins if not wallet.is_frozen_coin(c)]
        coins = list(filter(lambda x: (x.value_sats() == (1000 * COIN)),
                                       coins))

        if len(coins) > 0:
            self.outputs_list.add_outputs(coins)
        else:
            self.not_found.show()

    def select_collateral(self, c):
        if not c.hash or c.index < 0:
            return
        match = self.outputs_list.findItems(str(c), Qt.MatchExactly)
        if len(match):
            self.outputs_list.setCurrentItem(match[0])
            return True
        self.frozen_cb.setChecked(True)
        match = self.outputs_list.findItems(str(c), Qt.MatchExactly)
        if len(match):
            self.outputs_list.setCurrentItem(match[0])
            return True
        self.frozen_cb.setChecked(False)
        return False

    def on_set_output(self, coin):
        self.hide_error()
        self.hash.setText(coin.prevout.txid.hex())
        self.index.setText(str(coin.prevout.out_idx))
        self.addr.setText(coin.address)
        self.value.setText(str(coin.value_sats()))
        self.completeChanged.emit()

    def initializePage(self):
        new_mn = self.parent.new_mn
        c_idx = new_mn.collateral.index
        c_hash = new_mn.collateral.hash
        if c_hash and c_idx >= 0:
            self.no_collat_cb.setChecked(False)
        else:
            self.hash.setText('0'*64)
            self.index.setText('-1')

    def isComplete(self):
        return len(self.hash.text()) == 64

    def validatePage(self):
        parent = self.parent
        new_mn = parent.new_mn
        start_id = parent.startId()
        if start_id in parent.UPD_ENTER_PAGES:
            skip_alias = new_mn.alias
        else:
            skip_alias = None
        try:
            c_hash = self.hash.text()
            c_index = int(self.index.text())
            c_addr = self.addr.text()
            value = self.value.text()
            c_value = int(value if value else 0)
            collateral = '%s:%s' % (c_hash, c_index)
            parent.validate_collateral(collateral, c_addr, c_value,
                                       skip_alias=skip_alias)
        except ValidationError as e:
            self.show_error(str(e))
            return False

        new_mn.collateral = TxOutPoint(bfh(c_hash)[::-1], c_index)
        new_mn.protx_hash = ''  # reset hash for removed masternodes
        parent.collateral_addr = c_addr
        return True

    def nextId(self):
        return self.parent.SERVICE_PAGE


class ServiceWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(ServiceWizardPage, self).__init__(parent)
        self.parent = parent
        self.cur_service = None
        self.setTitle('Service params')
        self.setSubTitle('Select masternode IP address and port.')

        layout = QGridLayout()

        self.srv_addr_label = QLabel('Masternode Service Address:')
        self.srv_addr = SLineEdit()
        self.srv_addr.textChanged.connect(self.on_service_changed)
        self.srv_port_label = QLabel('Masternode Service Port:')
        self.srv_port = SLineEdit()
        self.srv_port.textChanged.connect(self.on_service_changed)

        self.err_label = QLabel('Error:')
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()
        self.cb_ignore = QCheckBox(_('Ignore and continue.'))
        self.cb_ignore.hide()

        layout.addWidget(self.srv_addr_label, 0, 0)
        layout.addWidget(self.srv_addr, 0, 1)
        layout.addWidget(self.srv_port_label, 0, 2)
        layout.addWidget(self.srv_port, 0, 3)

        layout.addWidget(self.err_label, 1, 0)
        layout.addWidget(self.err, 1, 1, 1, -1)
        layout.addWidget(self.cb_ignore, 3, 0, 1, -1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(2, 1)
        self.setLayout(layout)

    def hide_error(self):
        self.err_label.hide()
        self.err.hide()

    def show_error(self, err):
        self.err.setText(err)
        self.err_label.show()
        self.err.show()

    @pyqtSlot()
    def on_service_changed(self):
        self.completeChanged.emit()

    def isComplete(self):
        self.hide_error()
        if self.srv_port.text():
            return True
        return False

    def initializePage(self):
        new_mn = self.parent.new_mn
        str_mn_service = str(new_mn.service)
        if self.cur_service is None or self.cur_service != str_mn_service:
            self.cur_service = str_mn_service
            self.srv_addr.setText(new_mn.service.ip)
            self.srv_port.setText('%d' % new_mn.service.port)

    def validatePage(self):
        ip = self.srv_addr.text()
        port = self.srv_port.text()
        try:
            ignore_used = self.cb_ignore.isChecked()
            ip, port = self.parent.validate_service_ip_port(ip, port,
                                                            ignore_used)
        except UsedInWallet as e:
            self.cb_ignore.show()
            self.show_error(str(e))
            return False
        except ValidationError as e:
            self.show_error(str(e))
            return False
        self.parent.new_mn.service = ProTxService(ip, port)
        return True

    def nextId(self):
        if self.parent.new_mn.is_owned:
            return self.parent.SELECT_ADDR_PAGE
        else:
            return self.parent.BLS_KEYS_PAGE


class UpdSrvWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(UpdSrvWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('Update Service Features of Masternode')
        self.setSubTitle('Set Masternode service parameters.')

        layout = QGridLayout()

        self.srv_addr_label = QLabel('Masternode Service Address:')
        self.srv_addr = SLineEdit()
        self.srv_addr.textChanged.connect(self.on_service_changed)
        self.srv_port_label = QLabel('Masternode Service Port:')
        self.srv_port = SLineEdit()
        self.srv_port.textChanged.connect(self.on_service_changed)

        self.op_p_addr_label = QLabel('Operator Payout Address:')
        self.op_p_addr_cb = SComboBox()
        self.op_p_addr_cb.setEditable(True)
        self.op_p_addr = self.op_p_addr_cb.lineEdit()
        self.op_p_addr.textChanged.connect(self.on_change_op_p_addr)
        self.op_p_addr_label.hide()
        self.op_p_addr_cb.hide()

        self.err_label = QLabel('Error:')
        self.err_label.setObjectName('err-label')
        self.err = QLabel()
        self.err.setObjectName('err-label')
        self.err_label.hide()
        self.err.hide()
        self.cb_ignore = QCheckBox(_('Ignore and continue.'))
        self.cb_ignore.hide()

        layout.addWidget(self.srv_addr_label, 0, 0)
        layout.addWidget(self.srv_addr, 0, 1)
        layout.addWidget(self.srv_port_label, 0, 2)
        layout.addWidget(self.srv_port, 0, 3)
        layout.addWidget(self.op_p_addr_label, 1, 0)
        layout.addWidget(self.op_p_addr_cb, 1, 1, 1, -1)

        layout.addWidget(self.err_label, 2, 0)
        layout.addWidget(self.err, 2, 1, 1, -1)
        layout.addWidget(self.cb_ignore, 4, 0, 1, -1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(3, 1)
        self.setLayout(layout)

    def nextId(self):
        return self.parent.SAVE_DIP3_PAGE

    def hide_error(self):
        self.err_label.hide()
        self.err.hide()

    def show_error(self, err):
        self.err.setText(err)
        self.err_label.show()
        self.err.show()

    def initializePage(self):
        self.upd_mn = upd_mn = self.parent.new_mn
        if not upd_mn:
            return
        self.srv_addr.setText(upd_mn.service.ip)
        self.srv_port.setText('%d' % upd_mn.service.port)

        if not upd_mn.is_owned and upd_mn.is_operated:
            for addr in self.parent.wallet.get_unused_addresses():
                self.op_p_addr_cb.addItem(addr)
            self.op_p_addr.setText('')
            self.op_p_addr_label.show()
            self.op_p_addr_cb.show()

            first_op_p_addr = self.op_p_addr_cb.itemText(0)
            if not self.op_p_addr.text():
                op_payout_address = upd_mn.op_payout_address
                if op_payout_address:
                    self.op_p_addr.setText(op_payout_address)
                elif first_op_p_addr:
                    self.op_p_addr.setText(first_op_p_addr)

    @pyqtSlot()
    def on_change_op_p_addr(self):
        self.completeChanged.emit()

    @pyqtSlot()
    def on_service_changed(self):
        self.completeChanged.emit()

    def isComplete(self):
        self.hide_error()
        if self.srv_addr.text() and self.srv_port.text():
            return True
        return False

    def validatePage(self):
        ip = self.srv_addr.text()
        port = self.srv_port.text()
        try:
            ignore_used = self.cb_ignore.isChecked()
            ip, port = self.parent.validate_service_ip_port(ip, port,
                                                            ignore_used)
        except UsedInWallet as e:
            self.cb_ignore.show()
            self.show_error(str(e))
            return False
        except ValidationError as e:
            self.show_error(str(e))
            return False
        self.parent.new_mn.service = ProTxService(ip, port)
        if not self.upd_mn.is_owned and self.upd_mn.is_operated:
            op_p_addr = self.op_p_addr.text()
            if op_p_addr and not is_b58_address(op_p_addr):
                err = 'Operator payout address must be of P2PKH/P2SH type'
                self.show_error(err)
                return False
            self.upd_mn.op_payout_address = op_p_addr
        return True


class UpdRegWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(UpdRegWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('Update addresses')
        self.setSubTitle('Update Masternode voting/payout addresses.')
        wallet_is_p2sh = self.parent.wallet_is_p2sh
        layout = QGridLayout()
        self.v_addr_label = QLabel(_('Voting Address (P2PKH):'))
        if wallet_is_p2sh:
            self.v_addr = SLineEdit()
        else:
            self.v_addr_cb = SComboBox()
            self.v_addr_cb.setEditable(True)
            self.v_addr = self.v_addr_cb.lineEdit()
        self.v_addr.textChanged.connect(self.on_change_v_addr)
        self.v_err_label = QLabel(_('Error:'))
        self.v_err_label.setObjectName('err-label')
        self.v_err = QLabel()
        self.v_err.setObjectName('err-label')
        self.v_err_label.hide()
        self.v_err.hide()
        self.p_addr_label = QLabel(_('Payout Address (must differ from '
                                     'owner/voting):'))
        self.p_addr_cb = SComboBox()
        self.p_addr_cb.setEditable(True)
        self.p_addr = self.p_addr_cb.lineEdit()
        self.p_addr.textChanged.connect(self.on_change_p_addr)
        self.p_err_label = QLabel(_('Error:'))
        self.p_err_label.setObjectName('err-label')
        self.p_err = QLabel()
        self.p_err.setObjectName('err-label')
        self.p_err_label.hide()
        self.p_err.hide()

        layout.addWidget(self.v_addr_label, 0, 0, 1, -1)
        if wallet_is_p2sh:
            layout.addWidget(self.v_addr, 1, 0, 1, -1)
        else:
            layout.addWidget(self.v_addr_cb, 1, 0, 1, -1)
        layout.addWidget(self.v_err_label, 2, 0)
        layout.addWidget(self.v_err, 2, 1, 1, -1)
        layout.addWidget(self.p_addr_label, 3, 0, 1, -1)
        layout.addWidget(self.p_addr_cb, 4, 0, 1, -1)
        layout.addWidget(self.p_err_label, 5, 0)
        layout.addWidget(self.p_err, 5, 1, 1, -1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(6, 1)
        self.setLayout(layout)
        self.first_run = True

    def nextId(self):
        return self.parent.BLS_KEYS_PAGE

    def initializePage(self):
        new_mn = self.parent.new_mn
        wallet_is_p2sh = self.parent.wallet_is_p2sh
        if self.first_run:
            self.first_run = False
            for addr in self.parent.wallet.get_unused_addresses():
                self.p_addr_cb.addItem(addr)
                if not wallet_is_p2sh:
                    self.v_addr_cb.addItem(addr)
            self.v_addr.setText('')
            self.p_addr.setText('')

        i = 0
        first_v_addr = '' if wallet_is_p2sh else self.v_addr_cb.itemText(i)
        first_p_addr = self.p_addr_cb.itemText(i)

        if not self.v_addr.text():
            voting_addr = new_mn.voting_addr
            if voting_addr:
                self.v_addr.setText(voting_addr)
            elif first_v_addr:
                self.v_addr.setText(first_v_addr)

        if not self.p_addr.text():
            payout_address = new_mn.payout_address
            if payout_address:
                self.p_addr.setText(payout_address)
            else:
                while first_p_addr:
                    if first_p_addr in [new_mn.owner_addr, first_v_addr]:
                        i += 1
                        first_p_addr = self.p_addr_cb.itemText(i)
                    else:
                        self.p_addr.setText(first_p_addr)
                        break

    @pyqtSlot()
    def on_change_v_addr(self):
        self.hide_v_error()
        self.completeChanged.emit()

    @pyqtSlot()
    def on_change_p_addr(self):
        self.hide_p_error()
        self.completeChanged.emit()

    def isComplete(self):
        if self.v_addr.text() and self.p_addr.text():
            return True
        return False

    def hide_v_error(self):
        self.v_err_label.hide()
        self.v_err.hide()

    def hide_p_error(self):
        self.p_err_label.hide()
        self.p_err.hide()

    def validatePage(self):
        new_mn = self.parent.new_mn
        o_addr = new_mn.owner_addr
        v_addr = self.v_addr.text()
        p_addr = self.p_addr.text()

        try:
            self.parent.validate_voting_addr(v_addr)
        except ValidationError as e:
            self.v_err.setText(str(e))
            self.v_err_label.show()
            self.v_err.show()
            return False

        try:
            self.parent.validate_payout_addr(p_addr, o_addr, v_addr)
        except ValidationError as e:
            self.p_err.setText(str(e))
            self.p_err_label.show()
            self.p_err.show()
            return False

        new_mn.voting_addr = v_addr
        new_mn.payout_address = p_addr
        return True


class Dip3MasternodeWizard(QWizard):

    OPERATION_TYPE_PAGE = 1
    IMPORT_LEGACY_PAGE = 2
    SERVICE_PAGE = 3
    SELECT_ADDR_PAGE = 4
    BLS_KEYS_PAGE = 5
    SAVE_DIP3_PAGE = 6
    DONE_PAGE = 7

    COLLATERAL_PAGE = 100
    UPD_SRV_PAGE = 101
    UPD_REG_PAGE = 102

    UPD_ENTER_PAGES = [COLLATERAL_PAGE, SERVICE_PAGE, BLS_KEYS_PAGE,
                       UPD_SRV_PAGE, UPD_REG_PAGE]

    def __init__(self, parent, mn=None, start_id=None):
        super(Dip3MasternodeWizard, self).__init__(parent)
        self.gui = parent.gui
        self.manager = parent.manager
        self.wallet = w = parent.wallet
        self.wallet_is_p2sh = not is_p2pkh_address(w.get_addresses()[0])

        if mn:
            self.new_mn = ProTxMN.from_dict(mn.as_dict())
        else:
            self.new_mn = None
        self.collateral_addr = None
        self.saved_mn = False
        self.pro_tx_prepared = False

        self.setPage(self.OPERATION_TYPE_PAGE, OperationTypeWizardPage(self))
        self.setPage(self.IMPORT_LEGACY_PAGE, ImportLegacyWizardPage(self))
        self.setPage(self.SELECT_ADDR_PAGE, SelectAddressesWizardPage(self))
        self.setPage(self.BLS_KEYS_PAGE, BlsKeysWizardPage(self))
        self.setPage(self.SAVE_DIP3_PAGE, SaveDip3WizardPage(self))
        self.setPage(self.DONE_PAGE, DoneWizardPage(self))
        self.setPage(self.COLLATERAL_PAGE, CollateralWizardPage(self))
        self.setPage(self.SERVICE_PAGE, ServiceWizardPage(self))
        self.setPage(self.UPD_SRV_PAGE, UpdSrvWizardPage(self))
        self.setPage(self.UPD_REG_PAGE, UpdRegWizardPage(self))

        if start_id:
            self.setStartId(start_id)
            title = _('Update DIP3 Masternode: {}').format(mn.alias)
        else:
            title = _('Add DIP3 Masternode')

        logo = QPixmap(icon_path('tab_dip3.png'))
        logo = logo.scaledToWidth(32, mode=Qt.SmoothTransformation)
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setPixmap(QWizard.LogoPixmap, logo)
        self.setWindowTitle(title)
        self.setWindowIcon(read_QIcon('electrum-dash.png'))
        self.setMinimumSize(1000, 450)

    def validate_alias(self, alias):
        if not alias:
            raise ValidationError('Alias not set')
        if len(alias) > 32:
            raise ValidationError('Masternode alias cannot be longer '
                                  'than 32 characters')
        if alias in self.manager.mns.keys():
            raise ValidationError('Masternode with alias %s already exists' %
                                  alias)
        return alias

    def validate_str_service(self, service):
        if not service:
            raise ValidationError('No service value specified')
        try:
            ip, port = service_to_ip_port(service)
        except BaseException:
            raise ValidationError('Wrong service format specified')
        return ip, port

    def validate_service_ip_port(self, ip, port, ignore_used):
        try:
            if ip:
                ipaddress.ip_address(ip)
        except ValueError:
            raise ValidationError('Wrong service address specified')
        try:
            port = int(port)
        except ValueError:
            raise ValidationError('Service port must be integer number')
        if not 1 <= port <= 65535:
            raise ValidationError('Service port must be in range 1-65535')
        serv = ProTxService(ip, port)
        skip_alias = None
        start_id = self.startId()
        if start_id in self.UPD_ENTER_PAGES:
            skip_alias = self.new_mn.alias
        used = self.manager.find_service_use(serv, skip_alias=skip_alias,
                                             ignore_used=ignore_used)
        if isinstance(used, str):
            err = _('Service {} used by: {}').format(serv, used)
            raise UsedInWallet(err)
        elif used:
            err = _('Service {} used by registered masternodes').format(serv)
            raise ValidationError(err)
        return ip, port

    def validate_bls_pub(self, bls_pub, ignore_used):
        skip_alias = None
        start_id = self.startId()
        if start_id in self.UPD_ENTER_PAGES:
            skip_alias = self.new_mn.alias
        used = self.manager.find_bls_pub_use(bls_pub, skip_alias=skip_alias,
                                             ignore_used=ignore_used)
        if isinstance(used, str):
            err = _('pubKeyOperarot used by: {}').format(used)
            raise UsedInWallet(err)
        elif used:
            err = _('pubKeyOperarot used by registered masternodes')
            raise ValidationError(err)

    def validate_collateral(self, outpoint, addr, value, skip_alias=None):
        outpoint = outpoint.split(':')
        if len(outpoint) != 2:
            raise ValidationError('Wrong collateral format')
        prevout_hash, prevout_n = outpoint
        prevout_n = int(prevout_n)

        if prevout_hash == '0'*64 and prevout_n == -1:
            return prevout_hash, prevout_n, addr

        coins = self.wallet.get_utxos(domain=None, excluded_addresses=None,
                                      mature_only=True,
                                      confirmed_funding_only=True)

        coins = filter(lambda x: (x.prevout.txid.hex() == prevout_hash
                                  and x.prevout.out_idx == prevout_n),
                       coins)
        coins = list(coins)
        if not coins:
            raise ValidationError('Provided Outpoint not found in the wallet')

        c_vin = coins[0]

        if not value:
            raise ValidationError('No collateral value specified')
        if not addr:
            raise ValidationError('No collateral address specified')
        if not outpoint:
            raise ValidationError('No collateral outpoint specified')

        if not value == 1000 * COIN or not value == c_vin.value_sats():
            raise ValidationError('Wrong collateral value')


        if prevout_hash:
            if skip_alias:
                mns_collaterals = [(mns.as_dict())['collateral']
                                   for mns in self.manager.mns.values()
                                   if mns.alias != skip_alias]
            else:
                mns_collaterals = [(mns.as_dict())['collateral']
                                   for mns in self.manager.mns.values()]
            mns_collaterals = ['%s:%s' % (c['hash'], c['index'])
                               for c in mns_collaterals]
            coll_str = '%s:%s' % (prevout_hash, prevout_n)
            if coll_str in mns_collaterals:
                raise ValidationError('Provided Outpoint already used '
                                      'in saved DIP3 Masternodes')

        return prevout_hash, prevout_n, addr

    def validate_owner_addr(self, addr):
        if not is_p2pkh_address(addr):
            raise ValidationError('Owner address must be of P2PKH type')
        skip_alias = None
        start_id = self.startId()
        if start_id in self.UPD_ENTER_PAGES:
            skip_alias = self.new_mn.alias
        use = self.manager.find_owner_addr_use(addr, skip_alias=skip_alias)
        if use:
            raise ValidationError('Address already used by: {}'.format(use))

    def validate_voting_addr(self, addr):
        if not is_p2pkh_address(addr):
            raise ValidationError('Voting address must be of P2PKH type')

    def validate_payout_addr(self, addr, o_addr, v_addr):
        if not is_b58_address(addr):
            raise ValidationError('Payout address must be of P2PKH/P2SH type')
        if addr == o_addr or addr == v_addr:
            raise ValidationError('Payout address must differ from owner '
                                  'and voting addresses')

    def validate_sign_digest(self, addr, ignore_hw_warn):
        hw_warn_msg = None
        if not self.wallet.is_mine(addr):
            hw_warn_msg = ('Warning: sign_digest is not implemented in '
                           'hardware wallet keystores. If address you set '
                           'as Owner address belongs to hardware wallet '
                           'it is impossible to sign a ProUpRegTx. ')
        elif not hasattr(self.wallet.keystore, 'sign_digest'):
            hw_warn_msg = ('Warning: sign_digest not implemented in '
                           'hardware wallet keystores. You cannot use '
                           'this wallet to sign a ProUpRegTx. ')
        if hw_warn_msg and not ignore_hw_warn:
            hw_warn_possibility = ('You can still register a masternode,'
                                   ' but in the future it will be impossible'
                                   ' to change voting/payout addresses or'
                                   ' the operator public BLS key')
            raise HwWarnError(hw_warn_msg + hw_warn_possibility)


class Dip3FileWizard(QWizard):

    OP_TYPE_PAGE = 1
    EXPORT_PAGE = 2
    IMPORT_PAGE = 3
    DONE_PAGE = 4

    def __init__(self, parent, mn=None, start_id=None):
        super(Dip3FileWizard, self).__init__(parent)
        self.gui = parent.gui
        self.manager = parent.manager
        self.wallet = parent.wallet

        self.setPage(self.OP_TYPE_PAGE, FileOpTypeWizardPage(self))
        self.setPage(self.EXPORT_PAGE, ExportToFileWizardPage(self))
        self.setPage(self.IMPORT_PAGE, ImportFromFileWizardPage(self))
        self.setPage(self.DONE_PAGE, FileDoneWizardPage(self))
        self.saved_aliases = []
        self.saved_path = None
        self.imported_aliases = []
        self.skipped_aliases = []
        self.imported_path = None

        title = 'Export/Import DIP3 Masternodes to/from file'
        logo = QPixmap(icon_path('tab_dip3.png'))
        logo = logo.scaledToWidth(32, mode=Qt.SmoothTransformation)
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setPixmap(QWizard.LogoPixmap, logo)
        self.setWindowTitle(title)
        self.setWindowIcon(read_QIcon('electrum-dash.png'))
        self.setMinimumSize(1000, 450)


class FileOpTypeWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(FileOpTypeWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('Operation type')
        self.setSubTitle('Select operation type.')

        self.rb_export = QRadioButton('Export DIP3 Masternodes to file')
        self.rb_import = QRadioButton('Import DIP3 Masternodes from file')
        self.rb_export.setChecked(True)
        self.button_group = QButtonGroup()
        self.button_group.addButton(self.rb_export)
        self.button_group.addButton(self.rb_import)
        gb_vbox = QVBoxLayout()
        gb_vbox.addWidget(self.rb_export)
        gb_vbox.addWidget(self.rb_import)
        self.gb_op_type = QGroupBox('Select operation type')
        self.gb_op_type.setLayout(gb_vbox)

        layout = QVBoxLayout()
        layout.addWidget(self.gb_op_type)
        layout.addStretch(1)
        self.setLayout(layout)

    def nextId(self):
        if self.rb_export.isChecked():
            return self.parent.EXPORT_PAGE
        else:
            return self.parent.IMPORT_PAGE


class ExportToFileWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(ExportToFileWizardPage, self).__init__(parent)
        self.parent = parent
        self.setCommitPage(True)
        self.setTitle('Export to file')
        self.setSubTitle('Export DIP3 Masternodes to file.')

        self.lb_aliases = QLabel('Exported DIP3 Masternodes:')
        self.lw_aliases = QListWidget()
        self.lw_aliases.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sel_model = self.lw_aliases.selectionModel()
        self.sel_model.selectionChanged.connect(self.on_selection_changed)
        aliases = self.parent.manager.mns.keys()
        self.lw_aliases.addItems(aliases)
        self.lw_aliases.selectAll()

        layout = QVBoxLayout()
        layout.addWidget(self.lb_aliases)
        layout.addWidget(self.lw_aliases)
        self.setLayout(layout)
        self.aliases = []

    def initializePage(self):
        self.parent.setButtonText(QWizard.CommitButton, 'Save')
        self.aliases = [i.text() for i in self.lw_aliases.selectedItems()]

    @pyqtSlot()
    def on_selection_changed(self):
        self.aliases = [i.text() for i in self.lw_aliases.selectedItems()]
        self.completeChanged.emit()

    def isComplete(self):
        return len(self.aliases) > 0

    def nextId(self):
        return self.parent.DONE_PAGE

    def validatePage(self):
        fdlg = QFileDialog(self, 'Save DIP3 Masternodes', os.getenv('HOME'))
        fdlg.setOptions(QFileDialog.DontConfirmOverwrite)
        fdlg.setAcceptMode(QFileDialog.AcceptSave)
        fdlg.setFileMode(QFileDialog.AnyFile)
        fdlg.setNameFilter("ProTx (*.protx)");
        fdlg.exec()

        if not fdlg.result():
            return False

        self.path = fdlg.selectedFiles()
        if len(self.path) > 0:
            self.path = self.path[0]

        if self.path.find('*') > 0 or self.path.find('?') > 0:
            return False

        fi = QFileInfo(self.path)
        if fi.suffix() != 'protx':
            self.path = '%s.protx' % self.path
            fi = QFileInfo(self.path)

        if fi.exists():
            overwrite_msg = 'Overwrite existing file?\n%s'
            res = self.parent.gui.question(overwrite_msg % self.path)
            if not res:
                return False

        manager = self.parent.manager
        store_data = {'mns': {}}
        with open(self.path, 'w') as fd:
            for alias, mn in manager.mns.items():
                if alias not in self.aliases:
                    continue
                store_data['mns'][alias] = mn.as_dict()
            fd.write(json.dumps(store_data, indent=4))
        os.chmod(self.path, FILE_OWNER_MODE)
        self.parent.saved_aliases = self.aliases
        self.parent.saved_path = self.path
        return True


class ImportFromFileWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(ImportFromFileWizardPage, self).__init__(parent)
        self.parent = parent
        self.setCommitPage(True)
        self.setTitle('Import from file')
        self.setSubTitle('Import DIP3 Masternodes from file.')

        self.imp_btn = QPushButton('Load *.protx file')
        self.imp_btn.clicked.connect(self.on_load_protx)
        owerwrite_msg = 'Overwrite existing Masternodes with same aliases'
        self.cb_overwrite = QCheckBox(owerwrite_msg)

        self.lw_i_label = QLabel('Imported aliases')
        self.lw_i_aliases = QListWidget()
        self.lw_i_aliases.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.i_sel_model = self.lw_i_aliases.selectionModel()
        self.i_sel_model.selectionChanged.connect(self.on_i_selection_changed)
        self.i_aliases = []

        self.lw_w_label = QLabel('Existing aliases')
        self.lw_w_aliases = QListWidget()
        self.lw_w_aliases.setSelectionMode(QAbstractItemView.NoSelection)
        aliases = self.parent.manager.mns.keys()
        self.lw_w_aliases.addItems(aliases)

        layout = QGridLayout()
        layout.addWidget(self.imp_btn, 0, 0)
        layout.addWidget(self.cb_overwrite, 0, 2)
        layout.addWidget(self.lw_i_label, 1, 0)
        layout.addWidget(self.lw_w_label, 1, 2)
        layout.addWidget(self.lw_i_aliases, 2, 0)
        layout.addWidget(self.lw_w_aliases, 2, 2)
        layout.setColumnStretch(0, 5)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 5)
        self.setLayout(layout)

    def initializePage(self):
        self.parent.setButtonText(QWizard.CommitButton, 'Import')

    def nextId(self):
        return self.parent.DONE_PAGE

    @pyqtSlot()
    def on_load_protx(self):
        fdlg = QFileDialog(self, 'Load DIP3 Masternodes', os.getenv('HOME'))
        fdlg.setAcceptMode(QFileDialog.AcceptOpen)
        fdlg.setFileMode(QFileDialog.AnyFile)
        fdlg.setNameFilter("ProTx (*.protx)");
        fdlg.exec()

        if not fdlg.result():
            return False

        self.path = fdlg.selectedFiles()
        if len(self.path) > 0:
            self.path = self.path[0]

        self.lw_i_aliases.clear()
        with open(self.path, 'r') as fd:
            try:
                import_data = json.loads(fd.read())
                import_data = import_data.get('mns', None)
                if import_data is None:
                    raise Exception('No mns key found in protx file')
                if not isinstance(import_data, dict):
                    raise Exception('Wrong mns key format')
                aliases = import_data.keys()
                self.lw_i_aliases.addItems(aliases)
                self.lw_i_aliases.selectAll()
                self.import_data = import_data
            except Exception as e:
                self.parent.gui.show_error('Wrong file format: %s' % str(e))

    @pyqtSlot()
    def on_i_selection_changed(self):
        self.i_aliases = [i.text() for i in self.lw_i_aliases.selectedItems()]
        self.completeChanged.emit()

    def isComplete(self):
        return len(self.i_aliases) > 0

    def validatePage(self):
        overwrite = self.cb_overwrite.isChecked()
        manager = self.parent.manager
        aliases = manager.mns.keys()
        for ia in self.i_aliases:
            mn = ProTxMN.from_dict(self.import_data[ia])
            if ia in aliases:
                if overwrite:
                    manager.update_mn(ia, mn)
                    self.parent.imported_aliases.append(ia)
                else:
                    self.parent.skipped_aliases.append(ia)
                    continue
            else:
                self.parent.manager.add_mn(mn)
                self.parent.imported_aliases.append(ia)
        if len(self.parent.imported_aliases) > 0:
            dip3_tab = self.parent.parent()
            dip3_tab.w_model.reload_data()
        self.parent.imported_path = self.path
        return True

class FileDoneWizardPage(QWizardPage):

    def __init__(self, parent=None):
        super(FileDoneWizardPage, self).__init__(parent)
        self.parent = parent
        self.setTitle('All done')
        self.setSubTitle('All operations completed successfully.')

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

    def nextId(self):
        return -1

    def initializePage(self):
        parent = self.parent
        if parent.saved_path:
            aliases = ', '.join(parent.saved_aliases)
            path = parent.saved_path
            self.layout.addWidget(QLabel('Aliases: %s' % aliases))
            self.layout.addWidget(QLabel('Saved to file: %s' % path))
        elif parent.imported_path:
            aliases = ', '.join(parent.imported_aliases)
            skipped = ', '.join(parent.skipped_aliases)
            path = parent.imported_path
            self.layout.addWidget(QLabel('Imported from file: %s' % path))
            if aliases:
                self.layout.addWidget(QLabel('Impored Aliases: %s' % aliases))
            if skipped:
                self.layout.addWidget(QLabel('Skipped Aliases: %s' % skipped))
        self.layout.addStretch(1)
