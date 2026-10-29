# ISP bandwidth monitor

A Python command-line tool that measures interface bandwidth on a Juniper MX204 using read-only SNMPv2c queries. It prints incoming and outgoing traffic every 10 seconds. No Python packages, database, or dashboard are required, and the tool does not change router configuration.

The current configuration monitors **ae11 only** on `172.16.212.251`, using SNMP ifIndex **532**. The saved SSH inventory identifies ae11 as a 70 Gbps upstream aggregate with description `IIG-RT-UPSTREAM-QFX-ae1-70G`. This is an upstream-interface measurement, not a router-wide traffic total.

## Requirements

- Ubuntu/Linux with Python 3 and Net-SNMP (`snmpget`).
- Connection to the office network or VPN with access to the router.
- A read-only SNMPv2c community supplied securely by the router administrator.
- Router and network rules allowing SNMP requests on UDP port 161 from your VPN source IP, with a working return path.
- OpenSSH client only if you want to inspect interface mappings over SSH.

## Quick start

### 1. Get the code and install dependencies

Clone the repository using the URL supplied with this project, or extract the project archive. Open a terminal in the directory containing `collector.py` and `config.json`, then run:

```bash
sudo apt update
sudo apt install python3 snmp openssh-client
python3 collector.py --help
```

There is no `pip install` step.

### 2. Connect to the VPN

Check the route to the router:

```bash
ip route get 172.16.212.251
```

The value after `src` is your local source IP for this destination. Give this IP to the administrator when requesting SNMP access. VPN addresses and interface names can change after reconnecting; NAT may also change the address the router sees.

### 3. Configure the SNMP community

Run this in your own interactive terminal:

```bash
python3 collector.py --setup-community
```

Enter the exact community supplied by your administrator at the hidden prompt. It is saved outside the repository in `~/.snmp/snmp.conf`, owned by your user with permissions `0600`. Each person running the tool must configure their own credentials.

The collector reads Net-SNMP's local `defCommunity` setting. The community is not stored in `config.json` or passed in command arguments. Do not commit credentials or paste them into chat, screenshots, or issue reports. SNMPv2c does not encrypt traffic; use the approved office network or VPN.

The setup accepts a nonempty printable token without whitespace, quotes, `#`, or backslash. If the supplied community contains these characters, this setup helper cannot store it; consult the administrator rather than altering the value.

### 4. Run a short live test

```bash
python3 collector.py --samples 3
```

At the default interval, this performs three polling attempts over approximately 20 seconds, plus request time. The first successful reading establishes a baseline; the next two successful readings should print rates.

Illustrative output only (these are not measured results):

```text
Router 172.16.212.251; IN/OUT relative to router interfaces. Total includes [ae11].
Boundary: ae11 upstream QFX aggregate only; verified SSH ifIndex 532, seven 10G members
2026-10-29T16:00:00+06:00 [ae11] baseline/reset; total unavailable
2026-10-29T16:00:10+06:00 ae11 IN 12.345 Gbps OUT 3.210 Gbps
2026-10-29T16:00:10+06:00 TOTAL [ae11] IN 12.345 Gbps OUT 3.210 Gbps
```

A successful test must include actual rate lines and `TOTAL [ae11]`. Timeouts or `total unavailable` are not zero traffic. A polling failure is printed but does not necessarily produce a nonzero process exit status, so check the output rather than the exit status alone.

### 5. Run continuously

```bash
python3 collector.py
```

Press **Ctrl+C** to stop. To also save the terminal output:

```bash
python3 -u collector.py | tee bandwidth.log
```

## Repository files

| File | Purpose |
| --- | --- |
| `collector.py` | Credential setup, optional SSH inspection, SNMP polling, and rate calculation |
| `config.json` | Router address, polling interval, interface inventory, and selected measurement boundary |
| `.gitignore` | Excludes local credentials, SSH inspection captures, and Python cache files |
| `README.md` | Installation, operation, configuration, and troubleshooting |

## Configuration

Edit `config.json` to change these settings:

| Setting | Meaning |
| --- | --- |
| `host` | Router IP address; currently `172.16.212.251` |
| `interval_seconds` | Positive polling interval; currently `10` seconds |
| `interfaces` | Verified interface names and SNMP ifIndexes |
| `selected` | Interfaces to measure; currently `["ae11"]` |
| `boundary_label` | Description of the traffic boundary being measured |
| `boundary_reviewed` | Must be `true` before selecting multiple interfaces, after reviewing the boundary |
| `overlap_groups` | Interfaces that must not be selected together because they carry overlapping traffic |

