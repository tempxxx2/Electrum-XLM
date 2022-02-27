import os
from enum import IntEnum
from functools import partial

from kivy.clock import Clock
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.properties import BooleanProperty
from kivy.uix.button import Button

from electrum_dash import util
from electrum_dash.network import deserialize_proxy
from electrum_dash.interface import PREFERRED_NETWORK_PROTOCOL

from electrum_dash.gui.kivy.i18n import _
from electrum_dash.gui.kivy.uix.dialogs.question import Question


Builder.load_string('''
<AppInfoDialog@Popup>
    title: ''
    auto_dismiss: False
    size_hint: 0.9, None
    height: 192 + max(msg_label.height, 64)
    BoxLayout:
        orientation: 'vertical'
        size_hint: 1, 1
        padding: '10dp'
        BoxLayout:
            size_hint: 1, None
            height: max(msg_label.height, 64)
            spacing: '10dp'
            orientation: 'horizontal'
            Image:
                id: img
                source: ''
                size_hint: None, None
                pos_hint: {'top': 1}
                width: 64
                height: 64
                allow_stretch: True
            Label:
                id: msg_label
                text_size: self.width, None
                height: self.texture_size[1]
                markup: True
                on_ref_press:
                    import webbrowser
                    webbrowser.open(args[1])
        Widget:
            size_hint: 1, None
            height: '48dp'
        Button:
            size_hint: 1, None
            height: '48dp'
            pos_hint: {'bottom': 1}
            text: _('OK')
            on_release: root.dismiss()


<ElectrumServersTab@BoxLayout>
    orientation: 'vertical'
    title: ''
    Label:
        padding: '10dp', '10dp'
        text: root.title
        size_hint: 1, None
        text_size: self.width, None
        height: self.texture_size[1]
    ScrollView:
        GridLayout:
            id: content
            cols: 1
            size_hint: 1, None
            height: self.minimum_height
            padding: '10dp'


<ElectrumServersDialog@Popup>
    title: 'Electrum Servers'
    tabs: tabs
    connected_tab_header: connected_tab_header
    connected_tab: connected_tab_header.content
    other_tab_header: other_tab_header
    other_tab: other_tab_header.content
    blacklist_tab_header: blacklist_tab_header
    blacklist_tab: blacklist_tab_header.content
    BoxLayout:
        orientation: 'vertical'
        TabbedPanel:
            id: tabs
            do_default_tab: False
            TabbedPanelHeader:
                id: connected_tab_header
                text: _('Connected')
            TabbedPanelHeader:
                id: other_tab_header
                text: _('Other')
            TabbedPanelHeader:
                id: blacklist_tab_header
                text: _('Blacklist')
        Button:
            size_hint: 1, None
            height: '48dp'
            pos_hint: {'bottom': 1}
            text: _('OK')
            on_release: root.dismiss()


<TorWarnDialog@Popup>
    title: ''
    auto_dismiss: False
    BoxLayout:
        id: vbox
        orientation: 'vertical'
        padding: '10dp'
        BoxLayout:
            size_hint: 1, None
            spacing: '10dp'
            orientation: 'horizontal'
            Image:
                id: warn_img
                source: 'atlas://electrum_dash/gui/kivy/theming/atlas/light/error'
                size_hint: None, None
                width: 64
                height: 64
                allow_stretch: True
            Label:
                id: warn_lbl
                text_size: self.width, None
                height: self.texture_size[1]
                markup: True
                on_ref_press:
                    import webbrowser
                    webbrowser.open(args[1])
        Widget:
            id: w_spacer
            size_hint: 1, 0.4
        BoxLayout:
            id: tor_auto_on_hbox
            size_hint: 1, None
            orientation: 'horizontal'
            CheckBox:
                size_hint: None, None
                active: root.tor_auto_on_bp
                on_active: root.toggle_tor_auto_on()
            Label:
                text: app.network.TOR_AUTO_ON_MSG
                text_size: self.width, None
                height: self.texture_size[1]
        BoxLayout:
            id: btns_vbox
            spacing: '10dp'
            orientation: 'vertical'
            Button:
                size_hint: 1, 0.1
                text: _('Continue without Tor')
                on_release: root.continue_without_tor()
            Button:
                size_hint: 1, 0.1
                text: _('Open Orbot app')
                on_release: root.open_orbot_app()
            Button:
                size_hint: 1, 0.1
                text: _('Detect Tor again')
                on_release: root.detect_tor_again()
            Button:
                size_hint: 1, 0.1
                text: _('Close wallet')
                on_release: root.close_wallet()
''')


