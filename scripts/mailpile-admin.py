#!/usr/bin/env python2
#
# This is the Mailpile admin tool! It can do these things:
#
#  - Configure Apache for use with Mailpile (multi-user, proxying)
#  - Start or stop a user's Mailpile (in a screen session)
#  - Function as a CGI script to start Mailpile and reconfigure Apache
#
import argparse
import cgi
import getpass
import json
import os
import pwd
import random
import re
import socket
import subprocess
import sys
import time


MAILPILE_STOP_SCRIPT = [
    # Ask it to shut down nicely, remove pid-file if not running.
    'kill "%(pid)s" || (rm -f "%(pidfile)s"; false)',
    'sleep 10',
    # Remove pid-file iff shutdown succeeded.
    'kill "%(pid)s" || (rm -f "%(pidfile)s"; true)']

MAILPILE_FORCE_STOP_SCRIPT = [
    # If still running, wait 20 more seconds and then force things.
    'kill -INT "%(pid)s" && (sleep 20; kill -9 "%(pid)s") || true',
    # Clean up!
    'rm -f "%(pidfile)s"']

MAILPILE_START_SCRIPT = [
    # We start Mailpile in a screen session named "mailpile"
    'screen -S mailpile -d -m "%(mailpile)s"'
        ' "--www=%(host)s:%(port)s%(path)s"'
        ' "--pid=%(pidfile)s"'
        ' --interact']

INSTALL_APACHE_SCRIPT = [
    '"%(packager)s" install screen expect',
    'a2enmod headers rewrite proxy proxy_http',
    'mkdir -p /var/lib/mailpile/apache/ /var/lib/mailpile/pids/',
    'cp -a "%(mailpile-www)s"/* /var/lib/mailpile/apache/',
    'rm -f /var/lib/mailpile/apache/shared',
    'ln -fs "%(mailpile-static)s" /var/lib/mailpile/apache/shared',
    'ln -fs "%(mailpile-admin)s" /var/lib/mailpile/apache/admin.cgi',
    'ln -fs "%(mailpile-conf)s" /etc/apache2/conf-enabled/',
    'apache2ctl restart']

FIX_PERMS_SCRIPT = [
    'chown -R %(apache-user)s:%(apache-group)s /var/lib/mailpile/apache/',
    'chmod go+rwxt /var/lib/mailpile/pids',]

RUN_AS_EXPECT_SCRIPT = """\
    spawn su -l %(user)s
    expect assword {
        send "%(password)s\\n"
    }
    expect {
        incorrect   exit
        failure     exit
        timeout     exit
        "\\\\$"
    }
    send "exec %(command)s\\n"
    expect heat_death_of_universe
"""

MAILPILE_PIDS_PATH = "/var/lib/mailpile/pids"
APACHE_DEFAULT_WEBROOT = "/mailpile/"
APACHE_HTACCESS_PATH = "/var/lib/mailpile/apache/.htaccess"

APACHE_REWRITE_TEMPLATE = """\
RewriteRule ^(%(user)s/.*)$  http://%(host)s:%(port)s/mailpile/$1  [P,L,QSA]  # MP\
"""
APACHE_HTACCESS_TEMPLATE = """\
# Note: Autogenerated by mailpile-admin.py, edit at your own risk!

# These are our configured Mailpiles:
%(rewriterules)s

# Redirect any proxy errors or 404 errors to mailpile-admin.py:
ErrorDocument 503 %(webroot)snot-running.html
ErrorDocument 404 %(webroot)snot-running.html
RewriteRule ^not-running.html %(webroot)s [L,R=302,E=nolcache:1]
Header always set Cache-Control "no-store, no-cache, must-revalidate" env=nocache
Header always set Expires "Thu, 01 Jan 1970 00:00:00 GMT" env=nocache
"""


def _escape(string):
    return json.dumps(unicode(string).encode('utf-8'))[1:-1]


def _escaped(idict):
    return dict((k, _escape(v)) for k, v in idict.iteritems())


