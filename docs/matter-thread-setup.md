# Matter-over-Thread Setup on k3s

This document covers the host-level and workload configuration required to run Home Assistant with Matter-over-Thread support using OTBR (OpenThread Border Router) and python-matter-server on k3s.

## Architecture

```
 Phone (BLE)          LAN (eno1)              Thread mesh (wpan0)
     |                    |                         |
     v                    v                         v
 HA Companion -----> Home Assistant -----> python-matter-server
                     (hostNetwork)          (hostNetwork)
                          |                         |
                          +-----> OTBR (hostNetwork) <----> Nabu Casa ZBT-2
                                  mdnsd + ot-ctl           (USB Thread radio)
```

All three workloads (Home Assistant, OTBR, python-matter-server) run with `hostNetwork: true` on the same node so they share the host's network namespace. This is required because:

- **OTBR** needs direct access to the backbone interface (`eno1`) for border routing between Thread and LAN, the TUN device for wpan0, and the USB radio device.
- **python-matter-server** uses CHIP SDK's `minimal_mdns` which binds UDP port 5353 for multicast DNS. mDNS multicast does not cross Kubernetes pod network boundaries.
- **Home Assistant** needs to reach both services and handle BLE commissioning from companion apps.

## Host Prerequisites

### IPv6 Sysctl Settings

These settings are critical. Without them, IPv6 traffic cannot flow between the Thread mesh and the LAN, causing Matter device commissioning to fail at the mDNS resolution step after BLE handoff.

Apply on the k3s host:

```sh
sysctl -w net.ipv6.conf.all.forwarding=1
sysctl -w net.ipv6.conf.all.accept_ra=2
sysctl -w net.ipv6.conf.eno1.accept_ra=2
sysctl -w net.ipv6.conf.eno1.accept_ra_rt_info_max_plen=64
```

Persist across reboots by creating `/etc/sysctl.d/99-matter-thread.conf`:

```ini
# IPv6 forwarding - allows OTBR to route between Thread (wpan0) and LAN (eno1)
net.ipv6.conf.all.forwarding = 1

# accept_ra=2 - continue accepting Router Advertisements even with forwarding enabled
# Without this, eno1 loses its IPv6 address when forwarding is turned on
net.ipv6.conf.all.accept_ra = 2
net.ipv6.conf.eno1.accept_ra = 2

# Accept Route Information Options (RFC 4191) with prefix length up to /64
# OTBR advertises Thread mesh prefixes via RIO - the host must accept them
# to build routes that reach Thread devices
net.ipv6.conf.eno1.accept_ra_rt_info_max_plen = 64
```

### Kernel Module

The `ip6table_filter` module must be loaded for OTBR's IPv6 firewall rules:

```sh
modprobe ip6table_filter
```

Persist across reboots by creating `/etc/modules-load.d/matter-thread.conf`:

```
ip6table_filter
```

### IPv6 Route for Thread OMR Prefix

Since OTBR runs with `hostNetwork: true`, its Router Advertisements originate from the host's own link-local address. The Linux kernel ignores RAs from itself, so the Thread OMR prefix route is never installed automatically despite correct `accept_ra_rt_info_max_plen` settings.

The OTBR deployment includes a `postStart` lifecycle hook that dynamically queries the OMR prefix and adds the route:

```sh
prefix=$(ot-ctl br omrprefix | grep "Local:" | awk '{print $2}')
ip -6 route add "$prefix" dev wpan0
```

This runs in a retry loop (up to 60 seconds) to wait for OTBR to initialize. Since the prefix is read from OTBR at runtime, it works even if the Thread network is reset and a new prefix is generated.

The current OMR prefix can be verified via:

```sh
kubectl exec deploy/otbr -n otbr -- ot-ctl br omrprefix
```

### USB Device

The Nabu Casa ZBT-2 (or similar Thread radio) must be connected to the k3s host. Verify it appears:

```sh
ls -la /dev/serial/by-id/
```

The device path (e.g., `/dev/serial/by-id/usb-Nabu_Casa_ZBT-2_DCB4D9124108-if00`) is referenced in the OTBR deployment.

## Workload Configuration

### OTBR

- `hostNetwork: true` with `privileged: true`
- `BACKBONE_IF=eno1` - the host's LAN interface
- `FIREWALL=0`, `NAT64=0` - disabled to avoid complexity
- USB radio mounted from `/dev/serial/by-id/...` to `/dev/ttyACM0`
- TUN device mounted from `/dev/net/tun`
- PVC mounted at `/data` - persists the Thread dataset across pod restarts (the container symlinks `/var/lib/thread` to `/data/thread`), keeping the same network name, key, and OMR prefix
- `postStart` lifecycle hook dynamically queries OTBR for the OMR prefix and adds the IPv6 route via wpan0 (retries for up to 60 seconds while OTBR initializes)
- Runs `mdnsd` (Apple mDNS daemon) which advertises `_meshcop._udp` for border router discovery

