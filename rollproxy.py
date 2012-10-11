#!/usr/bin/python

__version__ = '0.5.2'

import __future__
import sys
import argparse
import logging
import SocketServer
import BaseHTTPServer
import threading
import time
import urlparse
import urllib3
import binascii
import base64
import re
import json
import math


class Pool:

    timeout = 10
    lp_timeout = None
    default_expire = 60

    def __init__(self, url, maxsize, proxy=None):
        self.url = url
        self.maxsize = maxsize
        self.proxy = proxy
        if url.find('://') < 0:
            url = 'http://' + url
        p = urlparse.urlsplit(url)
        i = p.netloc.rfind('@')
        self.host = p.netloc[i + 1:]
        if i >= 0:
            self.auth = ('Basic ' +
                         base64.b64encode(p.netloc[:i].encode('U8')).decode())
        else:
            self.auth = None
        self.path = urlparse.urlunsplit(('', '', p.path or '/', p.query, ''))
        self.fullpath = 'http://' + self.host + self.path
        if proxy:
            self.path = self.fullpath
        self.pool = urllib3.HTTPConnectionPool(proxy if proxy else self.host,
                                               maxsize=maxsize, block=True)
        self.lp_header = None
        self.lp_pool = self.pool
        self.lp_host = self.host
        self.lp_path = self.path
        self.expire = 0

    def request(self, method, body, headers, lp=False):
        headers['host'] = self.lp_host if lp else self.host
        if self.auth:
            headers['authorization'] = self.auth
        elif 'authorization' in headers:
            del headers['authorization']
        pool = self.lp_pool if lp else self.pool
        r = pool.urlopen(method, self.lp_path if lp else self.path,
                         body, headers, assert_same_host=False,
                         timeout=self.lp_timeout if lp else self.timeout)
        for h in ('content-encoding', 'transfer-encoding'):
            if h in r.headers:
                del r.headers[h]
        if ('x-long-polling' in r.headers and
                self.lp_header != r.headers['x-long-polling']):
            self.lp_header = r.headers['x-long-polling']
            p = urlparse.urlsplit(self.lp_header)
            self.lp_host = p.netloc or self.host
            if self.proxy:
                self.lp_path = urlparse.urljoin(self.fullpath, self.lp_header)
            else:
                self.lp_path = urlparse.urlunsplit(('', '', p.path or '/',
                                                    p.query, ''))
                if self.lp_host == self.host:
                    self.lp_pool = self.pool
                else:
                    self.lp_pool = urllib3.HTTPConnectionPool(p.netloc,
                                              maxsize=self.maxsize, block=True)
        return r

    def __str__(self):
        return self.fullpath


class Work:

    timeout = 10

    def __init__(self):
        self.lock = threading.Lock()
        self.time = 0
        self.headers = None
        self.data = None
        self.rolls_left = 0

    def roll_ntime(self):
        ntime = int(self.data['result']['data'][136:144], 16) + 1
        self.data['result']['data'] = (self.data['result']['data'][:136] +
                                       '%08x' % ntime +
                                       self.data['result']['data'][144:])

    def is_rollable(self):
        return self.rolls_left > 0 and time.time() - self.time < self.timeout


pools = []
pool_index = 0
work = Work()
lps = []
lps_lock = threading.Lock()
workbases = {}
workbases_lock = threading.Lock()
workers = {}
workers_lock = threading.Lock()
solutions = []
rejections = []
stats = {'start_time': time.time(),
         'local_gets': 0, 'upstream_gets': 0, 'solutions': 0, 'accepted': 0}
stats_lock = threading.Lock()