def app_arguments():
    ap = argparse.ArgumentParser(
        description="Mailpile installation and integration tool")

    ga = ap.add_mutually_exclusive_group(required=True)
    ga.add_argument(
        '--list', action='store_true',
        help='List running Mailpiles')
    ga.add_argument(
        '--start', action='store_true',
        help='Launch Mailpile in a screen session')
    ga.add_argument(
        '--stop', action='store_true',
        help='Stop a running Mailpile')
    ga.add_argument(
        '--install-apache', action='store_true',
        help='Configure Apache for use with Mailpile (run with sudo)')

    ap.add_argument('--force', action='store_true',
        help='With --stop, will kill -9 a running Mailpile')
    ap.add_argument('--password', default=None,
        help='For testing (with --user), do not use!')
    ap.add_argument('--user', default=None,
        help='Choose user, for use with --stop and --start')
    ap.add_argument('--port', default=None,
        help='Choose port, for use with --stop and --start')
    ap.add_argument('--host', default='localhost',
        help='Choose host, for use with --stop and --start')
    ap.add_argument('--discover', action='store_true',
        help='Discover running Mailpiles during --install-apache')
    ap.add_argument('--webroot', default=APACHE_DEFAULT_WEBROOT,
        help='Parent web directory for Mailpile instances')
    ap.add_argument('--mailpile-root', default=None,
        help='Location of Mailpile itself')
    ap.add_argument('--packager', default=None,
        help='Packaging tool (apt-get) for use during install')
    ap.add_argument('--apache-user', default=None)
    ap.add_argument('--apache-group', default=None)
    return ap


def _parse_ps():
    ps = subprocess.Popen(['ps', 'auxw'], stdout=subprocess.PIPE)
    ps_re = re.compile('^(\S+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(\S+)'
                       '.*\s(python2 .*/mp|mailpile)(?:\s+|$)')
    for line in ps.communicate()[0].splitlines():
        m = re.match(ps_re, line)
        if m:
            yield (m.group(1), m.group(2), m.group(3), m.group(4))


