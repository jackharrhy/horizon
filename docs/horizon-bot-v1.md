# Demand-rendered bot engine

This fork supplies CatanCraft's native Minecraft Java 26.3/protocol-777 body.
The controller, dashboard, FFmpeg supervisor and disposable lab live in the parent
CatanCraft repository, not in this engine. The initial integrated experiment is
preserved in commit `2ba10267`.

Build the native binaries:

```sh
cargo build --locked --profile fast -p horizon-client --bins
```

Launch only with fresh, isolated game/launcher/XDG homes as specified in AGENTS.md:

```sh
target/fast/horizon-client --bot --server 127.0.0.1:25576 --name CatanCraft \
  --control-socket /path/to/bot.sock --resources /path/to/prepared/26.3
```

`--bot` uses the same client progression and player simulation as ordinary play,
without requiring frame draws to advance. Graphics resources, textures and meshes
are retained. A window system/GPU is still required. Rendering shares the client
thread, so this is not a hard real-time runtime or a separate render worker.

## Local control

The existing horizon-ctl socket/MCP interface provides state, actions and events:

```sh
target/fast/horizon-ctl --socket /path/to/bot.sock state
target/fast/horizon-ctl --socket /path/to/bot.sock follow cheesemany
target/fast/horizon-ctl --socket /path/to/bot.sock move-ticks forward 20
target/fast/horizon-ctl --socket /path/to/bot.sock stop
target/fast/horizon-ctl --socket /path/to/bot.sock screenshot /path/to/view.png
```

`move-ticks` releases input after the exact number of client ticks, including
catch-up batches. Follow binds a visible player UUID, uses loaded-terrain planning,
and clears on stop, target loss, death, dimension change or navigation failure.
In bot mode the executor shortcuts only collision-checked, fully supported level
segments. Travel input and gaze are independent; turns have bounded angular speed
and acceleration. No random jitter or separate bot physics is introduced.

`vision idle` draws only requested screenshots. `vision live` draws at approximately
10 FPS. Screenshots carry client tick/pose, rendered camera/interpolation and pending
chunk metadata; they are client observations, not privileged server truth.

## Native media output

`broadcast start 127.0.0.1:PORT FPS [audio]` sends bounded raw media to a local encoder.
FPS must be 1..60. `broadcast audio on|off` changes mixer capture without resetting
the connection or clock. `broadcast stop` disables capture. Game audio capture
requires disabled physical speaker output (`HORIZON_NO_AUDIO=1`).

Packet headers are little-endian `<BI` (kind, payload length):

- Kind 1: `<IIQ` width, height, microseconds since broadcast start, then RGBA pixels.
- Kind 2: `<Q` starting sample-frame index, then stereo 48 kHz float32 little-endian PCM.

Two bounded asynchronous GPU readbacks avoid waiting for every captured frame.
Presentation uses absolute deadlines and refreshes interpolation before drawing;
the hidden-window idle FPS cap does not apply to bot scheduling. The simulation
remains at vanilla's 20 Hz. An owned buffer queue moves media off the client thread.

This is not zero-copy platform encoder integration. CatanCraft currently handles
H.264/AAC encoding and browser delivery outside the engine. No production auth,
survival policy, chat command authorization or autonomous agent is supplied here.
