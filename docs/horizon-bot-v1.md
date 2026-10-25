# Demand-rendered bot client (v1 experiment)

Horizon can run an external-server client with networking, physics and control actions
independent of frame drawing. `--bot` opts into this experiment; normal play is unchanged.
This is a bot **body**, not an autonomous AI or a replacement for Catancraft's behavior policies.

## Try the disposable lab

Build the game and control tool:

```sh
cargo build --locked --profile fast -p horizon-client --bins
```

The lab requires your own pinned Minecraft 26.3 server JAR, prepared Horizon resources,
and Java 25. Read the [Minecraft EULA](https://www.minecraft.net/eula) before accepting it:

```sh
python3 tools/horizon_bot_lab.py --accept-eula \
  --server-jar /path/to/26.3/server.jar \
  --resources /path/to/horizon-client/26.3 \
  --java /path/to/jdk-25/bin/java --check
```

After the checks pass:

- Join **127.0.0.1:25576** with a Minecraft Java 26.3 client.
- Open **http://127.0.0.1:8776** for follow, stop, jump, respawn, bounded coordinate travel and pictures.
- The bot is **HorizonLabBot**. Enter your player name in the panel and press Follow player.
- Request a single picture, or enable Watch for one requested picture per second.
- `cheesemany` is a creative operator in this disposable lab; the bot is in survival, on peaceful.
- Ctrl+C stops the lab and its own child processes. Temporary worlds/logs are retained for inspection.

Both TCP listeners bind **127.0.0.1 only**. The lab deliberately uses offline identities;
it is not an authentication test and must not be exposed to the network. It never reads
Prism/Catancraft credentials or uses their worlds, configuration, ports or resident process.
Every run creates fresh runtime directories. `.local/bot-lab.json` records the current
test address, control socket and log directory. Override `--port` and `--web-port` if needed.
Nondefault ports use `.local/bot-lab-PORT.json` instead.

### Dashboard and live video

Attach the richer dashboard without restarting the lab:

```sh
python3 tools/horizon_bot_dashboard.py --lab .local/bot-lab.json
```

Open **http://127.0.0.1:8786**. The main view has video, sound, Follow/Stop and health/food.
Coordinates sit under More controls. Frame rate, position and playback details are in Stats;
activity and raw data stay collapsed.
The dashboard is light-only. The original panel can remain open.

**Start stream** enables native game-frame capture and a local FFmpeg encoder.
Install FFmpeg with libx264 and AAC support first. Select 30 or 60 FPS and optionally
enable **Game audio**. This is the bot's own stereo mixer, not desktop/microphone capture;
speaker output must be disabled (`HORIZON_NO_AUDIO=1`, already set by the lab).
Browser autoplay rules may require pressing Play when enabling sound.

The pipeline is RGBA/stereo 48 kHz PCM over loopback TCP → FFmpeg H.264/AAC fragmented
MP4 → browser MediaSource. It is not an RTMP endpoint and requires a browser supporting
H.264/AAC MediaSource. The AAC track stays present while watching, with silence when Sound
is off. Sound toggles native capture and browser muting without restarting the encoder,
changing the stream URL or resetting the playback clock. The muted bot does not decode
or mix game sounds; the encoder still writes silent AAC to keep browser playback stable.
On macOS a hardware probe selects VideoToolbox H.264 when available; other systems or
failed probes use low-latency x264. The browser codec declaration comes from the actual
AVC decoder configuration, including the level needed for 720p60.
Frames use two bounded asynchronous GPU readbacks; encoding, pipe writes and browser delivery happen
off the simulation thread. Rendering still shares the game thread and can delay ticks.
Requested FPS is a ceiling, not a guarantee. Presentation uses absolute deadlines and
refreshes interpolation at render time without increasing the 20 Hz simulation rate.
The dashboard reports source capture FPS separately from browser presentation FPS,
buffered headroom (not end-to-end latency), dropped frames and stalls. Diagnostics include
duplicated encoder inputs, capture interval p95, pipe blocking and fragment delivery gaps.

FFmpeg emits independently decodable, keyframe-aligned fragments approximately every
200 ms. Playback targets roughly 600 ms of buffered media, corrects ordinary drift using
small playback-rate changes, and seeks only on attachment or major discontinuities.
Both video and optional audio share a clock mapped from the first captured frame.
An encoder more than 750 ms behind is stopped with an explicit lower-FPS recommendation,
rather than sending an unlimited burst of stale frames.
Pipe writes also have a 750 ms deadline. A requested video frame that has already left
the bounded half-second queue stops the stream rather than substituting future footage
at an old timestamp. Raw frames and cached fragments are released when stopping.
Paused playback retains a bounded rolling window; resuming returns near the live edge.

This is not zero-copy Metal-to-VideoToolbox capture: 720p60 RGBA is approximately 221 MB/s
before conversion. Native capture avoids a second full-frame packet copy and uses bulk
row copies, but GPU readback and local transport still cost bandwidth. Mediabunny or a
custom WebCodecs player would not repair upstream capture cadence; neither is required
for this MSE player. Sub-100-ms interaction would warrant a different transport/player.

**Stop stream** stops capture, audio mixing and FFmpeg, leaving the bot connected.
With `vision idle`, no more frames are drawn. Closing every viewer automatically stops
the encoder after 12 seconds. Up to four viewers share one encoder; slow viewers are
disconnected rather than building an unlimited queue. A stream restart changes its URL.
Capture frame remains available without FFmpeg. Do not expose this unauthenticated local
development dashboard through a tunnel or public listener.

`--check` exercises ticking without frame draws, server-observed movement/collision,
mouse turning without frames, screenshots, cancellation, idle/live movement replay,
and following a second disposable client (including retargeting, stop, death/respawn and target loss).
It then leaves the lab running for manual testing. This is an integration check, not a
vanilla differential proof of every movement rule.

## Runtime interface

Start a client with isolated game/config homes as described in `AGENTS.md`:

```sh
target/fast/horizon-client --bot --server 127.0.0.1:25576 --name HorizonLabBot \
  --control-socket /path/to/bot.sock --resources /path/to/resources
```

`--bot` requires a server and control socket. It uses a hidden, offscreen renderer and
disables focus pausing. The same `advance_client` implementation drives ordinary frames
and the bot runtime. Client timing still uses Horizon's clock; rendering does not add ticks.

State and gameplay `horizon-ctl` commands use the existing socket and MCP surface:

```sh
target/fast/horizon-ctl --socket /path/to/bot.sock state
target/fast/horizon-ctl --socket /path/to/bot.sock follow cheesemany
target/fast/horizon-ctl --socket /path/to/bot.sock stop
target/fast/horizon-ctl --socket /path/to/bot.sock move-ticks forward 20
target/fast/horizon-ctl --socket /path/to/bot.sock screenshot /path/to/view.png
```

`vision idle` draws requested pictures only (the default). `vision live` continuously
draws offscreen frames, scheduled at up to approximately 10 FPS; `vision idle` stops that.
The lab's Watch mode instead requests screenshots, so it has no separate player connection.
`status` reports drawn frame count and presentation state. A screenshot includes its client
tick, dimension, player pose, rendered camera/interpolation, capture time and pending work.
An image can contain incomplete geometry while chunks/meshes are loading; check the metadata.

For reproducible input scripts, `move-ticks DIR N` holds movement for exactly N client
ticks and releases it between ticks, even when a slow picture causes a batch of catch-up
ticks. It requires `--bot`. Ordinary `move DIR SECONDS` uses wall time instead; separate
`key down`, `wait`, `stop` socket requests are not an exact-tick input script.

Follow binds a visible player's UUID, uses the existing loaded-terrain player pathfinder,
and stops on target loss, death, dimension change, excessive separation or navigation failure.
Explicit movement cancels follow. Stop clears follow and held inputs. Nothing authorizes
actions from displayed chat messages; follow is a trusted local-control operation only.

### Less mechanical navigation

In bot mode, the grid path is a plan, not a sequence of compulsory block-centre stops.
The executor shortcuts clear, level segments using sampled, inflated player collision
bounds and full ground support. Steps and drops retain the original route. It does not
cut curves through walls or unsupported corners merely to look smoother.

Travel uses Horizon's existing controller-stick input so the gaze can differ from the
movement heading. Head turns have bounded angular speed and acceleration, updated once
per client tick rather than per draw or socket poll. The bot slows near arrival, turns
before reversing, looks toward its leader while following, and tracks the leader's eyes
when stopped. Follow resumes outside a wider radius than its stopping radius, avoiding
constant shuffling when someone moves slightly. These are actual player rotations sent
to the server, not a camera-only visual trick. No random head jitter is added.

Reference points (conceptual references, not copied implementations):

- [Mineflayer-pathfinder human-like walking](https://github.com/PrismarineJS/mineflayer-pathfinder#human-like-walking):
  separates planning from steering, with pure pursuit, head gestures and arrival behaviour.
- [Baritone LookBehavior](https://github.com/cabaletta/baritone/blob/master/src/main/java/baritone/behavior/LookBehavior.java):
  separates looking from path execution. Some Baritone smoothing is client-only; this
  experiment keeps the visible bot and server-reported rotation consistent.

After rebuilding, `tools/horizon_bot_lab_reload.py --resources /path/to/resources` replaces
only the lab bot and supervises it until the original lab ends. The vanilla server,
world and connected human players stay running. The replacement starts in stay mode.

## Deliberate v1 limits

- Graphics are **retained**, not removed: this still needs a window system/GPU to initialize.
- CPU meshing, textures and GPU allocations remain while idle. Only continuous drawing is eliminated.
- Rendering and PNG readback run on the client thread; a requested picture can briefly delay ticks.
  This is not an independent render worker or a hard real-time runtime.
- Follow/goto inherit Horizon's basic player planner. No digging, scaffolding, advanced
  parkour, survival policy, automatic eating, natural-language agent or Catancraft adapter is provided.
- GUI widgets depend on rendered layout; request a picture before reading/clicking them.
  General inventory/menu automation has not been acceptance-tested in bot mode.
- Observations are client-side predictions/received data, not privileged server truth.
- The lab uses an offline account. Production sign-in/refresh and secure-chat provenance
  remain separate work; do not transplant this lab's authentication policy into Catancraft.
- The driver currently uses Unix sockets. Windows lab support has not been implemented.

Next steps: compare difficult terrain against Catancraft's current backend, introduce a
narrow state/action/event adapter, then gate idle mesh work and evaluate lazy graphics startup.
Do not copy or fork the physics implementation into a separate bot simulation.

## Local verification

Verified on Apple M5 / macOS against a disposable vanilla 26.3 server:

- `--check`: all lab scenarios above passed, including server-observed collision and
  exact 20-tick input replay with rendering idle/live. Reset includes released ticks
  to settle velocity and vanilla's double-tap sprint timer; a teleport alone is not
  a complete input-state reset.
- Two screenshots during movement produced distinct capture ticks/poses, exactly two
  frame draws, then no idle draws. The connection reported zero packet decode errors.
- Browser panel loaded and displayed the bot's first-person PNG with camera metadata.
  Foreign Host/Origin requests were rejected.
- Ordinary in-frame and integrated-server-thread client smoke runs passed, along with
  the existing agent-control events scenario over both CLI and MCP.
- Debug-profile tests, run serially: 1,058 library tests passed; the latest binary
  suites passed 118 tests (including the new steering tests). There are 29 ignored tests.
  Optimized tests without debug symbols fail backtrace assertions; parallel credits
  tests also collided on temporary paths. Those files were not modified.
- Clippy (`--lib --bins -D warnings`), rustfmt, three lab-driver unit tests, generated
  command-reference verification and the public-tree check passed.

The complete differential parity matrix, production authentication and arbitrary terrain
have not been verified by this experiment. No real Catancraft or Cheesetown testing occurred.

The smoother executor was additionally checked on a separate disposable server: diagonal
travel stayed within 0.6 blocks of its direct line, a route around the solid wall completed,
rotations respected the 9-degree-per-tick yaw bound, and idle eye tracking did not move the
bot. All earlier lab checks still passed. The original interactive lab server/world was
left running and only its bot process was replaced.

The dashboard/stream was checked on another disposable server: Chrome MediaSource played
1280×720 constrained-baseline H.264 / stereo 48 kHz AAC, including newly connecting viewers.
A server-triggered experience-orb sound produced nonzero mixer PCM and nonzero decoded AAC
(peak approximately 0.399); browser tests stayed muted. Follow tracked a walking leader's
right-angle turn. The client maintained approximately 20.0 ticks/sec during streaming,
with approximately 26–27 captured FPS at a requested 30 FPS on this machine.
Stopping returned to zero idle draws; an unwatched stream shut down native capture and
FFmpeg after its viewer timeout. Foreign Host/Origin requests were rejected. Light desktop
and dark 390-pixel mobile layouts, snapshots and basic controls were browser-checked.
These measurements are not a performance guarantee or broad browser-compatibility test.

After the streaming timing review, a separate 78-second 720p60 run (including turns and
idle time beyond the hidden-window throttle) held 60.0 source FPS across all samples,
capture interval p95 ≤17.8 ms, and 20.0 simulation ticks/sec. A 90-sample browser run
measured approximately 59.96–60.0 presentation FPS, two total dropped frames and no
catch-up seeks. The stall counter stayed at its startup value of two throughout sampling.
The normal hidden-window frame limiter is bypassed only in bot mode, whose presentation
deadline already controls pacing. Merely raising the encoder FPS had not fixed that cap.

VideoToolbox output was probed as H.264 Baseline Level 3.2 at 60/1 FPS.
Audio-enabled output carried 48 kHz stereo AAC and a server-triggered
sound decoded with peak ≈0.408. A forced x264 fallback produced valid 30/1 FPS output.
A paused viewer stayed within an eight-second rolling window and resumed; an injected
clean EOF cleared its attachment and reconnected successfully. The latest full disposable
lab gameplay checks passed. The updated dashboard stayed light under a dark OS preference.

Sound-toggle checks used a fresh disposable lab and a browser audio analyser routed
through zero gain to avoid speaker output. Four successive toggles kept the same stream
ID and advancing playback time, with no media reset or error notice. Walking produced
nonzero decoded browser audio (peak ≈0.036). An injected autoplay rejection fell back to
muted video and asked for a Sound click rather than leaving playback stopped.
