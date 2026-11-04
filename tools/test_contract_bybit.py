import unittest
from _contract_loader import load_adapter


class BybitContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        async def emit(item):
            self.events.append(item)
        self.adapter = load_adapter('bybit')({'id': 'fixture', 'api_key': 'fixture', 'api_secret': 'fixture'}, emit)

    def fill(self, **extra):
        return dict(execId='fill-1', orderId='order-1', symbol='BTCUSDT', category='linear',
                    side='Buy', execPrice='100', execQty='2', execFee='0.04', feeCurrency='USDT',
                    execTime='1793779200000', **extra)

    def test_execution_id_quantity_price_fee_and_time(self):
        event = self.adapter._normalize(self.fill())
        self.assertEqual((event['execution_id'], event['price'], event['qty'], event['fee'], event['exec_time']),
                         ('fill-1', 100, 2, .04, 1793779200000))
        self.assertEqual(event['fee_currency'], 'USDT')

    def test_spot_and_derivative_models_remain_distinct(self):
        row = self.fill()
        self.assertEqual(self.adapter._normalize(row)['value_model'], 'crypto_derivative')
        row['category'] = 'spot'
        self.assertEqual(self.adapter._normalize(row)['value_model'], 'spot')

    async def test_confirmed_protective_stop_does_not_become_candidate(self):
        await self.adapter._handle_order_stop({'data': [dict(category='linear', orderStatus='Untriggered',
            stopOrderType='StopLoss', symbol='BTCUSDT', side='Sell', triggerPrice='90',
            triggerDirection=2, reduceOnly=True, positionIdx=0, orderId='stop-1')]})
        events = [e for e in self.events if e['type'] == 'risk_update']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['risk_type'], 'protective_stop')
        self.assertEqual(events[0]['protective_stop_price'], 90)

    async def test_history_error_never_claims_success(self):
        self.adapter._rest_get = lambda *a: {'retCode': 10001, 'retMsg': 'fixture-error'}
        with self.assertRaises(RuntimeError):
            await self.adapter._history_category('linear', 1, 2)
        self.assertEqual(self.events, [])

    async def test_missing_execution_id_rejected(self):
        row = self.fill()
        row.pop('execId')
        self.adapter._rest_get = lambda *a: {'retCode': 0, 'result': {'list': [row], 'nextPageCursor': ''}}
        with self.assertRaisesRegex(RuntimeError, 'execId'):
            await self.adapter._history_category('linear', 1, 2)


if __name__ == '__main__':
    unittest.main()
