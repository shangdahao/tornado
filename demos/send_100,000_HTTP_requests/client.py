from tornado import ioloop, httpclient

i = 0


def handle_request(response):
    global i
    print(response.code, 'Response index: ', i)
    i -= 1
    if i == 0:
        ioloop.IOLoop.instance().stop()

http_client = httpclient.AsyncHTTPClient()
for url in open('./urls.txt'):
    print('Request index: ', i)
    i += 1
    http_client.fetch(url.strip(), handle_request, method='HEAD')
ioloop.IOLoop.instance().start()