class AppInfoDialog(Factory.Popup):

    def __init__(self, msg, is_err=False):
        Factory.Popup.__init__(self)
        self.title = _('Error') if is_err else _('Information')
        img_path = 'atlas://electrum_dash/gui/kivy/theming/atlas/light'
        img = self.ids.img
        img.source = f'{img_path}/error' if is_err else f'{img_path}/info'
        msg_label = self.ids.msg_label
        msg_label.text = msg


class ExServersTabs(IntEnum):
    CONNECTED = 0
    OTHER = 1
    BLACKLIST = 2


class ElectrumServersTab(Factory.BoxLayout):

    def __init__(self, servers_dlg, tab_type):
        super(ElectrumServersTab, self).__init__()
        self.servers_dlg = servers_dlg
        self.app = servers_dlg.app
        self.tab_type = tab_type
        if tab_type == ExServersTabs.CONNECTED:
            self.title = _('Connected servers')
        elif tab_type == ExServersTabs.OTHER:
            self.title = _('Other known servers')
        elif tab_type == ExServersTabs.BLACKLIST:
            self.title = _('Blacklist')
        self.update()

    def update(self):
        c = self.ids.content
        c.clear_widgets()
        n = self.app.network
        if not n:
            return
        if self.tab_type == ExServersTabs.CONNECTED:
            self.update_connected()
        elif self.tab_type == ExServersTabs.OTHER:
            self.update_other()
        elif self.tab_type == ExServersTabs.BLACKLIST:
            self.update_blacklist()

    def update_connected(self):
        c = self.ids.content
        n = self.app.network
        for i in n.interfaces.copy():
            title = i.net_addr_str()
            if n.interface and i == n.interface.server:
                descr = _('Server for addresses and transactions')
            else:
                descr = ''
            cs = Factory.CardSeparator()
            c.add_widget(cs)
            si = Factory.SettingsItem(title=title, description=descr)
            si.action = partial(self.server_action, title)
            c.add_widget(si)

    def update_other(self):
        c = self.ids.content
        n = self.app.network
        blacklist = n.blacklist
        blacklist_servers = list(blacklist.keys())
        use_tor = n.proxy_is_tor(n.proxy) if n.proxy else False
        connected_hosts = set([i.host for i in n.interfaces])
        protocol = PREFERRED_NETWORK_PROTOCOL
        for _host, d in sorted(n.get_servers().items()):
            if _host in connected_hosts:
                continue
            if _host.endswith('.onion') and not use_tor:
                continue
            port = d.get(protocol)
            title = ''
            if port:
                title = f'{_host}:{port}'
                if title in blacklist_servers:
                    continue
            descr = ''
            cs = Factory.CardSeparator()
            c.add_widget(cs)
            si = Factory.SettingsItem(title=title, description=descr)
            si.action = partial(self.server_action, title)
            c.add_widget(si)

    def update_blacklist(self):
        c = self.ids.content
        n = self.app.network
        blacklist = n.blacklist
        blacklist_servers = list(blacklist.keys())
        for title in blacklist_servers:
            descr = str(blacklist[title][0])
            descr = f'Info: {descr}' if descr else ''
            cs = Factory.CardSeparator()
            c.add_widget(cs)
            si = Factory.SettingsItem(title=title, description=descr)
            si.action = partial(self.server_action, title)
            c.add_widget(si)

    def server_action(self, server_str, dt):
        if self.tab_type in [ExServersTabs.CONNECTED, ExServersTabs.OTHER]:
            q = _('Blacklist {}').format(server_str)
            action = self.servers_dlg.blacklist_server
        else:
            q = _('Unblacklist {}').format(server_str)
            action = self.servers_dlg.unblacklist_server
        def on_want_action(b):
            if b:
                action(server_str)
        d = Question(q, on_want_action)
        d.open()


