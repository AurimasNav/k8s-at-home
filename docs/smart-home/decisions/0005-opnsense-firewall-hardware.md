# 0005 — OPNsense firewall hardware: Intel N150 mini-PC, not an official Deciso appliance

- **Status:** Proposed 2026-09-12 — hardware chosen, nothing purchased or migrated yet.
- **Scope:** Which box runs OPNsense when the Asus router stops being the router. Covers the
  hardware choice only; the migration itself (DHCP reservations, Matter/Thread multicast, VLAN
  posture) is recorded under *Consequences* because it constrains the choice.
- **Related:** [0004 — whole-home backup](0004-whole-home-backup-not-automatic.md) (the k3s node
  is on the backed-up side; the firewall should be too),
  [Matter-over-Thread setup](../../matter-thread-setup.md)

## Context

The house runs a flat `192.168.1.0/24` with an Asus AiMesh router at `.1` doing NAT, DHCP and
DNS. Moving to OPNsense gives real firewall rules and egress control for the Hikvision kit and the
Sungrow WiNet-S dongle. The question was whether to buy an official OPNsense appliance from Deciso
or generic hardware.

**Azure site-to-site is explicitly not a requirement.** That tunnel is work infrastructure. At home
it would only ever be a test bed, so it carries no weight in the hardware choice and the IPsec
throughput figures below are context, not a constraint. The Azure findings are kept under
*Consequences* because the research was done, not because anything depends on them.

Requirements:

