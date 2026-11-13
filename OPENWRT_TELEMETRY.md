# Read-only OpenWrt telemetry

The native OpenWrt deployment runs three independent procd instances under
`unifi-telemetry`: a local collector, a virtual gateway publisher, and the
selected-client virtual switch. UniFi Network itself stays on its existing host.
The original upstream gateway entry point and other features remain available.

## Data published

* DHCP MAC/IP/name mappings from current leases.
* Hosts across configured local IPv4 networks that match the neighbor-table MAC
  and either answer ping or have a freshly REACHABLE ARP entry. Stale entries
  alone do not establish presence.
* Per-host Internet traffic bytes and packets from the existing `nlbwmon` database.
* Optional forwarded local IPv4 traffic counters, including traffic between VLANs.
* WAN/LAN interface totals and rates from kernel counters.
* Router CPU usage, memory use and actual router uptime.
* A real Internet ICMP check and latency to `1.1.1.1` in the local status file.

The gateway publishes the IP mappings while native UniFi switches and APs retain
their client attachment information. The virtual switch still reports only its
explicit target list. It gives clients missed by the real switches an independent
status source; it does not become the parent of every client.

UniFi requires the legacy gateway host-report capability and host observation
uptime to merge this data. `UGW3` and the reported firmware version are protocol
compatibility metadata. The device should be named clearly, for example
`OpenWrt (telemetry only)`, with auto-upgrade disabled for this emulated device.

Traffic statistics cover connections accounted for by OpenWrt. Traffic staying on
a switch or inside a hypervisor bridge is outside that path. UniFi accumulates
new observations; existing historical monthly data is not backfilled into UniFi's
history. The full accounting-period data remains available in OpenWrt/LuCI.
Counter decreases at an accounting reset are handled without negative rates or
unsigned-counter spikes. UniFi's client transmit fields represent download;
nlbwmon uses the client's receive fields for download. Byte and packet directions
are translated accordingly. UniFi derives client rates from successive totals.
Internet and local counters are accumulated independently so a reset of one
source does not replay the other. A RAM checkpoint in the publisher status
directory preserves continuity across telemetry restarts without flash writes.
New sources establish a baseline; their existing totals are not replayed.
No application-level DPI or physical switch-port
statistics are invented. Internet health in the local status file must not be
confused with a complete native UniFi gateway health history.

## Native deployment

Use OpenWrt's official `python3`, `python3-psutil`, and `python3-cryptography`
packages. The Inform codec supports both upstream PyCryptodome and the official
OpenWrt cryptography backend, with cross-backend CBC/GCM tests.

1. Install `client_reporter.py`, `router_telemetry.py`, `openwrt_snapshot.py`,
   `unifi_protocol.py`, `tools.py` and `tlv.py` under `/usr/lib/unifi-reporter/`,
   owned by root and writable only by root.
2. Create a locked account/group named `unifi-report`. Install the collector
   configuration from `conf/openwrt-snapshot.sample.json` at
   `/etc/unifi-telemetry-snapshot.json`. Match case-sensitive interface names.
   Set `lan_interfaces` to all local logical networks and `wan_interfaces` to the
   primary and fallback names in priority order. Each LAN gets a separate
   `network_table`/`host_table`; VLAN suffixes are preserved on the emulated LAN.
3. Install private deployment configurations at `/etc/unifi-reporters/clients.json`
   and `/etc/unifi-reporters/gateway.json`, based on the sample files. Both use
   `/tmp/unifi-telemetry/snapshot.json`; use the real LAN MAC for gateway identity
   and a dedicated synthetic MAC for the selected-client switch. Configure the
   actual Network Inform URL. Do not publish deployment configs or adoption keys.
4. Create `/etc/unifi-reporters/state/clients` and `state/gateway`, mode 0700,
   owned by `unifi-report`. Transfer existing per-device adoption state securely
   when migrating; stop old publishers before starting the same identities here.