class ElectrumServersDialog(Factory.Popup):

    def __init__(self, app):
        Factory.Popup.__init__(self)

        self.app = app
        self.network = app.network
        self.connected_tab_header.content = \
            ElectrumServersTab(self, ExServersTabs.CONNECTED)
        self.other_tab_header.content = \
            ElectrumServersTab(self, ExServersTabs.OTHER)
        self.blacklist_tab_header.content = \
            ElectrumServersTab(self, ExServersTabs.BLACKLIST)
        self.tabs.switch_to(self.connected_tab_header)

    def open(self):
        super(ElectrumServersDialog, self).open()
        util.register_callback(self.on_net_callback, ['network_updated'])

    def dismiss(self):
        super(ElectrumServersDialog, self).dismiss()
        util.unregister_callback(self.on_net_callback)

    def on_net_callback(self, event, *args):
        Clock.schedule_once(lambda dt: self.on_net_event(event, *args))

    def on_net_event(self, event, *args):
        if event == 'network_updated':
            self.update()

    def update(self):
        self.connected_tab.update()
        self.other_tab.update()
        self.blacklist_tab.update()

    def blacklist_server(self, server_str):
        self.network.add_blacklist_server(server_str)
        self.update()

    def unblacklist_server(self, server_str):
        self.network.remove_blacklist_server(server_str)
        self.update()


class TorWarnDialog(Factory.Popup):

    tor_auto_on_bp = BooleanProperty()

    def __init__(self, app, w_path, continue_load):
        self.app = app
        self.continue_load = continue_load
        self.can_hide = False
        self.config = app.electrum_config
        self.net = net = app.network
        self.tor_detected = False

        Factory.Popup.__init__(self)
        app_name = 'Dash Electrum'
        w_basename = os.path.basename(w_path)
        self.title = f'{app_name}  -  {w_basename}'

        warn_lbl = self.ids.warn_lbl
        warn_lbl.text = net.TOR_WARN_MSG_KIVY
        self.tor_auto_on_bp = self.config.get('tor_auto_on', True)

    def on_dismiss(self):
        if not self.can_hide:
            return True

    def toggle_tor_auto_on(self):
        self.tor_auto_on_bp = not self.config.get('tor_auto_on', True)
        self.config.set_key('tor_auto_on', self.tor_auto_on_bp, True)

    def continue_without_tor(self):
        net = self.net
        net_params = net.get_parameters()
        if net_params.proxy:
            host = net_params.proxy['host']
            port = net_params.proxy['port']
            if host == '127.0.0.1' and port in ['9050', '9150']:
                net_params = net_params._replace(proxy=None)
                coro = net.set_parameters(net_params)
                net.run_from_another_thread(coro)
        self.continue_load()
        self.can_hide = True
        self.dismiss()

    def open_orbot_app(self):
        err = self.app.run_other_app('org.torproject.android')
        if err:
            self.app.show_error(err)

    def detect_tor_again(self):
        net = self.net
        self.tor_detected = net.detect_tor_proxy()
        if self.tor_detected:
            net_params = net.get_parameters()
            proxy = deserialize_proxy(self.tor_detected)
            net_params = net_params._replace(proxy=proxy)
            coro = net.set_parameters(net_params)
            net.run_from_another_thread(coro)

            self.title = _('Information')
            self.ids.warn_lbl.text = _('Tor proxy detected')
            w_img = self.ids.warn_img
            w_img.source = 'atlas://electrum_dash/gui/kivy/theming/atlas/light/info'
            vbox = self.ids.vbox
            vbox.remove_widget(self.ids.tor_auto_on_hbox)
            vbox.remove_widget(self.ids.btns_vbox)
            self.ids.w_spacer.size_hint = (1, 0.7)
            ok_btn = Button(text=_('OK'), size_hint=(1, 0.1))
            ok_btn.bind(on_press=self.on_ok)
            vbox.add_widget(ok_btn)

    def on_ok(self, instance):
        self.continue_load()
        self.can_hide = True
        self.dismiss()

    def close_wallet(self):
        if not self.app.wallet:
            from kivy.base import stopTouchApp
            stopTouchApp()
        else:
            self.can_hide = True
            self.dismiss()
