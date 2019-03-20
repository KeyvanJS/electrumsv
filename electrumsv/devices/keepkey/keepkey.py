# ElectrumSV - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
# Copyright (C) 2019 ElectrumSV developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import threading

from electrumsv.app_state import app_state
from electrumsv.bip32 import xpub_from_pubkey
from electrumsv.bitcoin import TYPE_ADDRESS, TYPE_SCRIPT
from electrumsv.device import Device
from electrumsv.exceptions import UserCancelled
from electrumsv.i18n import _
from electrumsv.keystore import Hardware_KeyStore, is_xpubkey, parse_xpubkey
from electrumsv.logs import logs
from electrumsv.networks import Net
from electrumsv.util import bfh

from ..hw_wallet import HW_PluginBase

# TREZOR initialization methods
TIM_NEW, TIM_RECOVER, TIM_MNEMONIC, TIM_PRIVKEY = range(0, 4)
KEEPKEY_PRODUCT_KEY = 'KeepKey'


class KeepKey_KeyStore(Hardware_KeyStore):
    hw_type = 'keepkey'
    device = KEEPKEY_PRODUCT_KEY

    def get_derivation(self):
        return self.derivation

    def get_client(self, force_pair=True):
        return self.plugin.get_client(self, force_pair)

    def decrypt_message(self, sequence, message, password):
        raise RuntimeError(_('Encryption and decryption are not implemented by {}').format(
            self.device))

    def sign_message(self, sequence, message, password):
        client = self.get_client()
        address_path = self.get_derivation() + "/%d/%d"%sequence
        address_n = client.expand_path(address_path)
        msg_sig = client.sign_message(self.plugin.get_coin_name(client), address_n, message)
        return msg_sig.signature

    def sign_transaction(self, tx, password):
        if tx.is_complete():
            return
        # path of the xpubs that are involved
        xpub_path = {}
        for txin in tx.inputs():
            pubkeys, x_pubkeys = tx.get_sorted_pubkeys(txin)
            tx_hash = txin['prevout_hash']
            for x_pubkey in x_pubkeys:
                if not is_xpubkey(x_pubkey):
                    continue
                xpub, s = parse_xpubkey(x_pubkey)
                if xpub == self.get_master_public_key():
                    xpub_path[xpub] = self.get_derivation()

        self.plugin.sign_transaction(self, tx, xpub_path)