5. Install `pkg/openwrt/unifi-telemetry.init` as executable
   `/etc/init.d/unifi-telemetry`. It requires `ujail`; a publisher fails closed if
   its jail cannot be created. Start it and adopt only the new emulated devices.
   Enable at boot after checking fresh reports in UniFi.

### Routed traffic between local networks

nlbwmon deliberately excludes connections whose two endpoints are local. For
example, a camera streaming across a VLAN to a recorder has no nlbwmon entry.
Native AP observations may cover the camera while the wired recorder still
shows no traffic from the gateway.

To measure these forwarded IPv4 flows, adapt
`pkg/openwrt/unifi-local.sample.nft` to the actual LAN devices and subnets and
install it as `/etc/nftables.d/90-unifi-local.nft`. It belongs inside the existing
`inet fw4` table. Check the complete configuration with `fw4 check` before loading
the snippet or restarting the firewall. Set `local_traffic` to `true` in the
collector configuration only after the two sets exist. Preserve the snippet in
`/etc/sysupgrade.conf` along with the existing reporter configuration.

The rules only count already-forwarded local packets after the main filter
chains. They add no allow, drop, NAT, or routing rules. Keep flow offloading
disabled for complete forwarding-counter coverage. Firewall reloads recreate
the counters; the observer handles their resets independently of nlbwmon.
Counters are attributed only to verified IPv4 IP/MAC observations. A new IP/MAC
pair establishes a fresh baseline so a previous address owner's traffic is not
assigned to the new owner. Traffic staying on a switch, AP, or hypervisor bridge
still requires native device statistics; IPv6-only discovery is not provided.

When upgrading an already adopted deployment from the old direction mapping,
preserve its last reported wire-counter offsets in the RAM checkpoint before
resuming reports. Simply swapping large cumulative counters can pollute recent
history. This does not repair historical upload/download labels. Do not publish
deployment checkpoints or configurations.

### Per-device history in LuCI

The optional collectd adapter reads the existing snapshot and exports measured
download/upload rates separately for Internet and routed local traffic. It does
not collect packets or require UniFi credentials. MAC addresses identify the
history files; LuCI resolves device names and addresses when displaying them, so
renaming a device does not split its history. Unknown accounting, new IP/MAC
pairs, router reboots and gaps longer than 90 seconds produce missing samples
instead of invented zero usage or counter spikes. Existing accounting totals
are not backfilled into the timeline.

1. Install `luci-app-statistics` and `collectd-mod-exec` with `apk`.
2. Install `collectd_telemetry.py` beside the existing reporter modules under
   `/usr/lib/unifi-reporter/`, owned by root and mode 0644.
3. Install the files from `pkg/openwrt/luci/` as follows:
   * `device_traffic.js` → `/www/luci-static/resources/statistics/rrdtool/definitions/device_traffic.js`
   * `devices.js` → `/www/luci-static/resources/view/statistics/devices.js`
   * `devices.css` → `/www/luci-static/resources/statistics/devices.css`
   * The `uplot/` directory → `/www/luci-static/resources/statistics/uplot/`
   * `luci-device-traffic.json` → `/usr/share/luci/menu.d/luci-device-traffic.json`
   * `luci-device-traffic-acl.json` → `/usr/share/rpcd/acl.d/luci-device-traffic.json`
4. Enable `luci_statistics.collectd_exec` and add this UCI section to
   `/etc/config/luci_statistics`:

   ```uci
   config collectd_exec_input 'device_traffic'
       option cmduser 'nobody'
       option cmdgroup 'unifi-report'
       option cmdline '/usr/bin/python3 /usr/lib/unifi-reporter/collectd_telemetry.py'
   ```