def _parse_netstat():
    ns = subprocess.Popen(['netstat', '-ant', '--program'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ns_re = re.compile('^tcp\s+\S+\s+\S+\s+(\S+:\d+)\s+(\S+:.)'
                       '\s+.*?\s(\d+)\/(\S+)\s*$')
    for line in ns.communicate()[0].splitlines():
        m = re.match(ns_re, line)
        if m:
            lhp, rhp, pid, proc = m.group(1), m.group(2), m.group(3), m.group(4)
            yield lhp, rhp, pid, proc


def _get_random_port():
    ns = _parse_netstat()
    for tries in range(0, 100):
       port = '%s' % random.randint(34110, 64110)
       cport = ':' + port
       for lhp, rhp, pid, proc in ns:
           if lhp.endswith(cport):
               port = None
               break
       if port:
           return port
    assert(not 'All the ports appear taken!')


def get_os_settings(args):
    # FIXME: Detect OS, choose settings; these are just the Ubuntu defaults.

    mp_root = os.path.join(args.mailpile_root or
                           os.path.join(os.path.dirname(__file__), '..'))
    mp_root = os.path.realpath(mp_root)
    mpa_root = os.path.join(mp_root, 'packages', 'apache')
    return {
        'packager': args.packager or 'apt-get',
        'apache-user': args.apache_user or 'www-data',
        'apache-group': args.apache_group or 'www-data',
        'webroot': args.webroot,
        'mailpile': os.path.join(mp_root, 'mp'),
        'mailpile-root': mp_root,
        'mailpile-admin': os.path.realpath(sys.argv[0]),
        'mailpile-static': os.path.join(mp_root, 'mailpile', 'www', 'default'),
        'mailpile-www': os.path.join(mpa_root, 'www'),
        'mailpile-conf': os.path.join(mpa_root, 'mailpile.conf')}


def get_user_settings(args, user=None, mailpiles=None):
    settings = get_os_settings(args)
    user = user or pwd.getpwuid(os.getuid()).pw_name
    assert('.' not in user and '/' not in user)
    pidfile = os.path.join(MAILPILE_PIDS_PATH, user + '.pid')

    port = args.port
    if mailpiles and not port:
        ports = [int(m[2]) for m in mailpiles.values() if m[0] == user]
        if ports:
            port = '%s' % min(ports)
    if not port:
        port = _get_random_port()

    return {
        'user': user,
        'mailpile': settings['mailpile'],
        'host': '127.0.0.1',
        'port': port,
        'path': ('%s/%s/' % (args.webroot, user)).replace('//', '/'),
        'pidfile': pidfile,
        'pid': os.path.exists(pidfile) and open(pidfile, 'r').read().strip()}


def discover_mailpiles(mailpiles=None):
    mailpiles = mailpiles if (mailpiles is not None) else {}

    # Check the process table for running Mailpiles
    processes = {}
    for username, pid, rss, proc in _parse_ps():
        processes[pid] = [username, proc, rss]

    # Add the listening host:port details from netstat
    for listening_hostport, rhp, pid, proc in _parse_netstat():
        if pid in processes:
            processes[pid].append(listening_hostport)

    for pid, details in processes.iteritems():
        username, proc, rss, listening = (details[0], details[1],
                                          details[2], details[3:])
        if listening:
            hostport = sorted(listening)[0]
            host, port = hostport.split(':')
            if hostport not in mailpiles:
                mailpiles[hostport] = (username, host, port, False, pid, rss)
            else:
                mailpiles[hostport][4] = pid
                mailpiles[hostport][5] = rss

    return mailpiles


def parse_htaccess(args, os_settings, mailpiles=None):
    mailpiles = mailpiles if (mailpiles is not None) else {}
    try:
        # RewriteRule ^(%(user)s/.*)$  http://%(host)s:%(port)s/...  # MP
        parse = re.compile('^RewriteRule\s+[^a-z]+([a-z0-9]+)'
                           '.*?\/\/([^:]+):(\d+)\/.*# MP\s*$')
        with open(APACHE_HTACCESS_PATH, 'r') as fd:
            for line in fd:
                m = re.match(parse, line)
                if m:
                    host, port = m.group(2), m.group(3)
                    mailpiles['%s:%s' % (host, port)] = [
                        m.group(1), host, port, True, None, None]
    except (OSError, IOError, KeyError), err:
        print 'WARNING: %s' % err
    return mailpiles


def save_htaccess(args, os_settings, mailpiles):
    rules = []
    added = {}
    for hostport, details in mailpiles.iteritems():
        user, host, port = details[0:3]
        if user not in added:
            rules.append(APACHE_REWRITE_TEMPLATE % {
                'user': _escape(user), 'host': host, 'port': port})
            added[user] = True
        else:
            print ('WARNING: User %s has multiple Mailpiles! Skipped %s'
                   ) % (user, hostport)

    with open(APACHE_HTACCESS_PATH + '.new', 'w') as fd:
        os_settings['rewriterules'] = '\n'.join(rules)
        fd.write(APACHE_HTACCESS_TEMPLATE % os_settings)
    os.remove(APACHE_HTACCESS_PATH)
    os.rename(APACHE_HTACCESS_PATH + '.new', APACHE_HTACCESS_PATH)


def run_script(args, settings, script):
    for line in script:
        line = line % _escaped(settings)
        print '==> %s' % line
        rv = os.system(line)
        if 0 != rv:
            print '==[ FAILED! Exit code: %s ]==' % rv
            return
    print '===[ SUCCESS! ]==='


def _get_mailpiles(args):
    os_settings = get_os_settings(args)
    mailpiles = {}
    parse_htaccess(args, os_settings, mailpiles=mailpiles)
    if args.discover:
        discover_mailpiles(mailpiles=mailpiles)
    return mailpiles


def list_mailpiles(args):
    os_settings = get_os_settings(args)
    mailpiles = parse_htaccess(args, os_settings)
    discover_mailpiles(mailpiles=mailpiles)
    fmt =  '%-8.8s %6.6s %6.6s %-6.6s %5.5s %s'
    user_counts = {}
    print fmt % ('USER', 'PID', 'RSS', 'ACCESS', 'PORT', 'URL')
    for hostport in sorted(mailpiles.keys()):
        user, host, port, in_htaccess, pid, rss = mailpiles[hostport]
        user_counts[user] = user_counts.get(user, 0) + 1
        status = []
        if in_htaccess:
            url = 'http://%s%s%s/' % (socket.gethostname(),
                                      os_settings['webroot'], user)
        else:
            url = 'http://%s:%s/' % (host, port)
        print fmt % (
            user, pid or '', rss or '',
            'apache' if in_htaccess else 'direct', port, url)


def install_apache(app_args, args):
    if os.getuid() == 0:
        os_settings = get_os_settings(args)
        run_script(args, os_settings, INSTALL_APACHE_SCRIPT)
        save_htaccess(args, os_settings, _get_mailpiles(args))
        run_script(args, os_settings, FIX_PERMS_SCRIPT)
    else:
        usage(app_args, 'Please run this as root!')


def run_as_user(user, password, command):
    script = subprocess.Popen(['expect', '-'],
                              stdin=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              stdout=subprocess.PIPE)
    expects = RUN_AS_EXPECT_SCRIPT % _escaped({
        'user': user, 'password': password, 'command': command
    })
    return script.communicate(input=expects)


def start_mailpile(app_args, args):
    os_settings = get_os_settings(args)
    mailpiles = parse_htaccess(args, os_settings)
    user_settings = get_user_settings(args, user=args.user, mailpiles=mailpiles)
    assert(re.match('^[0-9]+$', user_settings['port']) is not None)
    assert(re.match('^[a-z0-9\.]+$', user_settings['host']) is not None)
    if args.user:
        command = '"%s" --start --port="%s" --host="%s"' % (
            _escape(os_settings['mailpile-admin']),
            _escape(user_settings['port']),
            _escape(user_settings['host']))
        if args.password:
            print '%s%s' % run_as_user(args.user, args.password, command)
            script = None
        else:
            script = ['sudo -u "%(user)s" -- ' + command]
    else:
        script = MAILPILE_START_SCRIPT

    if script:
        run_script(args, user_settings, script)

    if args.user:
        hostport = '%s:%s' % (user_settings['host'], user_settings['port'])
        mailpiles[hostport] = (user_settings['user'],
                               user_settings['host'],
                               user_settings['port'],
                               False, None, None)
        save_htaccess(args, os_settings, mailpiles)
        run_script(args, os_settings, FIX_PERMS_SCRIPT)


def stop_mailpile(app_args, args):
    user_settings = get_user_settings(args, user=args.user)
    script = []
    if args.user:
        command = '"%s" --stop' % (
            _escape(os.path.realpath(sys.argv[0])))
        if args.password:
            print '%s%s' % run_as_user(args.user, args.password, command)
            return
        script = ['sudo -u "%(user)s" -- ' + command]
    else:
        script += MAILPILE_STOP_SCRIPT
        if args.force:
            script += MAILPILE_FORCE_STOP_SCRIPT

    if user_settings.get('pid'):
        run_script(args, user_settings, script)
    else:
        usage(app_args, 'No PID found, cannot stop Mailpile', code=0)


def usage(ap, reason, code=3):
    print 'error: %s' % reason
    ap.print_usage()
    sys.exit(code)


def main():
    app_args = app_arguments()
    parsed_args = app_args.parse_args()
    if parsed_args.list:
        list_mailpiles(parsed_args)

    elif parsed_args.install_apache:
        install_apache(app_args, parsed_args)

    elif parsed_args.start:
        start_mailpile(app_args, parsed_args)

    elif parsed_args.stop:
        stop_mailpile(app_args, parsed_args)


def handle_cgi_post():
    app_args = app_arguments()
    try:
        request = cgi.FieldStorage()
        username = request.getfirst('username')
        password = request.getfirst('password')

        # Sanity checks; these will raise on invalid/missing username
        assert(username and password)
        pwd.getpwnam(username)

        # Send headers now, so output doesn't confuse Apache
        print 'Location: /mailpile/%s/' % username
        print 'Expires: 0'
        print

        # Launch Mailpile?
        rv = start_mailpile(app_args, app_args.parse_args([
            '--start', '--user', username, '--password', password]))

        time.sleep(5)
    except:
        print 'Location: /mailpile/?error=yes'
        print 'Expires: 0'
        print


if __name__ == "__main__":
    if os.getenv('REQUEST_METHOD') == 'POST':
        assert(len(sys.argv) == 1)
        handle_cgi_post()
    else:
        main()