- 4× 2.5GbE, fanless, low idle power (runs 24/7, on the inverter's `LOAD` side)
- Enough CPU headroom for Suricata and for VLANs later
- FreeBSD-supported NICs — this rules out most Realtek 2.5G parts

**Integrated WiFi was ruled out immediately.** FreeBSD has no 802.11ac/ax AP-mode support; the only
cards that do `hostap` are 802.11n Atheros parts. The Asus units stay as APs in AP Mode, which
keeps AiMesh working. That is the supported path, not a workaround.

## Decision

**Buy an Intel N150 4-port mini-PC with 4× Intel i226-V.** Two interchangeable brands: **CWWK**
(sold via Amazon.de, shipped from their German warehouse) and **Topton** (sold via AliExpress).
CWWK has no meaningful AliExpress presence — on AliExpress, Topton is the equivalent. The boards
are the same class and frequently the same ODM design; pick on price and delivery.

| Source | Link | Notes |
|---|---|---|
| Amazon.de — CWWK F2 barebone | https://www.amazon.de/CWWK-Firewall-Appliance-Computer-OPNsense/dp/B0DSJ64G82 | German warehouse, VAT paid, EU statutory warranty |
| Amazon.de — CWWK F4 8GB/128GB | https://www.amazon.de/CWWK-Upgraded-Firewall-Appliance-2-Display/dp/B0DTB6RWN1 | ready to run |
| Amazon.de — CWWK F2 32GB/512GB | https://www.amazon.de/CWWK-Firewall-Appliance-Computer-OPNsense/dp/B0DSJ5CYWT | more than needed |
| AliExpress — Topton N150 4×2.5G | https://www.aliexpress.com/item/1005004360072281.html | usually cheapest; import VAT and slower RMA |
| AliExpress — Topton N150 firewall | https://www.aliexpress.com/item/1005005161237666.html | alternate listing, same class |
| AliExpress — Topton N150 router | https://www.aliexpress.com/item/1005006810364387.html | alternate listing, same class |
| Topton direct | https://www.toptonpc.com/product/topton-firewall-mini-pc-intel-n150-n100-top-version-4x-2-5g-i226-v-fanless-soft-router-micro-appliance-pfsense-proxmox-aes-ni/ | |
| CWWK direct | https://cwwk.com/products/cwwk-f4-mini-pc-n150-n355-upgraded-n100-305-firewall-appliance-opnsense-mini-computer-with-4-port-i226-v-2-5gbe-lan-fanless-micro-pc-pcie3-0-x4-dual-4k-display-tf | USD, ex-VAT; their variant pricing has visible errors |

**Buy a pre-configured unit, not the barebone — and take 8GB, not 16GB.**

Normally the barebone plus own RAM and SSD is better value. In 2026 it is not. DDR5 retail prices
rose roughly **485% between August 2025 and August 2026** as AI and HBM demand crowded out
conventional DRAM production; German retail data in mid-August put DDR5 at 486% of its July 2025
level. A single 16GB DDR5 SO-DIMM is around **€200** as of 2026-09-12, against roughly €40
pre-surge. Vendors bundling RAM are still shipping stock bought at older contract prices, so the
configured listings are now the cheaper path by a wide margin — CWWK's own direct pricing asks only
about $25 to go from barebone to 8GB + 128GB, which is far below component cost today.

8GB is OPNsense's own recommended spec, and Suricata with the ET Open ruleset adds only ~500MB on
top. Paying ~€200 to reach 16GB is justified only if Zenarmor is planned, since it wants 8GB+ to
itself. There is one SO-DIMM slot, so a later upgrade replaces the stick rather than adding one —
but at current prices, defer that.

Re-check before ordering: the shortage is expected to persist into 2027, though the steepest
increases were cooling as of Q3 2026 on consumer affordability limits.

Whichever listing is used, confirm on the live page before ordering:

- **4× i226-V**, not i225 — the i225 had genuine link-flap errata across all three steppings
- **N150**, not N100 or an older J-series
- DDR5 SO-DIMM, single slot

AliExpress item IDs differ between gateways: a `.us` id of `3256805161237666` is `1005005161237666`
on `.com`. The `.com` form above is the one that resolves directly.

## Why not the alternatives

### Deciso DEC677 / DEC697 — rejected on silicon age, not on price

Prices verified 2026-09-12 on shop.opnsense.com. VAT status is not stated on the page; confirm at
checkout, it moves the comparison by about 21%.

| Model | Spec | Price |
|---|---|---|
| DEC677 | 4GB DDR3, 32GB SSD, 4× 2.5GbE | €598 |
| DEC697 | 8GB DDR3, 256GB NVMe, 4× 2.5GbE | €678 |

The DEC677 is **disqualified by its own product page** — "not intended for applications that
demand disk writes such as a web proxy, intrusion detection or local net flow recording." Wanting
Suricata means the DEC697.

Both use the **AMD GX-412HC**, a 28nm Jaguar-era embedded SoC from around 2013:

| | AMD GX-412HC (DEC697) | Intel N150 | Ratio |
|---|---|---|---|
| PassMark CPU Mark | 1,095 | 5,357 | 4.9× |
| Single Thread Rating | 414 | 1,896 | 4.6× |
| TDP | 7W | 6W | — |
| Memory | DDR3, 8GB fixed | DDR5-4800, to 32GB | ~3× bandwidth |
| IPsec throughput | 600 Mbps (rated) | >1 Gbps | — |

Roughly five times the compute at slightly *lower* power. After subtracting the bundled Business
Edition year (€149) the effective prices are close enough that this is a spec decision, not a price
one — and buying DDR3 hardware new in 2026 is the part that does not survive scrutiny.

**The N150 is not a Celeron.** Intel retired the Celeron and Pentium brands in 2023; the official
name is "Intel Processor N150" (Twin Lake, 4 Gracemont E-cores, 6MB cache, up to 3.60 GHz, 6W). It
occupies the market slot Celeron used to, which is why listings sometimes still write "Celeron
N150" — that is the seller's shorthand, not Intel's name. The J6412 in the Protectli *is* genuinely
Celeron-branded (Elkhart Lake, Tremont cores), and is the slower part.

The DEC697 is not a faster DEC677; it is the same CPU with more storage. Deciso's "suitable for
intrusion detection" distinction is about disk writes, not compute.

What is being given up: a 2-year EU carry-in warranty with a real RMA path, hardware validated
against the software, EU manufacture, and money going directly to the people who write OPNsense.

### OPNsense Business Edition — not bought

€149/yr. It is a release-train and enterprise-management product, not a feature unlock: a
conservative firmware repo (April/October releases), GeoIP without a MaxMind key, the official OVA,
and business-only plugins — OPNcentral, WAF, authoritative DNS, OpenID Connect, User Portal,
scheduled jobs. **None of it applies here**: one firewall, Blocky already serves DNS, the k3s
ingress already fronts anything exposed, one admin. Everything actually needed — IPsec, WireGuard,
VLANs, Suricata, HAProxy, CARP — is in the community edition. Worth buying as a donation; not for
features. Support is separate again at €329/yr.

### Protectli VP2420e — viable, rejected on CPU

€329 excl. / €398.09 incl. VAT barebone, EU store, coreboot, clean EU RMA. But it is a Celeron
J6412 — slower than the N150, with a roughly 400 Mbps IPsec ceiling — and was on backorder to
2026-09-25. The right answer if EU support matters more than headroom.

### Integrated-WiFi appliances — do not exist in any useful form

See *Context*. Separate APs, permanently.

## Consequences

### The i226-V erratum has to be handled on day one

The i226 advertises PCIe ASPM L1.2, but a hardware erratum makes exit latency longer than the
packet buffer absorbs under load — RX stalls, link flapping, `igc0: Watchdog timeout -- resetting`.
Real, and worst on exactly these Alder Lake-N boards.

Fixed upstream: FreeBSD PR #2318 adds `igc_disable_broken_aspm_l1_2()`, merged to main
**2026-07-31** and cherry-picked to release branches **2026-08-08** (closes FreeBSD bug #279245).
Recent enough that the installed OPNsense build should be checked for it.

Regardless, before migrating anything:

1. Update the BIOS — Intel NVM ≥ 2.22 corrects the link-flap; units often ship 2.13/2.14
2. Disable every ASPM option in BIOS
3. EEE already defaults off in the FreeBSD `igc` driver
4. If it still misbehaves: disable checksum/TSO/LRO offload, set `dev.igc.N.fc=0` per port

Do this *before* the migration: a flaky NIC looks exactly like a device dropping off the network. The
Flexit dropouts turned out to be a DHCP address change (`.134` → `.139`, found 2026-09-28), and link
flaps underneath would have made that much harder to pin down.

### Migrate flat first; VLANs are a separate, later decision

Nothing about the OPNsense swap requires VLANs, and doing both at once means debugging two changes
at once. OPNsense takes `.1`, the Asus goes to AP Mode, `192.168.1.0/24` stays.

**Egress blocking does not need VLANs.** Camera-to-internet traffic crosses the gateway, so outbound
rules on OPNsense close the Hikvision exposure item in [`../todo.md`](../todo.md) with three rules.
What a VLAN adds is *lateral* containment, which on a flat LAN no rule can see. When that is wanted:
one `VLAN 20 — cameras` (NVR plus the three Hikvisions, all wired, no mDNS or Thread dependency),
not a six-segment buildout. It needs a managed switch, and wireless VLANs would need Guest Network
Pro on every AiMesh node or replacement APs.

### Matter/Thread constrains what can ever be segmented

[`matter-thread-setup.md`](../../matter-thread-setup.md) assumes flat L2 throughout: OTBR
advertising RAs on `eno1`, `minimal_mdns` on 5353, `hostNetwork: true` so multicast is not boxed in
by the CNI. On the OPNsense side, set the LAN interface's IPv6 RA mode carefully — there will be two
RA sources on the same segment and OTBR's RIO advertisements must survive. Keep HA, OTBR and Matter
devices on one L2.

### Carry over before cutover

- DHCP reservations, at minimum `192.168.1.149` (nibegw, still open in [`../todo.md`](../todo.md))
  and `192.168.1.139` (Flexit, MAC `00:05:19:22:06:0A`, already reserved on the Asus)
- `gw.sync.lt: 192.168.1.1` in `gitops/blocky/configmap.yaml` stays valid if OPNsense takes `.1`
- Blocky as the DHCP-advertised DNS server
- Port forwards, DDNS and any VPN config move off the Asus, which loses them in AP Mode along with
  Adaptive QoS and Traffic Analyzer

### Azure site-to-site — reference only, not a driver

Recorded for the day it is wanted as a test bed. **The production tunnel is work infrastructure and
does not terminate here.** If it is ever built at home, keep it off the flat LAN: route only a
dedicated test subnet into Azure rather than `192.168.1.0/24`, so house devices and work resources
never see each other. Check employer policy before terminating anything work-related on a home
firewall.

OPNsense is **not** on Microsoft's validated VPN devices list. Neither is pfSense; VyOS and
EdgeRouter are, which shows the bar is vendor participation rather than capability. Microsoft's
position on unlisted devices is that they "still might work" — contact the manufacturer.

OPNsense does publish a first-party how-to, *IPsec VTI — connect to Microsoft Azure*: route-based
gateway, IKEv2, P1 AES-256/SHA256/DH2, P2 ESP AES-256/SHA256 with PFS off, MSS clamp 1350. Two
caveats — it is written against the deprecated legacy Tunnel Settings UI (build it in
Connections/swanctl instead, where PSKs move into a separate Pre-Shared Keys entry), and DH group 2
is 1024-bit. Route-based connections accept a custom IPsec/IKE policy, so force DH14 or better and
AES256-GCM. A dynamic WAN IP is fine: Azure Local Network Gateway accepts an FQDN (5-minute DNS
cache, first A record wins, no IPv6).

Practically: no Microsoft-blessed config guide and no joint support path. Fine here; it would be the
one real argument for a listed vendor if an SLA were attached.

## Implementation notes

Order of operations:

1. BIOS update and ASPM off, before the box sees production traffic
2. Install OPNsense, confirm the build includes the `igc` ASPM L1.2 fix
3. Rebuild DHCP reservations and point clients at Blocky
4. Cut over: OPNsense to `.1`, Asus to AP Mode, give it a static lease and reach its UI there
5. Add outbound block rules for the Hikvision NVR/cameras and the WiNet-S dongle
6. Verify Matter/Thread still commissions before touching anything else
7. *(optional)* Azure tunnel, only if ever wanted for testing — last, once the LAN is stable

Put the firewall on the inverter's `LOAD` side with the k3s node — per
[0004](0004-whole-home-backup-not-automatic.md) HA now survives an outage, and it should not lose
the network while doing so.

## Sources

- [DEC677](https://shop.opnsense.com/product/dec677-opnsense-desktop-security-appliance/) and
  [DEC697](https://shop.opnsense.com/product/dec697-opnsense-desktop-security-appliance/) — prices
  and the DEC677 IDS exclusion, read 2026-09-12
- [PassMark GX-412HC vs N150](https://www.cpubenchmark.net/compare/2473vs6304/AMD-GX-412HC-vs-Intel-N150)
- [FreeBSD PR #2318 — igc ASPM L1.2 fix](https://github.com/freebsd/freebsd-src/pull/2318)
- [Fixing i226 NIC drops on OPNsense](https://computingforgeeks.com/fix-intel-i226-nic-drops-opnsense/)
- [OPNsense: IPsec VTI — connect to Microsoft Azure](https://docs.opnsense.org/manual/how-tos/ipsec-s2s-route-azure.html)
- [Azure: About VPN devices](https://learn.microsoft.com/en-us/azure/vpn-gateway/vpn-gateway-about-vpn-devices)
  — validated device table, no OPNsense or pfSense entry
- [OPNsense Business Edition](https://shop.opnsense.com/product/opnsense-business-edition/) and
  [what it includes](https://docs.opnsense.org/be.html)
- [pfSense: recommended wireless hardware](https://docs.netgate.com/pfsense/en/latest/wireless/hardware.html)
  — no 802.11ac/ax AP mode on FreeBSD
- [Protectli EU 4-Port](https://eu.protectli.com/vault-4-port/)
