import unittest
from _contract_loader import load_adapter


class GateContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def emit(item):
            return None
        self.adapter = load_adapter('gate')({'id': 'fixture', 'api_key': 'fixture', 'api_secret': 'fixture'}, emit)

    def test_spot_preserves_fee_currency_and_execution_identity(self):
        e = self.adapter._norm_spot(dict(id='fill-7', currency_pair='BTC_USDT', price='100', amount='2',
            side='buy', fee='.04', fee_currency='USDT', create_time_ms='1793779200000'))
        self.assertEqual((e['execution_id'], e['qty'], e['price'], e['fee'], e['fee_currency']),
                         ('fill-7', 2, 100, .04, 'USDT'))

    def test_future_contract_value_and_reversal_parts(self):
        self.adapter._contract_spec = lambda *a: {'contract_multiplier': .01, 'contract_type': 'direct'}
        e = self.adapter._norm_fut(dict(id='fill-8', contract='BTC_USDT', size=-3, close_size=2,
            price='100', trade_value='3', fee='.04', create_time_ms='1793779200000'), use_position_snapshot=False)
        self.assertEqual(e['position_effect'], 'reversal')
        self.assertEqual((e['qty'], e['close_qty'], e['open_qty']), (3, 2, 1))
        self.assertAlmostEqual(e['contract_multiplier'], .01)
        self.assertEqual(e['notional'], 3)

    async def test_spot_pagination_collects_all_pages(self):
        calls = []
        def get(path, params):
            calls.append(params['page'])
            return [dict(id=str(i), currency_pair='BTC_USDT', create_time=i+1) for i in range(1000)] if params['page'] == 1 else [dict(id='last', currency_pair='BTC_USDT', create_time=1002)]
        self.adapter._get = get
        rows = await self.adapter._trade_rows('spot', 1, 2)
        self.assertEqual(len(rows), 1001)
        self.assertEqual(calls, [1, 2])

    async def test_repeated_page_fails_instead_of_partial_success(self):
        self.adapter._get = lambda *a: [dict(id=str(i), currency_pair='BTC_USDT', create_time=i) for i in range(1000)]
        with self.assertRaisesRegex(RuntimeError, '重复'):
            await self.adapter._trade_rows('spot', 1, 2)

    async def test_conflicting_fill_identity_fails(self):
        self.adapter._get = lambda *a: [dict(id='duplicate', currency_pair='BTC_USDT', price=1), dict(id='duplicate', currency_pair='BTC_USDT', price=2)]
        with self.assertRaisesRegex(RuntimeError, '冲突'):
            await self.adapter._trade_rows('spot', 1, 2)


if __name__ == '__main__':
    unittest.main()
