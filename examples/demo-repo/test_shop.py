import configparser
import unittest


class ShopTest(unittest.TestCase):
    def test_config_has_currency(self):
        cfg = configparser.ConfigParser()
        cfg.read("config.ini")
        self.assertEqual(cfg["shop"]["currency"], "EUR")


if __name__ == "__main__":
    unittest.main()
