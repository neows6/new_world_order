from broker.market_data import SchwabMarketData
m = SchwabMarketData()
resp = m._get_client().get_quote('AAPL')
print('status:', resp.status_code)
print('body:', resp.text[:500])
