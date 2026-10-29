#!/usr/bin/env python3
"""Read-only Juniper interface bandwidth collector; Python standard library + Net-SNMP."""
import argparse
from datetime import datetime
import getpass
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

BASE = '.1.3.6.1.2.1.31.1.1.1.'
UPTIME = '.1.3.6.1.2.1.1.3.0'
CREDENTIALS = Path.home() / '.snmp/snmp.conf'


def setup_community():
    if not sys.stdin.isatty():
        raise ValueError('Run --setup-community in your own interactive terminal.')
    secret = getpass.getpass('SNMPv2c community (hidden): ')
    # Restrict to an unambiguous Net-SNMP token; never echo invalid input.
    if not secret or any(c.isspace() or c in '\\"\'#' or not c.isprintable() for c in secret):
        raise ValueError('Community must be a nonempty printable token without whitespace, quotes, # or backslash.')
    CREDENTIALS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CREDENTIALS.is_symlink():
        raise ValueError('Refusing a symlink credential file.')
    old = CREDENTIALS.read_text() if CREDENTIALS.exists() else ''
    lines = [x for x in old.splitlines() if not x.strip().lower().startswith(('defcommunity ', 'defversion '))]
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write('\n'.join(lines) + '\ndefVersion 2c\ndefCommunity ' + secret + '\n')
    print('Saved community in ~/.snmp/snmp.conf with permissions 0600.')


def check_credentials():
    s = CREDENTIALS.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError('~/.snmp/snmp.conf must be owned by you, regular, and mode 0600.')
    if not re.search(r'^\s*defCommunity\s+\S+', CREDENTIALS.read_text(), re.M | re.I):
        raise ValueError('No v2c community configured. Run --setup-community locally.')


