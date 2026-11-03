# Selected client reporting

For the native router installation, see [OpenWrt telemetry](OPENWRT_TELEMETRY.md).
That deployment reads a fresh local verified snapshot and keeps status files in
RAM; the systemd/SSH deployment below is an optional alternative.

This fork retains the upstream gateway emulator and adds `client_reporter.py`,
a separate reporting-only virtual switch for wired clients that existing UniFi
hardware fails to report. It is unofficial and needs compatibility validation
after UniFi updates. The `US8` identity and firmware version are protocol
compatibility metadata; there is no physical switch or vendor firmware here.

Each configured target must answer an ICMP ping and have its expected MAC in the
local neighbor table on the configured interface. DHCP leases and historical
neighbors alone never make a target online. Only explicit target MACs are included.
Use this for selected wired clients on the same subnet, not wireless clients
already managed by UniFi APs. Up to seven targets fit the virtual switch's ports.
The upstream gateway collector also now uses DHCP leases only to name observed
hosts; an unobserved lease no longer becomes an online host.

## Installation

Install this checkout in `/opt/unifi-client-reporter`, create a Python virtual
environment there, and install `requirements.txt`. Create a system account named
`unifi-client-reporter` without a login shell. Copy
`conf/client-reporter.sample.json` to `/etc/unifi-client-reporter.json` and set the
controller URL, interface, a dedicated locally administered virtual-device MAC,
and target IP/MAC pairs. Keep real deployment files and adoption keys outside the
public checkout. The management IP is display metadata only; it is never assigned
to an interface. A documentation address such as `192.0.2.1` avoids claiming a
real client's address.

Install `unifi-client-reporter.service`, reload systemd, and start it. Adopt only
the newly appearing virtual switch in UniFi. Adoption state is stored in
`/var/lib/unifi-client-reporter/adoption.json`; `status.json` and the journal show
the last successful report. Enable the service after verifying live client status.

Unprivileged ICMP sockets must be allowed by `net.ipv4.ping_group_range`. The
service has an empty capability set and cannot use privileged raw sockets.

## Failure boundary

* No router, controller administration, SSH or cloud API credentials are installed
  in the service. Its adoption key belongs only to its own virtual device.
* No listening sockets or discovery broadcasts are used.
* No routes, DHCP settings, firmware, shell commands, resets or provisioning are
  applied. Gateway mode remains a separate entry point and is not started here.
* Only adoption-key, encryption-mode and configuration-version metadata are
  accepted from `setparam`. `system_cfg` is never applied. Redirects are rejected.
* Unknown response types, executable commands, reboot/upgrade/reset requests or
  undecodable responses stop the reporter with exit status 78. Systemd does not
  restart that status. Connection failures retry every 30 seconds.
* Failed probes are omitted. Cached live-client lists are never replayed after
  observation errors. Only fixed-argument `ping` and read-only `ip neigh` run.
* If reporting stops, UniFi eventually ages out the virtual switch and its selected
  clients. Routing, DHCP, firewall, physical switches and native client reports
  do not depend on this service.
* The switch placement is synthetic. No traffic counters or link speeds are
  fabricated. A live client means it answered with its expected MAC, not that its
  application is healthy.

The unit runs unprivileged with read-only system paths, a private temporary
directory, no device access, restricted address families and CPU/memory limits.
Only its own state directory is writable. These boundaries protect network
configuration; vendor UI behavior and reporting compatibility cannot be guaranteed.

## Disable

```
systemctl disable --now unifi-client-reporter.service
```

The router and real devices require no rollback. Forget the virtual switch in
UniFi after stopping it if it is no longer needed. Preserve state to restart it.
