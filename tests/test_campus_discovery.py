import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import seu_isp_login as login


class CampusDiscoveryTests(unittest.TestCase):
    source = "10.200.1.5"
    mac = "001122334455"
    isp = ("SEU-ISP", source, mac)
    wlan = ("SEU-WLAN", source, mac)
    cfg = {**login.DEFAULT_CONFIG, "account": "test", "password": "test-password"}

    def test_discovers_port_from_trusted_school_asset(self):
        def read(port, target, source, timeout):
            path = urlsplit(target).path
            self.assertNotIn("user_password", parse_qs(urlsplit(target).query))
            if (port, path) == (443, "/"):
                return '<script src="https://other.test/out.js"></script><script src="/campus-v2.js?v=3"></script>'
            if (port, path) == (443, "/campus-v2.js"):
                return "var enHTTPSPort = 814;"
            self.assertEqual((port, path), (814, "/eportal/portal/page/loadConfig"))
            return 'dr1003({"code":1,"data":{"login_method":1,"en_md5":0}})'

        with patch.object(login, "campus_portal_get", side_effect=read):
            self.assertEqual(login.discover_campus_route("SEU-ISP", self.source, 4), login.CampusRoute("portal", 814))

    def test_unknown_method_does_not_authenticate(self):
        with patch.multiple(login, campus_source=Mock(return_value=self.isp), load_campus_cache=Mock(return_value={}),
                            campus_portal_get=Mock(side_effect=["enHTTPSPort=814;", '{"code":1,"data":{"login_method":9}}']),
                            log=Mock(), credentials_for=Mock()):
            self.assertEqual(login.login_campus_wifi(self.cfg, once=True), 1)
            login.credentials_for.assert_not_called()

    def test_stale_port_refreshes_once_and_verifies_before_caching(self):
        old = login.CampusRoute("portal", 804)
        new = login.CampusRoute("portal", 814)
        steps = []

        def read(port, target, source, timeout):
            url = urlsplit(target)
            steps.append((port, url.path))
            if port == 804:
                raise login.CampusPortalHTTPError(404)
            if url.path.endswith("online_list"):
                online = len(steps) == 4
                return json.dumps({"result": int(online), "list": [{"online_ip": source}] if online else []})
            self.assertEqual(parse_qs(url.query)["user_account"], ["test@cmcc"])
            return '{"result":1}'

        discover, remember = Mock(return_value=new), Mock()
        with patch.multiple(login, campus_source=Mock(return_value=self.isp),
                            load_campus_cache=Mock(return_value={"SEU-ISP": old}),
                            campus_portal_get=read, discover_campus_route=discover,
                            cache_campus_route=remember, connectivity_ok=Mock(return_value=(True, "")), log=Mock()):
            self.assertEqual(login.login_campus_wifi(self.cfg, once=True), 0)
        discover.assert_called_once()
        remember.assert_called_once_with(self.cfg, {"SEU-ISP": old}, "SEU-ISP", new)
        self.assertEqual(steps, [(804, "/eportal/portal/online_list"), (814, "/eportal/portal/online_list"),
                                 (814, "/eportal/portal/login"), (814, "/eportal/portal/online_list")])

    def test_auth_rejection_does_not_refresh_or_cache(self):
        discover, remember = Mock(), Mock()
        with patch.multiple(login, campus_source=Mock(return_value=self.isp),
                            load_campus_cache=Mock(return_value={"SEU-ISP": login.CAMPUS_DEFAULT_ROUTES["SEU-ISP"]}),
                            campus_route_online=Mock(return_value=False), campus_portal_get=Mock(return_value='{"result":0,"msg":"密码错误"}'),
                            discover_campus_route=discover, cache_campus_route=remember, log=Mock()):
            self.assertEqual(login.login_campus_wifi(self.cfg, once=True), 2)
        discover.assert_not_called()
        remember.assert_not_called()

    def test_isp_accepts_current_and_legacy_unicom_suffixes(self):
        self.assertEqual(login.CARRIERS["3"][0], "@unicom")
        routes = {"SEU-ISP": login.CAMPUS_DEFAULT_ROUTES["SEU-ISP"]}
        for suffix in ("@unicom", "@lt"):
            with self.subTest(suffix=suffix):
                cfg = {**self.cfg, "profiles": [{"ssid": "SEU-ISP", "isp_suffix": suffix}]}
                read = Mock(return_value='{"result":1}')
                with patch.multiple(login, campus_source=Mock(return_value=self.isp),
                                    load_campus_cache=Mock(return_value=routes), campus_portal_get=read,
                                    campus_route_online=Mock(side_effect=[False, True]), log=Mock(),
                                    cache_campus_route=Mock(), connectivity_ok=Mock(return_value=(True, ""))):
                    self.assertEqual(login.login_campus_wifi(cfg, once=True), 0)
                read.assert_called_once()
                query = parse_qs(urlsplit(read.call_args.args[1]).query)
                self.assertEqual(query["user_account"], ["test@unicom"])

    def test_switching_to_wlan_before_submission_sends_no_credentials(self):
        read = Mock(return_value='{"result":0}')
        with patch.multiple(login, campus_source=Mock(side_effect=[self.isp, self.wlan]),
                            load_campus_cache=Mock(return_value={"SEU-ISP": login.CAMPUS_DEFAULT_ROUTES["SEU-ISP"]}),
                            campus_portal_get=read, log=Mock()):
            self.assertEqual(login.login_campus_wifi(self.cfg, once=True), 0)
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("user_password", read.call_args.args[1])

    def test_wlan_keeps_classic_login_when_it_is_available(self):
        read = Mock(side_effect=['{"result":0}', '{"result":1}', json.dumps({"result": 1, "v4ip": self.source})])
        discover = Mock()
        with patch.multiple(login, campus_source=Mock(return_value=self.wlan), load_campus_cache=Mock(return_value={}),
                            campus_portal_get=read, discover_campus_route=discover, cache_campus_route=Mock(),
                            connectivity_ok=Mock(return_value=(True, "")), log=Mock()):
            self.assertEqual(login.login_campus_wifi(self.cfg, once=True), 0)
        discover.assert_not_called()
        self.assertEqual([request.args[0] for request in read.call_args_list], [443, 802, 443])
        query = parse_qs(urlsplit(read.call_args_list[1].args[1]).query)
        self.assertEqual(query["user_account"], [",0,test"])

    def test_cache_preserves_both_ssids_and_filters_bad_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "cache.json")
            path.write_text('{"SEU-ISP":{"protocol":"portal","port":true},"hotspot":{"protocol":"portal","port":814}}')
            with patch.multiple(login, BASE_DIR=directory, CAMPUS_CACHE_PATH=str(path)):
                cache = login.load_campus_cache()
                self.assertEqual(cache, {})
                for ssid, route in login.CAMPUS_DEFAULT_ROUTES.items():
                    login.cache_campus_route(self.cfg, cache, ssid, route)
                self.assertEqual(login.load_campus_cache(), login.CAMPUS_DEFAULT_ROUTES)
            for row in json.loads(path.read_text()).values():
                self.assertEqual(set(row), {"protocol", "port"})
