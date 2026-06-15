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

The numeric variables are continuously updated and marked with historizing/access flags.

## Helpful tuning

- Increase `--history-seed-minutes` if your conformance history window is large.
- Reduce `--history-sample-seconds` to make history denser.
- Reduce `--update-interval-seconds` for faster live change detection.

## Connect i3x2ua

Example environment variable:

```powershell
$env:I3X_OPCUA_ENDPOINT = "opc.tcp://127.0.0.1:4840/freeopcua/server/"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```
