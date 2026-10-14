# coding: utf-8
import unittest

import easytrader
from easytrader.clienttrader import ClientTrader
from easytrader.universal_clienttrader import UniversalClientTrader


class TestClientTraderFactory(unittest.TestCase):
    """测试客户端工厂创建与废弃分支"""

    def test_create_ths_client(self):
        user = easytrader.use("ths")
        self.assertIsInstance(user, ClientTrader)

    def test_create_universal_client(self):
        user = easytrader.use("universal_client")
        self.assertIsInstance(user, UniversalClientTrader)

    def test_obsolete_brokers_raise_not_implemented(self):
        obsolete_brokers = [
            "yh_client", "银河客户端",
            "ht_client", "华泰客户端",
            "wk_client", "五矿客户端",
            "htzq_client", "海通证券客户端",
            "gj_client", "国金客户端",
            "gf_client", "广发客户端",
        ]
        for broker in obsolete_brokers:
            with self.assertRaises(NotImplementedError) as ctx:
                easytrader.use(broker)
            self.assertIn("已废弃", str(ctx.exception))

    def test_unknown_broker_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            easytrader.use("non_existent_broker")
        self.assertIn("不支持的券商或平台类型", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