### python-matter-server

- `hostNetwork: true` with `privileged: true`
- `--primary-interface=eno1` - tells the CHIP SDK which interface to use for mDNS advertisements and link-local addresses
- Uses CHIP SDK's `minimal_mdns` on UDP port 5353 for device discovery
- Both OTBR's `mdnsd` and matter-server's `minimal_mdns` can coexist on port 5353 via Linux `SO_REUSEPORT` for UDP multicast
- PVC for `/data` to persist node credentials and commissioning state
- Startup "Failed to advertise records" errors on `lxc*`/`cilium_*` interfaces are harmless — these CNI interfaces don't support IPv6 multicast; the eno1 endpoint works correctly

### Home Assistant

- `hostNetwork: true` with `NET_ADMIN` and `NET_RAW` capabilities
- `dnsPolicy: ClusterFirstWithHostNet` to resolve cluster-internal service names
- Integrations configured:
  - **Matter**: WebSocket URL `ws://matter-server.matter-server:5580/ws`
  - **OTBR**: REST URL `http://otbr.otbr:8081`
  - **Thread**: auto-configured via OTBR integration

## Router Configuration

The LAN router must allow mDNS multicast traffic between wired and wireless clients. Without this, phones on WiFi cannot discover the OTBR (which advertises `_meshcop._udp` on the wired interface), and Matter commissioning fails with "your network needs a border router."

On **Asus routers (Merlin firmware)**:
- **LAN > IPTV** tab: enable **"Enable multicast routing (IGMP Proxy)"**

On other routers, look for:
- Multicast routing / IGMP Proxy — **enable**
- AP isolation / Client isolation — **disable**
- IGMP Snooping — **disable** (if multicast still doesn't cross wired/wireless)

## Companion App Configuration

Both iOS and Android companion apps need the internal URL set to `http://<host-ip>:8123` (not the Kubernetes service port). This allows the app to communicate directly with HA for Matter commissioning flows.

## Commissioning Flow

1. Phone's companion app initiates Matter commissioning via BLE
2. Phone discovers the Thread border router via mDNS (`_meshcop._udp`)
3. Phone shares Thread credentials with the device over BLE
4. Device joins the Thread network and gets a mesh-local IPv6 address
5. Phone hands off to HA, which delegates to python-matter-server
6. python-matter-server discovers the device via mDNS on the Thread/LAN boundary
7. IPv6 traffic flows: matter-server (eno1) -> OTBR routing -> wpan0 -> Thread device

Steps 6-7 require the IPv6 sysctl settings to be correct - without forwarding and proper route acceptance, the host cannot route to Thread mesh addresses.

## Troubleshooting

### "No border router found"

- Verify OTBR's `mdnsd` is running: `kubectl exec deploy/otbr -n otbr -- s6-svstat /run/service/mdns`
- Check mDNS advertisement: `kubectl exec deploy/home-assistant -n home-assistant -- avahi-browse -t _meshcop._udp` (or use `dig @<host-ip> -p 5353 _meshcop._udp.local PTR`)
- Ensure companion app has the correct internal URL (`http://<host-ip>:8123`)

### "Timeout waiting for mDNS resolution" in matter-server

- Verify IPv6 forwarding: `cat /proc/sys/net/ipv6/conf/all/forwarding` (must be `1`)
- Verify accept_ra: `cat /proc/sys/net/ipv6/conf/eno1/accept_ra` (must be `2`)
- Verify rt_info_max_plen: `cat /proc/sys/net/ipv6/conf/eno1/accept_ra_rt_info_max_plen` (must be `64`)
- Check Thread routes exist: `ip -6 route | grep wpan0`
- Ensure matter-server is on hostNetwork (mDNS multicast does not cross pod network boundaries)

### "Failed to advertise records" in matter-server

- Usually caused by missing IPv6 configuration, not the port 5353 "conflict"
- Verify all sysctl settings above
- Verify `ip6table_filter` module is loaded: `lsmod | grep ip6table`

### OTBR Thread network resets on restart

With the PVC mounted at `/data`, the Thread dataset persists automatically. If the PVC is lost or the network needs manual restore:

```sh
kubectl exec deploy/otbr -n otbr -- ot-ctl dataset set active <hex-tlv>
kubectl exec deploy/otbr -n otbr -- ot-ctl ifconfig up
kubectl exec deploy/otbr -n otbr -- ot-ctl thread start
```

The active dataset TLV can be found in HA under **Settings > Devices > Thread > Configure > Thread Network**, or via:

```sh
kubectl exec deploy/otbr -n otbr -- ot-ctl dataset active -x
```

### "Your network needs a border router" on phone

- The phone cannot discover the OTBR via mDNS — multicast is not crossing between wired and wireless
- Enable multicast routing (IGMP Proxy) on the router (see Router Configuration above)
- Verify with an mDNS browser app on the phone that `_meshcop._udp.local` is visible