def query(host, oids):
    env = os.environ.copy()
    env['MIBS'] = ''
    # No secret is supplied in argv or environment. Net-SNMP reads its local config.
    try:
        p = subprocess.run(['snmpget', '-v', '2c', '-r', '0', '-t', '2', '-m', '',
                            '-On', '-Ot', host, *oids], capture_output=True, text=True,
                           timeout=5, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError('SNMP request timed out') from None
    if p.returncode:
        # Suppress raw library diagnostics, which may contain config contents.
        raise RuntimeError('SNMP request failed (VPN, community, ACL, or timeout)')
    rows = p.stdout.splitlines()
    if len(rows) != len(oids):
        raise RuntimeError('Incomplete SNMP response')
    values = []
    for oid, row in zip(oids, rows):
        match = re.fullmatch(re.escape(oid) + r' = (\w+): (.*)', row)
        if not match:
            raise RuntimeError('Unsupported or missing SNMP object')
        values.append((match[1], match[2]))
    return values


def number(value, expected):
    kind, text = value
    if kind != expected or not text.isdigit():
        raise RuntimeError('Unexpected SNMP counter type/value')
    n = int(text)
    if n < 0 or (expected == 'Counter64' and n >= 2**64):
        raise RuntimeError('Invalid counter range')
    return n


def snapshot(host, interfaces):
    oids = [UPTIME]
    for item in interfaces:
        idx = str(item['ifindex'])
        oids.extend(BASE + column + '.' + idx for column in ('1', '6', '10', '19'))
    start = time.monotonic()
    values = query(host, oids)
    sampled = (start + time.monotonic()) / 2
    uptime = number(values[0], 'Timeticks')
    counters = []
    for i, item in enumerate(interfaces):
        name, incoming, outgoing, discontinuity = values[1 + i*4:5 + i*4]
        if name[0] != 'STRING' or name[1].strip('"') != item['name']:
            raise RuntimeError('ifIndex/name mapping changed or does not match configuration')
        counters.append((number(incoming, 'Counter64'), number(outgoing, 'Counter64'),
                         number(discontinuity, 'Timeticks')))
    return sampled, uptime, counters


def rates(old, new):
    elapsed = new[0] - old[0]
    if elapsed <= 0 or new[1] < old[1]:
        return None
    result = []
    for before, after in zip(old[2], new[2]):
        if before[2] != after[2] or after[0] < before[0] or after[1] < before[1]:
            return None
        result.append(tuple((after[i] - before[i]) * 8 / elapsed for i in (0, 1)))
    return result


def selection(config, requested):
    entries = config['interfaces']
    names = requested.split(',') if requested else config['selected']
    if not names or len(names) != len(set(names)):
        raise ValueError('Select one or more distinct interface names.')
    selected = [next((x for x in entries if x['name'] == n), None) for n in names]
    if any(x is None for x in selected):
        raise ValueError('Selected interface is absent from configuration inventory.')
    if any(type(x['ifindex']) is not int or x['ifindex'] <= 0 for x in selected):
        raise ValueError('ifIndexes must be positive integers.')
    if len({x['ifindex'] for x in selected}) != len(selected):
        raise ValueError('Duplicate ifIndexes would double count traffic.')
    if len(names) > 1:
        if not config.get('boundary_reviewed') or not config.get('boundary_label', '').strip():
            raise ValueError('Multiple interfaces require a reviewed boundary and boundary_label in config; see README.')
        for a in names:
            for b in names:
                if a != b and a.split('.')[0] == b.split('.')[0]:
                    raise ValueError('Do not combine a parent and its units, or units on one parent in this basic collector.')
        for group in config.get('overlap_groups', []):
            if len(set(names) & set(group)) > 1:
                raise ValueError('Selected interfaces share an overlap group.')
    return selected


def inspect(config):
    # SSH owns the terminal and password prompt; no password enters Python.
    if not sys.stdin.isatty():
        raise ValueError('Run --inspect-ssh in your own interactive terminal for secure SSH password entry.')
    command = ('show interfaces descriptions | no-more; '
               'show interfaces extensive | no-more; '
               'show lacp interfaces extensive | no-more')
    result = subprocess.run(['ssh', '-t', '-o', 'StrictHostKeyChecking=yes',
                             '-o', 'PubkeyAuthentication=no', '-o', 'ConnectTimeout=10',
                             '-p', '22', 'arnab@' + config['host'], command])
    if result.returncode:
        raise RuntimeError('SSH inspection failed; verify VPN, credentials and trusted host key.')


def fmt(bps):
    return f'{bps / 1e9:.3f} Gbps' if bps >= 1e9 else f'{bps / 1e6:.3f} Mbps'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--interfaces', help='Comma-separated configured names')
    parser.add_argument('--samples', type=int, help='Stop after N poll attempts (minimum 2); useful for a live test')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--setup-community', action='store_true')
    group.add_argument('--inspect-ssh', action='store_true')
    args = parser.parse_args()
    if args.setup_community:
        setup_community()
        return
    config = json.loads(Path(args.config).read_text())
    if args.inspect_ssh:
        inspect(config)
        return
    interfaces = selection(config, args.interfaces)
    interval = float(config.get('interval_seconds', 10))
    if not math.isfinite(interval) or interval <= 0 or (args.samples is not None and args.samples < 2):
        raise ValueError('Interval must be positive and samples at least 2.')
    check_credentials()
    included = ', '.join(x['name'] for x in interfaces)
    print(f'Router {config["host"]}; IN/OUT relative to router interfaces. Total includes [{included}].', flush=True)
    print('Boundary: ' + (config.get('boundary_label') or included), flush=True)
    old = None
    attempt = 0
    deadline = time.monotonic()
    while args.samples is None or attempt < args.samples:
        time.sleep(max(0, deadline - time.monotonic()))
        attempt += 1
        stamp = datetime.now().astimezone().isoformat(timespec='seconds')
        try:
            new = snapshot(config['host'], interfaces)
            computed = rates(old, new) if old else None
            if computed is None:
                print(f'{stamp} [{included}] baseline/reset; total unavailable', flush=True)
            else:
                for item, (incoming, outgoing) in zip(interfaces, computed):
                    print(f'{stamp} {item["name"]} IN {fmt(incoming)} OUT {fmt(outgoing)}', flush=True)
                print(f'{stamp} TOTAL [{included}] IN {fmt(sum(x[0] for x in computed))} OUT {fmt(sum(x[1] for x in computed))}', flush=True)
            old = new
        except RuntimeError as exc:
            old = None
            print(f'{stamp} {exc}; total unavailable; next success establishes baseline', flush=True)
        deadline += interval
        if deadline < time.monotonic():
            deadline = time.monotonic() + interval


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        # Do not echo file/config parsing errors that could expose credential text.
        if isinstance(exc, OSError):
            print('Error: required file/program unavailable or permission denied.', file=sys.stderr)
        else:
            print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
