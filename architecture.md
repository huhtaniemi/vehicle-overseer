```mermaid
flowchart LR
  subgraph Targets
    Service["Target service<br/>POST pings (~3s)<br/>{uid, label, ip-address, state, data}"]
    Device["Target device"]
    Stub["Current stub service (Python)<br/>device-service/service.py<br/>waits for tun0 IPv4 before POST<br/>TCP action/log endpoints"]
    Updater["Target updater (Python)<br/>updater/updater.py<br/>pulls manifest + artifact<br/>uses device UID"]
  end

  subgraph Backend
    API["HTTP API<br/>POST /api/ping<br/>POST /api/action/select"]
    Manifest["Update manifest API<br/>GET /api/device/manifest<br/>per device/group targeting"]
    Artifact["Artifact download<br/>GET /api/device/artifacts/<artifact-id>"]
    WS["WS /ws (same port)<br/>init + entry deltas"]
    ActionConn["Per-action connection<br/>backend → device<br/>host = ip-address<br/>port = deviceActionPort"]
    Stages["Backend stages (real steps)<br/>connecting/apply/restart/success"]
    Logs["WS /logs?uid=...<br/>log proxy<br/>port = deviceLogPort"]
    DB["database<br/>artifacts + versions<br/>device targets + keys + tokens"]
  end

  subgraph UI["Web UI (mobile-first)"]
    List["Entry list<br/>stable ordering + update flash<br/>last-seen tick (1 Hz)<br/>errors persist"]
    Picker["IP picker popup + confirm popup"]
    Status["Status bar<br/>online/offline counts<br/>backend URL (in settings popup)"]
    LogView["Fullscreen log overlay<br/>filter + highlight"]
  end

  Service -->|POST pings| API
  Stub -->|POST pings| API
  Updater -->|GET manifest (uid)| Manifest
  Manifest --> Artifact
  Updater -->|GET artifact| Artifact
  API --> WS
  WS --> List
  List --> Picker
  Picker -->|confirmed IP| API
  API --> ActionConn
  ActionConn --> Stages
  Stages --> WS
  API --> DB
  Logs --> LogView
  UI -->|connects| Logs

  ActionConn -->|new connection per action| Device
  ActionConn -->|new connection per action| Stub

  Simulator["Simulator tool (Python)<br/>net-setup/net-cleanup for dummy iface<br/>posts pings + emulates action/log TCP endpoints"] -->|POST pings| API
```
