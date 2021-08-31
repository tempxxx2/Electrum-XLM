import os

from unittest.mock import patch

from electrum_dash.storage import WalletStorage

from . import ElectrumTestCase


class ReplaceWithPermissionErrorMock:
    '''Mock for windows os.replace where PermissionError can happen randomly'''

    def __init__(self, *args, **kwargs):
        self.error_count = 1
        self.os_replace = os.replace

    def __call__(self, src, dst):
        if self.error_count > 0:
            self.error_count -= 1
            raise PermissionError
        else:
            return self.os_replace(src, dst)


class TestWalletStorage(ElectrumTestCase):

    def test_write(self):
        path = os.path.join(self.electrum_path, 'default_wallet')
        storage = WalletStorage(path)
        data = 'testdata'
        storage.write(data)
        with open(path, 'r') as fd:
            assert fd.read() == data

    def test_write_with_permission_error_done(self):
        path = os.path.join(self.electrum_path, 'default_wallet')
        storage = WalletStorage(path)
        data = 'testdata'
        with patch('os.replace', new_callable=ReplaceWithPermissionErrorMock):
            storage.write(data)
        with open(path, 'r') as fd:
            assert fd.read() == data

    def test_write_with_permission_error_fails(self):
        path = os.path.join(self.electrum_path, 'default_wallet')
        storage = WalletStorage(path)
        storage.write_attempts = 1
        data = 'testdata'
        with patch('os.replace', new_callable=ReplaceWithPermissionErrorMock):
            with self.assertRaises(PermissionError):
                storage.write(data)