class KeepKeyPlugin(HW_PluginBase):

    MAX_LABEL_LEN = 32

    firmware_URL = 'https://www.keepkey.com'
    libraries_URL = 'https://github.com/keepkey/python-keepkey'
    minimum_firmware = (4, 0, 0)
    keystore_class = KeepKey_KeyStore

    def __init__(self, name):
        super().__init__(name)
        try:
            from . import client
            import keepkeylib
            import keepkeylib.ckd_public
            self.client_class = client.KeepKeyClient
            self.ckd_public = keepkeylib.ckd_public
            self.types = keepkeylib.client.types
            self.DEVICE_IDS = (KEEPKEY_PRODUCT_KEY,)
            self.libraries_available = True
        except ImportError:
            self.libraries_available = False

        self.logger = logs.get_logger("plugin.keepkey")

        self.main_thread = threading.current_thread()

    def get_coin_name(self, client):
        # No testnet support yet
        if client.features.major_version < 6:
            return "BitcoinCash"
        return "BitcoinSV"

    def _enumerate_hid(self):
        if self.libraries_available:
            from keepkeylib.transport_hid import HidTransport
            return HidTransport.enumerate()
        return []

    def _enumerate_web_usb(self):
        if self.libraries_available:
            from keepkeylib.transport_webusb import WebUsbTransport
            return WebUsbTransport.enumerate()
        return []

    def _get_transport(self, device):
        self.logger.debug("Trying to connect over USB...")

        if device.path.startswith('web_usb'):
            for d in self._enumerate_web_usb():
                if self._web_usb_path(d) == device.path:
                    from keepkeylib.transport_webusb import WebUsbTransport
                    return WebUsbTransport(d)
        else:
            for d in self._enumerate_hid():
                if str(d[0]) == device.path:
                    from keepkeylib.transport_hid import HidTransport
                    return HidTransport(d)

        raise RuntimeError(f'device {device} not found')

    def _device_for_path(self, path):
        return Device(
            path=path,
            interface_number=-1,
            id_=path,
            product_key=KEEPKEY_PRODUCT_KEY,
            usage_page=0,
            transport_ui_string=path,
        )

    def _web_usb_path(self, device):
        return f'web_usb:{device.getBusNumber()}:{device.getPortNumberList()}'

    def enumerate_devices(self):
        devices = []

        for device in self._enumerate_web_usb():
            devices.append(self._device_for_path(self._web_usb_path(device)))

        for device in self._enumerate_hid():
            # Cast needed for older firmware
            devices.append(self._device_for_path(str(device[0])))

        return devices

    def create_client(self, device, handler):
        # disable bridge because it seems to never returns if keepkey is plugged
        try:
            transport = self._get_transport(device)
        except Exception as e:
            self.logger.error("cannot connect to device")
            raise

        self.logger.debug("connected to device at %s", device.path)

        client = self.client_class(transport, handler, self)

        # Try a ping for device sanity
        try:
            client.ping('t')
        except Exception as e:
            self.logger.error("ping failed %s", e)
            return None

        if not client.atleast_version(*self.minimum_firmware):
            msg = (_('Outdated {} firmware for device labelled {}. Please '
                     'download the updated firmware from {}')
                   .format(self.device, client.label(), self.firmware_URL))
            self.logger.error(msg)
            handler.show_error(msg)
            return None

        return client

    def get_client(self, keystore, force_pair=True):
        client = app_state.device_manager.client_for_keystore(self, keystore, force_pair)
        # returns the client for a given keystore. can use xpub
        if client:
            client.used()
        return client

    def initialize_device(self, device_id, wizard, handler):
        # Initialization method
        msg = _("Choose how you want to initialize your {}.\n\n"
                "The first two methods are secure as no secret information "
                "is entered into your computer.\n\n"
                "For the last two methods you input secrets on your keyboard "
                "and upload them to your {}, and so you should "
                "only do those on a computer you know to be trustworthy "
                "and free of malware."
        ).format(self.device, self.device)
        choices = [
            # Must be short as QT doesn't word-wrap radio button text
            (TIM_NEW, _("Let the device generate a completely new seed randomly")),
            (TIM_RECOVER, _("Recover from a seed you have previously written down")),
            (TIM_MNEMONIC, _("Upload a BIP39 mnemonic to generate the seed")),
            (TIM_PRIVKEY, _("Upload a master private key"))
        ]
        def f(method):
            settings = self.request_trezor_init_settings(wizard, method, self.device)
            t = threading.Thread(target = self._initialize_device_safe,
                                 args=(settings, method, device_id, wizard, handler))
            t.setDaemon(True)
            t.start()
            wizard.loop.exec_()
        wizard.choice_dialog(title=_('Initialize Device'), message=msg,
                             choices=choices, run_next=f)

    def _initialize_device_safe(self, settings, method, device_id, wizard, handler):
        exit_code = 0
        try:
            self._initialize_device(settings, method, device_id, wizard, handler)
        except UserCancelled:
            exit_code = 1
        except Exception as e:
            handler.show_error(str(e))
            exit_code = 1
        finally:
            wizard.loop.exit(exit_code)

    def _initialize_device(self, settings, method, device_id, wizard, handler):
        item, label, pin_protection, passphrase_protection = settings

        language = 'english'
        client = app_state.device_manager.client_by_id(device_id)

        if method == TIM_NEW:
            strength = 64 * (item + 2)  # 128, 192 or 256
            client.reset_device(True, strength, passphrase_protection,
                                pin_protection, label, language)
        elif method == TIM_RECOVER:
            word_count = 6 * (item + 2)  # 12, 18 or 24
            client.step = 0
            client.recovery_device(False, word_count, passphrase_protection,
                                   pin_protection, label, language)
        elif method == TIM_MNEMONIC:
            pin = pin_protection  # It's the pin, not a boolean
            client.load_device_by_mnemonic(str(item), pin,
                                           passphrase_protection,
                                           label, language)
        else:
            pin = pin_protection  # It's the pin, not a boolean
            client.load_device_by_xprv(item, pin, passphrase_protection,
                                       label, language)

    def setup_device(self, device_info, wizard):
        '''Called when creating a new wallet.  Select the device to use.  If
        the device is uninitialized, go through the intialization
        process.'''
        device_id = device_info.device.id_
        client = app_state.device_manager.client_by_id(device_id)
        client.handler = self.create_handler(wizard)
        if not device_info.initialized:
            self.initialize_device(device_id, wizard, client.handler)
        client.get_xpub('m', 'standard')
        client.used()

    def get_xpub(self, device_id, derivation, xtype, wizard):
        client = app_state.device_manager.client_by_id(device_id)
        client.handler = self.create_handler(wizard)
        xpub = client.get_xpub(derivation, xtype)
        client.used()
        return xpub

    def sign_transaction(self, keystore, tx, xpub_path):
        self.xpub_path = xpub_path
        client = self.get_client(keystore)
        inputs = self.tx_inputs(tx, True)
        outputs = self.tx_outputs(keystore.get_derivation(), tx)
        signatures = client.sign_tx(self.get_coin_name(client), inputs, outputs,
                                    lock_time=tx.locktime)[0]
        tx.update_signatures(signatures)

    def show_address(self, wallet, address):
        keystore = wallet.get_keystore()
        client = self.get_client(keystore)
        change, index = wallet.get_address_index(address)
        derivation = keystore.derivation
        address_path = "%s/%d/%d"%(derivation, change, index)
        address_n = client.expand_path(address_path)
        script_type = self.types.SPENDADDRESS
        client.get_address(Net.KEEPKEY_DISPLAY_COIN_NAME, address_n,
                           True, script_type=script_type)

    def tx_inputs(self, tx, for_sig=False):
        inputs = []
        for txin in tx.inputs():
            txinputtype = self.types.TxInputType()
            if txin['type'] == 'coinbase':
                prev_hash = "\0"*32
                prev_index = 0xffffffff  # signed int -1
            else:
                if for_sig:
                    x_pubkeys = txin['x_pubkeys']
                    if len(x_pubkeys) == 1:
                        x_pubkey = x_pubkeys[0]
                        xpub, s = parse_xpubkey(x_pubkey)
                        xpub_n = self.client_class.expand_path(self.xpub_path[xpub])
                        txinputtype.address_n.extend(xpub_n + s)
                        txinputtype.script_type = self.types.SPENDADDRESS
                    else:
                        def f(x_pubkey):
                            if is_xpubkey(x_pubkey):
                                xpub, s = parse_xpubkey(x_pubkey)
                            else:
                                xpub = xpub_from_pubkey('standard', bfh(x_pubkey))
                                s = []
                            node = self.ckd_public.deserialize(xpub)
                            return self.types.HDNodePathType(node=node, address_n=s)
                        pubkeys = [f(x) for x in x_pubkeys]
                        multisig = self.types.MultisigRedeemScriptType(
                            pubkeys=pubkeys,
                            signatures=[bfh(x)[:-1] if x else b'' for x in txin.get('signatures')],
                            m=txin.get('num_sig'),
                        )
                        script_type = self.types.SPENDMULTISIG
                        txinputtype = self.types.TxInputType(
                            script_type=script_type,
                            multisig=multisig
                        )
                        # find which key is mine
                        for x_pubkey in x_pubkeys:
                            if is_xpubkey(x_pubkey):
                                xpub, s = parse_xpubkey(x_pubkey)
                                if xpub in self.xpub_path:
                                    xpub_n = self.client_class.expand_path(self.xpub_path[xpub])
                                    txinputtype.address_n.extend(xpub_n + s)
                                    break

                prev_hash = bytes.fromhex(txin['prevout_hash'])
                prev_index = txin['prevout_n']

            if 'value' in txin:
                txinputtype.amount = txin['value']
            txinputtype.prev_hash = prev_hash
            txinputtype.prev_index = prev_index

            if 'scriptSig' in txin:
                script_sig = bfh(txin['scriptSig'])
                txinputtype.script_sig = script_sig

            txinputtype.sequence = txin.get('sequence', 0xffffffff - 1)

            inputs.append(txinputtype)

        return inputs

    def tx_outputs(self, derivation, tx):
        outputs = []
        has_change = False

        for _type, address, amount in tx.outputs():
            info = tx.output_info.get(address)
            if info is not None and not has_change:
                has_change = True # no more than one change address
                index, xpubs, m = info
                if len(xpubs) == 1:
                    script_type = self.types.PAYTOADDRESS
                    address_n = self.client_class.expand_path(derivation + "/%d/%d"%index)
                    txoutputtype = self.types.TxOutputType(
                        amount = amount,
                        script_type = script_type,
                        address_n = address_n,
                    )
                else:
                    script_type = self.types.PAYTOMULTISIG
                    address_n = self.client_class.expand_path("/%d/%d"%index)
                    nodes = [self.ckd_public.deserialize(xpub) for xpub in xpubs]
                    pubkeys = [self.types.HDNodePathType(node=node, address_n=address_n)
                               for node in nodes]
                    multisig = self.types.MultisigRedeemScriptType(
                        pubkeys = pubkeys,
                        signatures = [b''] * len(pubkeys),
                        m = m)
                    txoutputtype = self.types.TxOutputType(
                        multisig = multisig,
                        amount = amount,
                        address_n = self.client_class.expand_path(derivation + "/%d/%d"%index),
                        script_type = script_type)
            else:
                txoutputtype = self.types.TxOutputType()
                txoutputtype.amount = amount
                if _type == TYPE_SCRIPT:
                    txoutputtype.script_type = self.types.PAYTOOPRETURN
                    txoutputtype.op_return_data = address.to_script()[2:]
                elif _type == TYPE_ADDRESS:
                    txoutputtype.script_type = self.types.PAYTOADDRESS
                    txoutputtype.address = address.to_string()

            outputs.append(txoutputtype)

        return outputs