Use an alternative configuration or select a configured interface:

```bash
python3 collector.py --config /path/to/config.json --samples 3
python3 collector.py --interfaces ae11 --samples 3
```

Verify mappings on the target router before reusing this configuration elsewhere. Every poll checks that ifIndex 532 still reports `ae11`; a mismatch prevents rate calculation.

### Avoid double counting

Do not sum an aggregate and its physical members, or a parent interface and its logical units. Upstream and customer-facing ports may also carry the same forwarded packets.

The saved inventory lists ae11's seven active 10G members as `xe-0/1/0`, `xe-0/1/1`, `xe-0/1/2`, `xe-0/1/4`, `xe-0/1/5`, `xe-0/1/6`, and `xe-0/1/7`. Do not combine these members, or ae11 logical units, with ae11 in a total.

Before measuring multiple interfaces, verify their forwarding roles and inventory, update `overlap_groups`, set a specific `boundary_label`, and set `boundary_reviewed` to `true`. The tool rejects duplicate selections/indexes, common-parent selections, and configured overlap groups, but cannot determine whether your network boundary or overlap inventory is complete.

## Optional: verify interfaces over SSH

For an ordinary SSH login, use your own authorized username:

```bash
ssh YOUR_USERNAME@172.16.212.251
```

Run these read-only Junos commands:

```text
show interfaces descriptions | no-more
show interfaces extensive | no-more
show lacp interfaces extensive | no-more
```

Confirm ae11's physical-interface SNMP ifIndex, speed, and LACP membership. Logical units have separate ifIndexes.

The included helper runs those commands, but currently uses the fixed username **arnab**, password-based authentication, and port **22**:

```bash
python3 collector.py --inspect-ssh | tee inspection.txt
```

Use the manual SSH method if your username differs. The helper requires an interactive terminal and an existing trusted host key. Verify any new or changed host-key fingerprint with the administrator before trusting it. `inspection*.txt` is ignored by Git; review captures for private operational details before sharing.

## Troubleshooting SNMP failures

If the output says `SNMP request failed (VPN, community, ACL, or timeout)`, check the following:

1. Confirm the VPN is connected and inspect `ip route get 172.16.212.251`.
2. Confirm the community with the administrator through a secure channel, then rerun `python3 collector.py --setup-community`.
3. Ask the administrator to check SNMPv2c, community client restrictions, UDP 161 filters, and the return route for your current source IP.
4. Check local firewall rules. On Ubuntu, `sudo ufw status verbose` checks UFW; an inactive UFW does not rule out other firewall or VPN filtering.

An independent query using the locally configured community is:

```bash
snmpget -v2c -On -t 3 -r 1 172.16.212.251 \
  .1.3.6.1.2.1.1.3.0 \
  .1.3.6.1.2.1.31.1.1.1.1.532 \
  .1.3.6.1.2.1.31.1.1.1.6.532 \
  .1.3.6.1.2.1.31.1.1.1.10.532
```

This requests uptime, interface name (expected `ae11`), and 64-bit received/transmitted byte counters. A timeout does not establish whether the community is correct or where traffic is blocked. SSH connectivity proves TCP port 22 is reachable, not that SNMP on UDP 161 is allowed.

### Current live validation status — 29 October 2026

The saved SSH inspection confirms ae11/532. Local checks found Net-SNMP installed, a configured community with appropriate credential-file permissions, a VPN route, and reachable SSH. UFW was inactive. The user reported entering the community displayed on the router, but all three subsequent SNMP polling attempts failed; **live bandwidth rates have not yet been validated**.

The observed VPN source IP was `192.168.115.196`; check it again before requesting access because it may change. Router SNMP access restrictions and UDP 161 connectivity still need investigation with the administrator.

## How measurements work

Rates are calculated as `(new byte counter - old byte counter) × 8 / elapsed seconds`, using monotonic request-midpoint timestamps. Output uses decimal Mbps and Gbps; IN and OUT are relative to the router interface. Interface counters measure counted traffic and overhead, not application goodput. IN and OUT are displayed separately.

Each poll fetches sysUpTime, ifName, ifHCInOctets, ifHCOutOctets, and ifCounterDiscontinuityTime in one request. The collector requires Counter64 traffic counters and does not fall back to 32-bit values. A failed poll clears the baseline. Reboots, decreasing counters, changed discontinuity markers, or uptime wrap establish a new baseline and suppress rates for that interval. Recovery requires two consecutive successful samples.

Polling is approximate interval measurement, not packet capture. Counter resets that the router does not signal and whose counters overtake the previous value between polls cannot reliably be detected.