class RollProxyHandler(BaseHTTPServer.BaseHTTPRequestHandler):

    getwork_path = '/'
    getwork_lp_path = '/LP'
    force_rolls = False
    rate_time = 900

    def handle_one_request(self):
        try:
            BaseHTTPServer.BaseHTTPRequestHandler.handle_one_request(self)
        except:
            logging.error(sys.exc_info()[1])

    def finish(self):
        try:
            BaseHTTPServer.BaseHTTPRequestHandler.finish(self)
        except:
            pass

    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        logging.error('%s - %s' % (self.requestline, format), *args)

    def send_response_only(self, code, message=None):
        if message is None:
            if code in self.responses:
                message = self.responses[code][0]
            else:
                message = ''
        if self.request_version != 'HTTP/0.9':
            self.wfile.write(('%s %d %s\r\n' %
                              (self.protocol_version,
                               code, message)).encode('latin1'))

    def respond(self, code, headers, body):
        self.send_response_only(code)
        for k, v in headers.iteritems():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global pool_index
        if self.path not in (self.getwork_path, self.getwork_lp_path):
            self.send_error(404)
            return
        headers = dict((k.lower(), v) for k, v in self.headers.items())
        try:
            length = int(headers['content-length'])
            body = self.rfile.read(length).decode()
            data = json.loads(body)
        except:
            body = None
        is_get = not body or (data['method'] == 'getwork' and
                              len(data['params']) == 0)
        is_lp = self.path == self.getwork_lp_path
        username = '-'
        if 'authorization' in headers:
            m = re.match('Basic (.+)', headers['authorization'])
            if m:
                auth = base64.b64decode(m.group(1).encode()).decode('U8')
                username = auth.split(':')[0]
        with workers_lock:
            if username not in workers:
                workers[username] = {'solutions': 0, 'accepted': 0,
                                     'first_time': time.time()}
            workers[username]['last_time'] = time.time()
        if is_get:
            with stats_lock:
                stats['local_gets'] += 1
        else:
            try:
                base = workbases[data['params'][0][:136]]
            except:
                logging.error('Unknown work is being submitted')
                base = {'pool': pools[pool_index], 'value': 0}
        if is_lp:
            lps_lock.acquire()
            for lp in lps:
                if lp.rolls_left > 0:
                    lp.rolls_left -= 1
                    lps_lock.release()
                    lp.lock.acquire()
                    if not lp.data:
                        lp.lock.release()
                        is_lp = False
                        break
                    lp.roll_ntime()
                    body = json.dumps(lp.data)
                    lp.lock.release()
                    self.respond(200, lp.headers, body.encode())
                    return
        if is_get and not is_lp:
            work.lock.acquire()
            if work.is_rollable():
                work.roll_ntime()
                work.rolls_left -= 1
                body = json.dumps(work.data)
                work.lock.release()
                self.respond(200, work.headers, body.encode())
                return
        if is_lp:
            lps[:] = (w for w in lps if (not w.time or
                                         time.time() - w.time < 60))
            lp = Work()
            lp.lock.acquire()
            lp.rolls_left = pools[pool_index].expire
            lps.insert(0, lp)
            lps_lock.release()
        try:
            if 'x-mining-extensions' in headers:
                t = headers['x-mining-extensions'].split(' ')
                if 'rollntime' not in t:
                    t.append('rollntime')
                if 'noncerange' in t:
                    t.remove('noncerange')
                headers['x-mining-extensions'] = ' '.join(t)
            else:
                headers['x-mining-extensions'] = 'rollntime'
            while True:
                try:
                    if is_get:
                        pool = pools[pool_index]
                    else:
                        pool = base['pool']
                    if is_get:
                        with stats_lock:
                            stats['upstream_gets'] += 1
                    r = pool.request(self.command, body, headers, is_lp)
                    if r.status != 200:
                        logging.error('%d %s', r.status, r.reason)
                    data = json.loads(r.data.decode())
                    if data['error']:
                        logging.error(data['error']['message'])
                    if data['error'] == None or not is_get or len(pools) == 1:
                        break
                except urllib3.exceptions.TimeoutError:
                    if is_lp:
                        continue
                    logging.error('Upstream request timed out')
                except:
                    logging.error(sys.exc_info()[1])
                    if not is_get or len(pools) == 1:
                        self.send_error(502)
                        return
                if len(pools) > 1:
                    pool_index = (pool_index + 1) % len(pools)
                    logging.warning('Switching to %s', pools[pool_index])
                time.sleep(1)
            if r.status != 200 or data['error']:
                body = r.data
            else:
                body = json.dumps(data).encode()
                r.headers['content-length'] = str(len(body))
                if 'x-long-polling' in r.headers:
                    r.headers['x-long-polling'] = self.getwork_lp_path
                if is_get:
                    expire = 0
                    if ('x-roll-ntime' in r.headers and
                            r.headers['x-roll-ntime'] not in ('', 'N')):
                        m = re.match('expire=(\d+)', r.headers['x-roll-ntime'])
                        if m:
                            expire = int(m.group(1))
                        else:
                            expire = Pool.default_expire
                        del r.headers['x-roll-ntime']
                    elif self.force_rolls:
                        expire = Pool.default_expire
                    pool.expire = expire
                    if is_lp:
                        logging.info('Long polling pushed new work')
                        work.rolls_left = 0
                    if expire > 0:
                        if is_lp:
                            lp.headers = r.headers
                            lp.data = data
                        else:
                            logging.info('New work fetched')
                            work.time = time.time()
                            work.headers = r.headers
                            work.data = data
                            work.rolls_left = expire
                    else:
                        logging.warning('Work cannot be reused')
                    id = data['result']['data'][:136]
                    target = data['result']['target'].encode()
                    target = binascii.hexlify(binascii.unhexlify(target)[::-1])
                    value = 2 ** 256 // (int(target, 16) + 1)
                    base = {'time': time.time(), 'pool': pool, 'value': value}
                    with workbases_lock:
                        if not is_lp:
                            tmin = time.time() - 600
                            for k, v in workbases.items():
                                if v['time'] < tmin:
                                    del workbases[k]
                        workbases[id] = base
                else:
                    sol = {'time': time.time(), 'result': data['result'],
                           'address': self.address_string(),
                           'username': username, 'value': base['value'],
                           'pool': base['pool']}
                    result = str(data['result'])
                    if 'x-reject-reason' in r.headers:
                        result += ' (%s)' % r.headers['x-reject-reason']
                        sol['reason'] = r.headers['x-reject-reason']
                    logging.info('Proof of work result: %s', result)
                    with stats_lock:
                        stats['solutions'] += 1
                        if data['result']:
                            stats['accepted'] += 1
                    with workers_lock:
                        if solutions and (time.time() - solutions[0]['time'] >
                                          2 * self.rate_time):
                            tmin = time.time() - self.rate_time
                            solutions[:] = (s for s in solutions if
                                            s['time'] > tmin)
                        solutions.append(sol)
                        if not data['result']:
                            rejections.append(sol)
                        workers[username]['solutions'] += 1
                        if data['result']:
                            workers[username]['accepted'] += 1
            self.respond(r.status, r.headers, body)
        finally:
            if is_get:
                if is_lp:
                    lp.time = time.time()
                    lp.rolls_left = 0
                    lp.lock.release()
                else:
                    work.lock.release()

    def do_GET(self):
        if self.path == self.getwork_lp_path:
            self.do_POST()
            return
        if self.path != '/':
            self.send_error(404)
            return
        now = time.time()
        body = '<!DOCTYPE html><head><title>RollProxy</title></head>'
        body += '<body><h1>RollProxy %s</h1>' % __version__
        body += '<p>Upstream server: %s</p>' % pools[pool_index]
        uptime = now - stats['start_time']
        body += '<p>Uptime: %.3f days</p>' % (uptime / 86400.)
        ratio = float(stats['local_gets']) / max(1, stats['upstream_gets'])
        body += ('<p>Getwork requests: %d local, %d upstream,'
                 ' %.1f:1 ratio</p>' %
                 (stats['local_gets'], stats['upstream_gets'], ratio))
        rejected = stats['solutions'] - stats['accepted']
        eff = 100. * stats['accepted'] / max(1, stats['solutions'])
        body += ('<p>Submissions: %d total, %d rejected,'
                 ' %.2f%% efficiency</p>' %
                 (stats['solutions'], rejected, eff))
        with workers_lock:
            rate = (sum(s['value'] for s in solutions
                        if now - s['time'] < self.rate_time) /
                    min(uptime, self.rate_time))
            rate = self.format_rate(rate, len(solutions))
            body += '<p>Current rate estimate: %s</p>' % rate
            body += '<h2>Most Recent Submissions</h2><table border="1">'
            body += '<tr><th>Time</th><th>Result</th><th>Worker</th>'
            body += '<th>Address</th><th>Difficulty</th><th>Server</th></tr>'
            for s in solutions[:-9:-1]:
                t = time.strftime('%Y-%m-%d %H:%M:%S %Z',
                                  time.localtime(s['time']))
                r = str(s['result'])
                if 'reason' in s:
                    r += ' (%s)' % s['reason']
                d = s['value'] * 0xffff * 2 ** -48
                body += ('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                         '<td>%.8f</td><td>%s</td></tr>' %
                         (t, r, s['username'], s['address'], d, s['pool']))
            body += '</table>'

            body += '<h2>Most Recent Rejections</h2><table border="1">'
            body += '<tr><th>Time</th><th>Reason</th><th>Worker</th>'
            body += '<th>Address</th><th>Difficulty</th><th>Server</th></tr>'
            for s in rejections[:-9:-1]:
                t = time.strftime('%Y-%m-%d %H:%M:%S %Z',
                                  time.localtime(s['time']))
                r = s['reason'] if 'reason' in s else '-'
                d = s['value'] * 0xffff * 2 ** -48
                body += ('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                         '<td>%.8f</td><td>%s</td></tr>' %
                         (t, r, s['username'], s['address'], d, s['pool']))
            body += '</table>'

            for k in workers.iterkeys():
                workers[k]['points'] = 0
                workers[k]['hashes'] = 0
            for s in solutions:
                if now - s['time'] < self.rate_time:
                    workers[s['username']]['points'] += 1
                    workers[s['username']]['hashes'] += s['value']
            body += '<h2>Workers</h2><table border="1">'
            body += '<tr><th>Worker</th><th>Rate</th><th>Submissions</th>'
            body += '<th>Rejections</th><th>Efficiency</th>'
            body += '<th>Last contact</th></tr>'
            for k in sorted(workers.iterkeys()):
                w = workers[k]
                t = self.format_dt(now - w['last_time']) + ' ago'
                r = self.format_rate(w['hashes'] / min(now - w['first_time'],
                                                       self.rate_time),
                                     w['points'])
                rejected = w['solutions'] - w['accepted']
                eff = (100. * w['accepted'] / max(1, w['solutions']))
                body += ('<tr><td>%s</td><td>%s</td><td>%d</td>'
                         '<td>%d</td><td>%.2f%%</td><td>%s</td></tr>' %
                         (k, r, w['solutions'], rejected, eff, t))
            body += '</table>'
        body += '</body></html>'
        body = body.encode('U8')
        headers = {'Date': self.date_time_string(),
                   'Content-Type': 'text/html; charset=utf-8',
                   'Content-Length': str(len(body))}
        self.respond(200, headers, body)

    def format_dt(self, dt):
        for v, n in ((86400, 'days'), (3600, 'hours'), (60, 'minutes'),
                     (1, 'seconds')):
            if dt >= 2 * v:
                break
        return '%d %s' % (dt // v, n)

    def format_rate(self, rate, points):
        prefixes = ('', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
        k = min(int(math.log10(max(1, rate)) // 3), len(prefixes) - 1)
        rate /= 1e3 ** k
        p = max(0, min(3, int(math.log10(0.5 * points / rate)))) if rate else 0
        return '%.*f %sH/s' % (p, round(rate, p), prefixes[k])


class ThreadingHTTPServer(SocketServer.ThreadingMixIn,
                          BaseHTTPServer.HTTPServer): pass


if __name__ == '__main__':
    ap = argparse.ArgumentParser(fromfile_prefix_chars='@')
    ap.add_argument('urls', metavar='URL', nargs='+',
                    help='URL of upstream mining server'
                         ' (including credentials)')
    ap.add_argument('-p', '--port', dest='port', type=int, default=8345,
                    help='port to serve on (default: 8345)')
    ap.add_argument('-n', '--size', dest='n', type=int, default=100,
                    help='maximum number of connections to each upstream'
                         ' server (default: 100)')
    ap.add_argument('-T', '--timeout', dest='timeout', type=float,
                    metavar='N', default=10,
                    help='timeout for regular getwork requests, in seconds'
                         ' (default: 10)')
    ap.add_argument('--lp-timeout', dest='lp_timeout', type=float,
                    metavar='N',
                    help='set a timeout for long polling requests, in seconds')
    ap.add_argument('-L', '--lifetime', dest='lifetime', type=float,
                    metavar='N', default=10,
                    help='number of seconds a work unit can be reused for'
                         ' (default: 10)')
    ap.add_argument('-f', '--force', dest='force', action='store_true',
                    help='force reuse of work units')
    ap.add_argument('-x', '--proxy', dest='proxy', metavar='HOST:PORT',
                    help='connect via a HTTP proxy')
    ap.add_argument('-v', '--verbose', dest='verbosity', action='count',
                    help='increase output verbosity')
    args = ap.parse_args()

    if args.verbosity > 1:
        loglevel = logging.DEBUG
    elif args.verbosity > 0:
        loglevel = logging.INFO
    else:
        loglevel = logging.WARNING
    logging.basicConfig(level=loglevel, format='[%(asctime)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    Pool.timeout = args.timeout
    Pool.lp_timeout = args.lp_timeout
    Work.timeout = args.lifetime
    pools = [Pool(u, args.n, proxy=args.proxy) for u in args.urls]
    RollProxyHandler.protocol_version = 'HTTP/1.1'
    RollProxyHandler.force_rolls = args.force
    server = ThreadingHTTPServer(('', args.port), RollProxyHandler)
    server.daemon_threads = True;
    logging.info('Serving HTTP on port %d' % args.port)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        logging.info('Shutting down')
