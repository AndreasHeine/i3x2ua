# i3x2ua Conformance OPC UA Fixture

This folder contains a small `asyncua` OPC UA server fixture intended to make i3X conformance checks more reliable.

It provides:

- deterministic live changes for subscription tests (`SUB-*`)
- pre-seeded historical values for history tests (`QRY-*`)
- multiple objects with numeric child variables so object selection is less likely to land on empty/infrastructure nodes

## Run

From repository root:

```powershell
uv run python conf-test-server/server.py
```

Custom options:

```powershell
uv run python conf-test-server/server.py --host 127.0.0.1 --port 4840 --history-seed-minutes 90 --update-interval-seconds 0.75
```

## Default endpoint

`opc.tcp://0.0.0.0:4840/freeopcua/server/`

## Exposed model

- `ConformancePlant`
- `ConformancePlant/LineA/Mixer-01`
- `ConformancePlant/LineB/Heater-01`

Each machine includes:

- `Temperature` (Double)
- `Pressure` (Double)
- `Speed` (Double)
- `IsRunning` (Boolean)

All four variables are marked with historizing/access flags.
The numeric variables are continuously updated, and `IsRunning` toggles periodically so history queries can return boolean samples as well.
On startup, the fixture also reads back a few raw history points from each signal and logs a warning if the history backend is not returning samples.

## Helpful tuning

- Increase `--history-seed-minutes` if your conformance history window is large.
- Reduce `--history-sample-seconds` to make history denser.
- Reduce `--update-interval-seconds` for faster live change detection.

## Connect i3x2ua

### Single-line launch (PowerShell)

Start i3x2ua with all conformance-relevant settings in one command:

```powershell
$env:I3X_OPCUA_ENDPOINT="opc.tcp://127.0.0.1:4840/freeopcua/server/"; $env:I3X_OPCUA_SECURITY_MODE="None"; $env:I3X_OPCUA_SECURITY_POLICY=""; $env:I3X_SUBSCRIPTIONS_INITIAL_VALUES="true"; $env:I3X_SUBSCRIPTION_INTERVAL_SECONDS="0.2"; $env:I3X_MODEL_PRELOAD_ON_STARTUP="true"; $env:I3X_MODEL_PRELOAD_BLOCKING="true"; $env:I3X_SKIP_OPCUA_CONNECT="0"; uv run uvicorn i3x_server.main:app --host 127.0.0.1 --port 8000 --loop none
```

### Single-line launch (cmd)

```cmd
set "I3X_OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840/freeopcua/server/" && set "I3X_OPCUA_SECURITY_MODE=None" && set "I3X_OPCUA_SECURITY_POLICY=" && set "I3X_SUBSCRIPTIONS_INITIAL_VALUES=true" && set "I3X_SUBSCRIPTION_INTERVAL_SECONDS=0.2" && set "I3X_MODEL_PRELOAD_ON_STARTUP=true" && set "I3X_MODEL_PRELOAD_BLOCKING=true" && set "I3X_SKIP_OPCUA_CONNECT=0" && uv run uvicorn i3x_server.main:app --host 127.0.0.1 --port 8000 --loop none
```

Note: In Windows `cmd`, avoid `set VAR=value && ...` because the space before `&&` becomes part of the value (for example `true `), which can break boolean parsing. Use `set "VAR=value"` as shown above.

### Variable reference

| Variable | Recommended value | Purpose |
|---|---|---|
| `I3X_OPCUA_ENDPOINT` | `opc.tcp://127.0.0.1:4840/freeopcua/server/` | Points i3x2ua at the fixture server |
| `I3X_OPCUA_SECURITY_MODE` | `None` | No TLS needed for local fixture |
| `I3X_OPCUA_SECURITY_POLICY` | _(empty)_ | Matches security mode None |
| `I3X_SUBSCRIPTIONS_INITIAL_VALUES` | `true` | Seeds current values into new subscriptions immediately |
| `I3X_SUBSCRIPTION_INTERVAL_SECONDS` | `0.2` | Subsecond polling keeps update queues fresh for conformance timing |
| `I3X_MODEL_PRELOAD_ON_STARTUP` | `true` | Ensures model is ready before conformance suite connects |
| `I3X_MODEL_PRELOAD_BLOCKING` | `true` | Blocks startup until model is fully loaded |
| `I3X_SKIP_OPCUA_CONNECT` | `0` | Must be 0 (default) to actually connect |