5. Keep the RRD data in `/tmp/rrd`, set
   `luci_statistics.collectd_rrdtool.CacheTimeout=30`, and enable native backups
   with `luci_statistics.collectd_rrdtool.backup=1`. A cron entry such as
   `15 */6 * * * /etc/init.d/luci_statistics backup` also saves history every six
   hours. Native backups go to `/etc/luci_statistics/rrdbackup.tgz` and restore
   when LuCI Statistics starts. An unexpected power loss can lose samples since
   the last backup. The native backup briefly restarts collectd to flush RRDs.
6. Validate generated configuration with `collectd -t -C <generated-config>`,
   then enable and restart `/etc/init.d/luci_statistics`.
7. Preserve the custom LuCI files and `uplot/` assets in `/etc/sysupgrade.conf`. The existing
   reporter-directory preservation covers the Python adapter; LuCI Statistics
   supplies its own history-backup hook for sysupgrade. Reinstall its packages
   after an upgrade as necessary.

Open **Statistics → Devices** at `/cgi-bin/luci/admin/statistics/devices`, select
a device and period, and allow two fresh collector samples for the first rates.
The page renders only the selected device and refreshes every 30 seconds. Its
interactive uPlot charts are served locally and read the existing RRD files;
changing the renderer does not replace the history. Hovering shows sample values,
rates use bit/s units, and unknown samples remain gaps. The bundled uPlot 1.6.32
files retain their upstream MIT license. The standard Statistics → Graphs page
still has the native PNG definition available.

The Devices page requires statistics, network-status and `luci-device-traffic`
read permissions. The added permission allows only per-device RRD fetch commands
under `/tmp/rrd`; it provides no configuration writes. The Exec
reader runs as `nobody` with the snapshot-reading group; the UniFi publishers
retain their existing user and filesystem jails.

The collector updates a RAM snapshot every approximately 30 seconds. Publishers
report every 30 seconds; probes and controller requests add a small delay.
Adoption metadata is persistent and written only when it changes. Frequently
updated snapshots and status files stay in RAM, avoiding repeated flash writes.

## Isolation and failure behavior

The root collector accepts no controller messages, opens no listener, and executes
only fixed read-only data queries and IP-address-validated ICMP probes. It cannot
be instructed through a report to apply UCI, routing, DHCP or firewall changes.

Both publishers run as the locked user with `no_new_privs` in read-only filesystem
jails. Router configuration and the ubus socket are absent. Only each publisher's
own adoption state and RAM status directory are writable. Effective capabilities
are zero. There are no router SSH keys or UniFi administrative API keys.

Unknown controller commands and incompatible responses stop the affected
publisher. Publishers are deliberately not automatically respawned after failure.
The collector is independent and continues its probes. Stale or invalid snapshots
are not reported. WAN address loss does not remove LAN observations.

If UniFi breaks gateway reporting, extra IP/traffic/system information can stop
updating. Real routing and DHCP continue. Native UniFi device reports and the
separate selected-client publisher continue independently. No software promises
vendor UI compatibility across future releases.

## Operations

```
/etc/init.d/unifi-telemetry status
cat /tmp/unifi-reporters/clients/status.json
cat /tmp/unifi-reporters/gateway/status.json
logread -e python3
```

To restart all custom reporting: `/etc/init.d/unifi-telemetry restart`.
Each publisher starts only when its matching `/etc/unifi-reporters/clients.json`
or `gateway.json` exists. Rename a configuration to `.json.disabled` to retire that
publisher across restarts. If the gateway alone covers the selected clients, the
virtual switch can be retired this way and forgotten in UniFi.
To disable it: `/etc/init.d/unifi-telemetry disable` followed by
`/etc/init.d/unifi-telemetry stop`. These actions do not restart network, firewall,
dnsmasq, UniFi APs or switches. Remove/forget emulated devices in UniFi only after
stopping their publishers.

For a normal sysupgrade, preserve `/etc/unifi-reporters/` and the collector config
securely, reinstall the code/dependencies/init script, then verify reporting.
Adoption state contains device-specific secrets and belongs in a private backup.
